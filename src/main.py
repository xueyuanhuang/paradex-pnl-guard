import argparse
import time
import logging
import signal
import sys
import os
from dotenv import load_dotenv
from paradex_py.common.order import OrderSide

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("GridMonitor")

BTC_MARKET = "BTC-USD-PERP"
ETH_MARKET = "ETH-USD-PERP"
LEVEL_ORDER = ["L1", "L2", "L3"]
STABLECOIN_TOKENS = {"USDC", "USDT", "DAI", "USDP", "TUSD"}


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def compute_thresholds(asset_size):
    """Scale thresholds linearly with asset size. Base: $1000."""
    s = asset_size / 1000
    return {
        "L1_TP":    17 * s,
        "L2_open": -17 * s,
        "L2_close": 32 * s,
        "L3_open": -68 * s,
        "L3_close": 33 * s,
        "warning": -170 * s,
    }


def detect_grid_level(positions, asset_size):
    """Auto-detect grid level and direction from actual BTC position size."""
    btc_pos = next((p for p in positions if p.get("market") == BTC_MARKET), None)
    if not btc_pos:
        return "FLAT", 0

    direction = 1 if btc_pos.get("side") == "LONG" else -1
    notional = abs(float(btc_pos.get("cost", 0) or 0))

    # Boundaries: midpoints between expected BTC notionals
    # L1 = 1×asset, L1+L2 = 3×asset, L1+L2+L3 = 6×asset
    if notional < asset_size * 0.5:
        return "FLAT", 0
    elif notional < asset_size * 2.0:
        return "L1", direction
    elif notional < asset_size * 4.5:
        return "L1_L2", direction
    else:
        return "L1_L2_L3", direction


def position_integrity_issue(positions, asset_size):
    pos_by_market = position_map(positions)
    btc_pos = pos_by_market.get(BTC_MARKET)
    eth_pos = pos_by_market.get(ETH_MARKET)

    if not btc_pos and not eth_pos:
        return None
    if not btc_pos or not eth_pos:
        missing = "BTC" if not btc_pos else "ETH"
        return f"{missing} leg is missing while the other leg is open"

    btc_side = btc_pos.get("side")
    eth_side = eth_pos.get("side")
    if not ((btc_side == "LONG" and eth_side == "SHORT") or (btc_side == "SHORT" and eth_side == "LONG")):
        return f"BTC/ETH sides are not paired: BTC {btc_side}, ETH {eth_side}"

    btc_notional = abs(_to_float(btc_pos.get("cost")))
    eth_notional = abs(_to_float(eth_pos.get("cost")))
    if btc_notional <= 0 or eth_notional <= 0:
        return f"BTC/ETH notional is invalid: BTC {btc_notional:.2f}, ETH {eth_notional:.2f}"

    eth_to_btc = eth_notional / btc_notional
    if eth_to_btc < 0.30 or eth_to_btc > 1.25:
        return f"BTC/ETH notional ratio is abnormal: ETH/BTC {eth_to_btc:.2f}"

    return None


def opening_side_for(market, direction):
    if market == BTC_MARKET:
        return "BUY" if direction == 1 else "SELL"
    if market == ETH_MARKET:
        return "SELL" if direction == 1 else "BUY"
    return None


def position_map(positions):
    return {p.get("market"): p for p in positions}


def current_round_start_at(positions):
    created = [
        _to_int(p.get("created_at"))
        for p in positions
        if p.get("market") in (BTC_MARKET, ETH_MARKET) and p.get("created_at")
    ]
    if not created:
        return None
    return max(0, min(created) - 120_000)


def build_lot_snapshot(client, positions, level_state, direction):
    """Return per-level BTC/ETH sizes, preferring actual fills over estimates."""
    fills = None
    start_at = current_round_start_at(positions)
    if start_at is not None:
        fills = client.get_fills(start_at=start_at)

    lots = reconstruct_lots_from_fills(positions, fills or [], direction)
    if has_lot_for_level(lots, close_level_for_state(level_state)):
        return {"source": "fills", "lots": lots}

    return {"source": "estimated", "lots": estimate_lots_from_positions(positions, level_state)}


def reconstruct_lots_from_fills(positions, fills, direction):
    pos_by_market = position_map(positions)
    groups_by_market = {BTC_MARKET: [], ETH_MARKET: []}

    for market in (BTC_MARKET, ETH_MARKET):
        position_created_at = _to_int(pos_by_market.get(market, {}).get("created_at"))
        open_side = opening_side_for(market, direction)
        by_order = {}

        for fill in sorted(fills, key=lambda f: _to_int(f.get("created_at"))):
            if fill.get("market") != market:
                continue
            if fill.get("side") != open_side:
                continue
            # Paradex position.created_at can be a few milliseconds after the fill.
            if position_created_at and _to_int(fill.get("created_at")) < position_created_at - 5000:
                continue

            order_id = fill.get("order_id") or f"{fill.get('created_at')}-{len(by_order)}"
            group = by_order.setdefault(order_id, {
                "market": market,
                "size": 0.0,
                "notional": 0.0,
                "first_created_at": _to_int(fill.get("created_at")),
            })
            size = abs(_to_float(fill.get("size")))
            price = _to_float(fill.get("price"))
            group["size"] += size
            group["notional"] += size * price
            group["first_created_at"] = min(group["first_created_at"], _to_int(fill.get("created_at")))

        groups_by_market[market] = sorted(by_order.values(), key=lambda g: g["first_created_at"])

    lots = {}
    for idx, level in enumerate(LEVEL_ORDER):
        level_lot = {}
        for market in (BTC_MARKET, ETH_MARKET):
            groups = groups_by_market[market]
            if idx < len(groups):
                group = groups[idx]
                avg_entry = group["notional"] / group["size"] if group["size"] else 0.0
                level_lot[market] = {
                    "size": group["size"],
                    "entry": avg_entry,
                    "notional": group["notional"],
                }
        if level_lot:
            lots[level] = level_lot

    return lots


def has_lot_for_level(lots, level):
    if not level:
        return False
    lot = lots.get(level, {})
    return bool(lot.get(BTC_MARKET, {}).get("size") and lot.get(ETH_MARKET, {}).get("size"))


def close_level_for_state(level_state):
    return {
        "L1": "L1",
        "L1_L2": "L2",
        "L1_L2_L3": "L3",
    }.get(level_state)


def estimate_lots_from_positions(positions, level_state):
    fractions = {
        "L1": {"L1": 1.0},
        "L1_L2": {"L1": 1 / 3, "L2": 2 / 3},
        "L1_L2_L3": {"L1": 1 / 6, "L2": 2 / 6, "L3": 3 / 6},
    }.get(level_state, {})
    pos_by_market = position_map(positions)
    lots = {}

    for level, fraction in fractions.items():
        lots[level] = {}
        for market in (BTC_MARKET, ETH_MARKET):
            pos = pos_by_market.get(market, {})
            size = abs(_to_float(pos.get("size")))
            entry = _to_float(pos.get("average_entry_price"))
            lots[level][market] = {
                "size": size * fraction,
                "entry": entry,
                "notional": abs(_to_float(pos.get("cost"))) * fraction,
            }

    return lots


def format_size(value):
    return f"{value:.8f}".rstrip("0").rstrip(".")


def format_money(value):
    return f"${value:,.0f}"


def format_usd(value):
    return f"${value:,.2f}"


def market_asset(market):
    return "BTC" if market == BTC_MARKET else "ETH"


def signed_position_size(position):
    size = abs(_to_float(position.get("size")))
    if position.get("side") == "SHORT":
        size = -size
    return format_size(size)


def order_side_label(order):
    return "买入" if order.order_side == OrderSide.Buy else "卖出"


def order_expected_size(order):
    return _to_float(order.size)


def order_status_size(status):
    if not status:
        return 0.0, 0.0

    total_size = _to_float(status.get("size"))
    remaining = _to_float(status.get("remaining_size"))
    filled = _to_float(status.get("filled_size"), None)
    if filled is None:
        filled = max(0.0, total_size - remaining)
    return filled, remaining


def order_avg_price(status):
    if not status:
        return 0.0
    return _to_float(status.get("avg_fill_price")) or _to_float(status.get("price"))


def order_notional(status):
    filled, _ = order_status_size(status)
    return abs(filled * order_avg_price(status))


def order_tolerance(order):
    return 0.000005 if order.market == BTC_MARKET else 0.00005


def order_fully_filled(order, status):
    if not status:
        return False
    if order_terminal_failure(status):
        return False
    filled, remaining = order_status_size(status)
    expected = order_expected_size(order)
    tol = order_tolerance(order)
    return filled >= expected - tol and remaining <= tol


def order_terminal_failure(status):
    if not status:
        return False
    if status.get("cancel_reason"):
        return True
    status_text = str(status.get("status") or "").upper()
    failure_tokens = ("CANCEL", "REJECT", "EXPIRE", "FAIL")
    return any(token in status_text for token in failure_tokens)


def order_failure_reason(order, status):
    if not status:
        return "ORDER_HISTORY_NOT_FOUND"
    reason = status.get("cancel_reason")
    if reason:
        return reason
    if order_terminal_failure(status):
        return status.get("status") or "ORDER_FAILED"
    if not order_fully_filled(order, status):
        filled, remaining = order_status_size(status)
        return f"PARTIAL_OR_UNFILLED filled={format_size(filled)} remaining={format_size(remaining)}"
    return ""


def wait_for_order_statuses(client, orders, timeout=15.0, poll_interval=1.0):
    statuses = {}
    deadline = time.time() + timeout

    while time.time() <= deadline:
        all_found = True
        for order in orders:
            status = client.get_order_by_client_id(order.client_id)
            if status:
                statuses[order.client_id] = status
            else:
                all_found = False

        if all_found and all(order_fully_filled(order, statuses.get(order.client_id)) for order in orders):
            break
        if any(order_terminal_failure(statuses.get(order.client_id)) for order in orders):
            break
        time.sleep(poll_interval)

    return statuses


def simulated_order_statuses(orders):
    return {
        order.client_id: {
            "client_id": order.client_id,
            "market": order.market,
            "side": "BUY" if order.order_side == OrderSide.Buy else "SELL",
            "size": str(order.size),
            "remaining_size": "0",
            "avg_fill_price": "0",
            "cancel_reason": "",
            "status": "DRY_RUN",
        }
        for order in orders
    }


def summarize_orders(orders, statuses):
    lines = []
    for order in sorted(orders, key=lambda o: o.market):
        status = statuses.get(order.client_id)
        filled, _ = order_status_size(status)
        notional = order_notional(status)
        asset = market_asset(order.market)
        reason = order_failure_reason(order, status)
        base = (
            f"{asset}: {order_side_label(order)} {format_size(filled)} {asset}"
            f" | 名义价值 {format_usd(notional)}"
        )
        if reason:
            base += f" | 未完全成交，原因: {reason}"
        lines.append(base)
    return lines


def summarize_positions(positions):
    if not positions:
        return ["无持仓"]

    lines = []
    by_market = position_map(positions)
    for market in (BTC_MARKET, ETH_MARKET):
        pos = by_market.get(market)
        if not pos:
            continue
        pnl = _to_float(pos.get("unrealized_pnl"))
        side = pos.get("side", "UNKNOWN")
        asset = market_asset(market)
        emoji = "🟢" if pnl >= 0 else "🔴"
        lines.extend([
            f"{emoji} {market} ({side})",
            (
                f"  PnL: {pnl:+.2f} USDC"
                f" | Size: {signed_position_size(pos)} {asset}"
                f" | Notional: {format_usd(abs(_to_float(pos.get('cost'))))}"
            ),
        ])
    return lines or ["无持仓"]


def operation_kind(action_key):
    return "open" if action_key in {"L2_open", "L3_open", "L1_open"} else "close"


def operation_title(action_key, kind, success=True):
    label = "开仓" if kind == "open" else "平仓"
    if success:
        return f"✅ {label}完成：{action_key}"
    return f"⚠️ {label}异常暂停：{action_key}"


def scaled_open_notional(alert_type, asset_size):
    scale = asset_size / 1000
    if alert_type == "L2_open":
        return 2000 * scale, 1333 * scale
    if alert_type == "L3_open":
        return 3000 * scale, 2000 * scale
    if alert_type in ("flat", "L1_TP"):
        return 1000 * scale, 667 * scale
    return 0, 0


def build_open_details(alert_type, asset_size, state):
    btc_notional, eth_notional = scaled_open_notional(alert_type, asset_size)
    if not btc_notional:
        return ""

    if state.direction == 1:
        btc_side, eth_side = "多", "空"
    elif state.direction == -1:
        btc_side, eth_side = "空", "多"
    else:
        btc_side, eth_side = "多/空", "空/多"

    if alert_type == "L1_TP":
        btc_side, eth_side = ("空", "多") if state.direction == 1 else ("多", "空")

    return (
        "开仓名义价值:\n"
        f"BTC: {btc_side} {format_money(btc_notional)}\n"
        f"ETH: {eth_side} {format_money(eth_notional)}"
    )


def build_close_details(alert_type, lot_snapshot, state, asset_size):
    level = {
        "L1_TP": "L1",
        "L2_close": "L2",
        "L3_close": "L3",
    }.get(alert_type)
    if not level:
        return ""

    lot = lot_snapshot["lots"].get(level, {})
    btc_size = lot.get(BTC_MARKET, {}).get("size", 0.0)
    eth_size = lot.get(ETH_MARKET, {}).get("size", 0.0)
    source = "fills 反推" if lot_snapshot["source"] == "fills" else "按当前仓位比例估算"

    btc_action = "卖出" if state.direction == 1 else "买入"
    eth_action = "买入" if state.direction == 1 else "卖出"

    lines = [
        f"平仓数量（{source}）:",
        f"BTC: reduce-only {btc_action} {format_size(btc_size)} BTC",
        f"ETH: reduce-only {eth_action} {format_size(eth_size)} ETH",
    ]

    if alert_type == "L1_TP":
        lines.extend(["", build_open_details("L1_TP", asset_size, state)])

    return "\n".join(line for line in lines if line)


def should_send(state, alert_key, repeat_interval_seconds):
    return state.alert_due(alert_key, repeat_interval_seconds)


def send_and_mark(notifier, state, alert_key, total_pnl, positions, action_details):
    if notifier.send_grid_alert(alert_key, total_pnl, state, positions, action_details):
        state.mark_alert(alert_key)
        return True
    return False


def error_detail(exc):
    text = str(exc)
    response = getattr(exc, "response", None)
    if response is not None:
        body = getattr(response, "text", "")
        if body:
            if len(body) > 800:
                body = body[:800] + "..."
            text = f"{text}\nResponse: {body}"
    return text


def is_stablecoin_transfer(transfer):
    token = str(transfer.get("token", "")).upper()
    return token in STABLECOIN_TOKENS


def notify_stablecoin_transfers(client, notifier, state):
    transfers = client.get_transfers(page_size=20)
    if transfers is None:
        return

    stable_transfers = [t for t in transfers if is_stablecoin_transfer(t)]
    if not state.stablecoin_transfers_initialized:
        state.seed_stablecoin_transfers(stable_transfers)
        return

    for transfer in reversed(stable_transfers):
        if state.transfer_notice_due(transfer) and notifier.send_stablecoin_notice(transfer):
            state.mark_transfer_seen(transfer)


def mark_prices_from_positions(positions):
    marks = {}
    for p in positions:
        market = p.get("market")
        size = _to_float(p.get("size"))
        entry = _to_float(p.get("average_entry_price"))
        pnl = _to_float(p.get("unrealized_pnl"))
        if market in (BTC_MARKET, ETH_MARKET) and size:
            marks[market] = entry + pnl / size
    return marks


def bbo_total_pnl(positions, quotes):
    total = 0.0
    seen = 0

    for p in positions:
        market = p.get("market")
        if market not in (BTC_MARKET, ETH_MARKET):
            continue

        quote = quotes.get(market, {})
        side = p.get("side")
        size = abs(_to_float(p.get("size")))
        entry = _to_float(p.get("average_entry_price"))
        bid = _to_float(quote.get("bid"))
        ask = _to_float(quote.get("ask"))
        if not size or not entry or not bid or not ask:
            return None

        if side == "LONG":
            total += size * (bid - entry)
        elif side == "SHORT":
            total += size * (entry - ask)
        else:
            return None
        seen += 1

    return total if seen else None


def bbo_spread_pct(quote):
    bid = _to_float(quote.get("bid"))
    ask = _to_float(quote.get("ask"))
    if bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return (ask - bid) / mid


def bbo_diagnostics(positions, quotes):
    ws_total_pnl = bbo_total_pnl(positions, quotes)
    spreads = []
    for market in (BTC_MARKET, ETH_MARKET):
        quote = quotes.get(market, {})
        spread_pct = bbo_spread_pct(quote)
        if spread_pct is not None:
            spreads.append(f"{market.split('-')[0]} spread {spread_pct * 100:.3f}%")
    return ws_total_pnl, ", ".join(spreads)


def direction_label_for(direction):
    if direction == 1:
        return "LONG BTC / SHORT ETH"
    if direction == -1:
        return "SHORT BTC / LONG ETH"
    return "NONE"


def send_operation_notice(notifier, title, action_key, kind, total_pnl, direction_label, orders, statuses, positions, extra_lines=None):
    action_label = "开仓" if kind == "open" else "平仓"
    lines = [
        f"方向: {direction_label}",
        f"触发: REST PnL {total_pnl:+.2f} USDC",
        "",
        f"本次{action_label}:",
    ]
    if orders:
        lines.extend(summarize_orders(orders, statuses))
    else:
        lines.append("未提交订单")

    if extra_lines:
        lines.extend(["", "处理:", *extra_lines])

    lines.extend(["", f"{action_label}后剩余仓位:", *summarize_positions(positions)])
    return notifier.send_trade_notice(title, lines)


def sync_state_from_positions(state, positions, asset_size):
    detected_level, detected_dir = detect_grid_level(positions, asset_size)
    if detected_level != state.level_state:
        old = state.level_state
        state.transition_to(detected_level, detected_dir)
        logger.info(f"Level change: {old} -> {detected_level}")

    if detected_dir != 0 and detected_dir != state.direction:
        state.data["direction"] = detected_dir


def block_auto_action_if_system_not_ok(client, notifier, state, action_key, total_pnl, positions, direction_label=None):
    status = client.get_system_state()
    if status == "ok":
        return False

    kind = operation_kind(action_key)
    reason = f"Paradex SystemState is {status or 'unknown'}"
    state.halt(action_key, reason)
    send_operation_notice(
        notifier,
        operation_title(action_key, kind, success=False),
        action_key,
        kind,
        total_pnl,
        direction_label or state.direction_label,
        [],
        {},
        positions,
        [
            f"未提交订单，原因: {reason}",
            "机器人已暂停，等待人工确认。",
        ],
    )
    if action_key in state.data.get("alerts_sent", {}):
        state.mark_alert(action_key)
    return True


def halt_before_submit(notifier, state, action_key, kind, total_pnl, direction_label, positions, asset_size, detail):
    reason = f"{action_key} pre-submit check failed: {detail}"
    state.halt(action_key, reason)
    sync_state_from_positions(state, positions, asset_size)
    send_operation_notice(
        notifier,
        operation_title(action_key, kind, success=False),
        action_key,
        kind,
        total_pnl,
        direction_label,
        [],
        {},
        positions,
        [
            f"下单前检查失败: {detail}",
            "未提交订单。",
            "机器人已暂停，等待人工确认。",
        ],
    )
    if action_key in state.data.get("alerts_sent", {}):
        state.mark_alert(action_key)


def build_auto_orders(trader, action_key, lot_snapshot, positions, state, asset_size, price_marks=None):
    prefix = f"{action_key}-{int(time.time())}"
    marks = price_marks or mark_prices_from_positions(positions)

    if action_key == "L3_close":
        return trader.close_lot_orders("L3", lot_snapshot, state.direction, prefix)
    if action_key == "L2_close":
        return trader.close_lot_orders("L2", lot_snapshot, state.direction, prefix)
    if action_key == "L2_open":
        btc_notional, eth_notional = scaled_open_notional("L2_open", asset_size)
        return trader.open_notional_orders("L2", btc_notional, eth_notional, marks, state.direction, prefix)
    if action_key == "L3_open":
        btc_notional, eth_notional = scaled_open_notional("L3_open", asset_size)
        return trader.open_notional_orders("L3", btc_notional, eth_notional, marks, state.direction, prefix)
    if action_key == "L1_TP":
        return trader.close_lot_orders("L1", lot_snapshot, state.direction, prefix)

    raise RuntimeError(f"Unsupported auto action: {action_key}")


def flatten_all_positions(trader, client, reason):
    positions = client.get_open_positions() or []
    if not positions:
        return {
            "orders": [],
            "statuses": {},
            "positions": [],
            "lines": ["没有剩余仓位需要平。"],
        }

    orders = trader.close_position_orders(positions, f"halt-flatten-{int(time.time())}")
    if not orders:
        return {
            "orders": [],
            "statuses": {},
            "positions": positions,
            "lines": ["未找到可提交的 reduce-only 清仓订单，请人工检查。"],
        }

    lines = [f"触发清仓原因: {reason}", "已尝试 reduce-only 平掉所有剩余仓位。"]
    try:
        trader.submit_batch(orders)
    except Exception as e:
        detail = error_detail(e)
        logger.error(f"Flatten failed: {detail}")
        lines.append(f"清仓提交失败: {detail}")
        return {
            "orders": orders,
            "statuses": {},
            "positions": client.get_open_positions() or positions,
            "lines": lines,
        }

    statuses = simulated_order_statuses(orders) if trader.dry_run else wait_for_order_statuses(client, orders)
    if not all(order_fully_filled(order, statuses.get(order.client_id)) for order in orders):
        lines.append("清仓订单未完全成交，请立即人工检查。")

    time.sleep(2)
    return {
        "orders": orders,
        "statuses": statuses,
        "positions": client.get_open_positions() or [],
        "lines": lines,
    }


def submit_order_set_checked(trader, notifier, state, client, action_key, kind, total_pnl, orders, asset_size, direction_label):
    try:
        trader.submit_batch(orders)
    except Exception as e:
        detail = error_detail(e)
        logger.error(f"Auto execution failed for {action_key}: {detail}")
        cleanup = flatten_all_positions(trader, client, f"{action_key} submit failed")
        final_positions = cleanup["positions"]
        reason = f"{action_key} submit failed: {detail}"
        state.halt(action_key, reason)
        sync_state_from_positions(state, final_positions, asset_size)
        send_operation_notice(
            notifier,
            operation_title(action_key, kind, success=False),
            action_key,
            kind,
            total_pnl,
            direction_label,
            orders,
            {},
            final_positions,
            [f"提交失败: {detail}", *cleanup["lines"], "机器人已暂停，等待人工确认。"],
        )
        if action_key in state.data.get("alerts_sent", {}):
            state.mark_alert(action_key)
        return False, final_positions

    statuses = simulated_order_statuses(orders) if trader.dry_run else wait_for_order_statuses(client, orders)
    success = all(order_fully_filled(order, statuses.get(order.client_id)) for order in orders)
    if not success:
        reasons = [
            f"{market_asset(order.market)}: {order_failure_reason(order, statuses.get(order.client_id))}"
            for order in orders
            if not order_fully_filled(order, statuses.get(order.client_id))
        ]
        reason = "; ".join(reasons)
        cleanup = flatten_all_positions(trader, client, f"{action_key} leg verification failed")
        final_positions = cleanup["positions"]
        state.halt(action_key, reason)
        sync_state_from_positions(state, final_positions, asset_size)
        send_operation_notice(
            notifier,
            operation_title(action_key, kind, success=False),
            action_key,
            kind,
            total_pnl,
            direction_label,
            orders,
            statuses,
            final_positions,
            [f"成交校验失败: {reason}", *cleanup["lines"], "机器人已暂停，等待人工确认。"],
        )
        if action_key in state.data.get("alerts_sent", {}):
            state.mark_alert(action_key)
        return False, final_positions

    time.sleep(2)
    post_positions = client.get_open_positions() or []
    sync_state_from_positions(state, post_positions, asset_size)
    send_operation_notice(
        notifier,
        operation_title(action_key, kind, success=True),
        action_key,
        kind,
        total_pnl,
        direction_label,
        orders,
        statuses,
        post_positions,
    )
    if action_key in state.data.get("alerts_sent", {}):
        state.mark_alert(action_key)
    return True, post_positions


def submit_l1_take_profit(trader, notifier, state, client, total_pnl, positions, lot_snapshot, asset_size, price_marks=None):
    old_direction = state.direction
    prefix = f"L1_TP-{int(time.time())}"
    try:
        close_orders = trader.close_lot_orders("L1", lot_snapshot, old_direction, prefix)
    except Exception as e:
        detail = error_detail(e)
        logger.error(f"Cannot build L1_TP close orders: {detail}")
        halt_before_submit(
            notifier, state, "L1_TP", "close", total_pnl,
            direction_label_for(old_direction), positions, asset_size, detail
        )
        return True

    close_ok, close_positions = submit_order_set_checked(
        trader, notifier, state, client, "L1_TP", "close", total_pnl,
        close_orders, asset_size, direction_label_for(old_direction)
    )
    if not close_ok or state.is_halted:
        return True

    if not trader.dry_run and block_auto_action_if_system_not_ok(
        client, notifier, state, "L1_open", total_pnl, close_positions, direction_label_for(-old_direction)
    ):
        return True

    btc_notional, eth_notional = scaled_open_notional("L1_TP", asset_size)
    marks = price_marks or mark_prices_from_positions(positions)
    try:
        open_orders = trader.open_notional_orders("L1", btc_notional, eth_notional, marks, -old_direction, prefix)
    except Exception as e:
        detail = error_detail(e)
        logger.error(f"Cannot build L1 reverse-open orders: {detail}")
        halt_before_submit(
            notifier, state, "L1_open", "open", total_pnl,
            direction_label_for(-old_direction), close_positions, asset_size, detail
        )
        return True

    submit_order_set_checked(
        trader, notifier, state, client, "L1_open", "open", total_pnl,
        open_orders, asset_size, direction_label_for(-old_direction)
    )
    return True


def submit_auto_action(trader, notifier, state, action_key, total_pnl, positions, action_details, lot_snapshot, asset_size, client, price_marks=None):
    if action_key == "L1_TP":
        return submit_l1_take_profit(trader, notifier, state, client, total_pnl, positions, lot_snapshot, asset_size, price_marks)

    kind = operation_kind(action_key)
    try:
        orders = build_auto_orders(trader, action_key, lot_snapshot, positions, state, asset_size, price_marks)
    except Exception as e:
        detail = error_detail(e)
        logger.error(f"Cannot build auto orders for {action_key}: {detail}")
        halt_before_submit(
            notifier, state, action_key, kind, total_pnl,
            state.direction_label, positions, asset_size, detail
        )
        return True

    submit_order_set_checked(
        trader, notifier, state, client, action_key, kind, total_pnl,
        orders, asset_size, state.direction_label
    )
    return True


def act_or_alert(trader, notifier, state, client, action_key, total_pnl, positions, details, asset_size, repeat_interval, price_marks=None):
    if not should_send(state, action_key, repeat_interval):
        return False

    needs_lot = action_key in {"L1_TP", "L2_close", "L3_close"}
    lot_snapshot = build_lot_snapshot(client, positions, state.level_state, state.direction) if needs_lot else {
        "source": "not_required",
        "lots": {},
    }

    if trader.enabled:
        if not trader.dry_run and block_auto_action_if_system_not_ok(client, notifier, state, action_key, total_pnl, positions):
            return True
        return submit_auto_action(trader, notifier, state, action_key, total_pnl, positions, details, lot_snapshot, asset_size, client, price_marks)
    return send_and_mark(notifier, state, action_key, total_pnl, positions, details)


def signal_handler(sig, frame):
    logger.info("Shutting down...")
    sys.exit(0)


def cmd_monitor(args):
    from paradex import ParadexClient
    from notifier import TelegramNotifier
    from state import GridState
    from trader import AutoTrader
    from ws_feed import BboFeed

    signal.signal(signal.SIGINT, signal_handler)

    jwt = args.jwt or os.getenv('PARADEX_JWT')
    if not jwt:
        print("Error: JWT required (--jwt or PARADEX_JWT env var)")
        sys.exit(1)

    tg_token = os.getenv('TG_BOT_TOKEN')
    tg_chat = os.getenv('TG_CHAT_ID')
    if not tg_token or not tg_chat:
        print("Error: TG_BOT_TOKEN and TG_CHAT_ID required in .env")
        sys.exit(1)

    client = ParadexClient(jwt)
    notifier = TelegramNotifier(tg_token, tg_chat)
    state = GridState()
    trader = AutoTrader(logger)
    thresholds = compute_thresholds(args.asset)
    bbo_feed = None
    if args.ws_bbo:
        bbo_feed = BboFeed([BTC_MARKET, ETH_MARKET], logger=logger)
        bbo_feed.start()

    logger.info(f"Grid Monitor started | Asset: ${args.asset} | Interval: {args.interval}s")
    logger.info(f"Thresholds: {thresholds}")
    logger.info(f"Auto trade mode: {trader.mode_label()}")
    logger.info(f"WS BBO diagnostics: {'enabled' if bbo_feed else 'disabled'}")

    positions = None
    rest_total_pnl = 0.0
    last_rest_sync = 0.0
    last_log = 0.0
    last_state_save = 0.0
    system_status = None
    last_system_state_sync = 0.0

    while True:
        try:
            if positions is not None:
                time.sleep(args.interval)

            now = time.time()
            system_due = system_status is None or now - last_system_state_sync >= args.system_state_interval
            if system_due:
                fresh_system_status = client.get_system_state()
                if fresh_system_status:
                    if fresh_system_status != system_status:
                        logger.info(f"Paradex SystemState: {fresh_system_status}")
                    system_status = fresh_system_status
                last_system_state_sync = now

            rest_due = (
                positions is None
                or now - last_rest_sync >= args.interval
                or (state.pending_action and now - last_rest_sync >= args.pending_check_interval)
            )

            if rest_due:
                for cmd in notifier.poll_commands():
                    if cmd == "pnl":
                        pos = client.get_open_positions()
                        if pos is not None:
                            pnl = sum(float(p.get("unrealized_pnl", 0)) for p in pos)
                            notifier.send_pnl_report(pnl, pos, state)

                fresh_positions = client.get_open_positions()
                if fresh_positions is None:
                    if positions is None:
                        time.sleep(5)
                        continue
                else:
                    positions = fresh_positions
                    rest_total_pnl = sum(float(p.get("unrealized_pnl", 0)) for p in positions)
                    last_rest_sync = now

                    notify_stablecoin_transfers(client, notifier, state)

                    integrity_issue = position_integrity_issue(positions, args.asset)
                    if integrity_issue and not state.is_halted:
                        logger.error(f"Position integrity issue: {integrity_issue}")
                        if trader.enabled and not trader.dry_run:
                            cleanup = flatten_all_positions(trader, client, integrity_issue)
                            positions = cleanup["positions"]
                        state.halt("position_integrity", integrity_issue)
                        notifier.send_trade_notice(
                            operation_title("position_integrity", "close", success=False),
                            [
                                f"方向: {state.direction_label}",
                                f"触发: REST PnL {rest_total_pnl:+.2f} USDC",
                                "",
                                "本次平仓:",
                                "检测到 BTC/ETH 仓位不成对，已进入保护处理。",
                                "",
                                "处理:",
                                *(cleanup["lines"] if trader.enabled and not trader.dry_run else [integrity_issue]),
                                "机器人已暂停，等待人工确认。",
                                "",
                                "平仓后剩余仓位:",
                                *summarize_positions(positions),
                            ],
                        )
                        state.save()
                        continue

                    detected_level, detected_dir = detect_grid_level(positions, args.asset)
                    if detected_level != state.level_state:
                        old = state.level_state
                        state.transition_to(detected_level, detected_dir)
                        logger.info(f"Level change: {old} -> {detected_level}")

                    if detected_dir != 0 and detected_dir != state.direction:
                        state.data["direction"] = detected_dir

            if positions is None:
                continue

            quotes = bbo_feed.snapshot() if bbo_feed else {}
            ws_diag_pnl, ws_diag_spreads = (None, "")
            if bbo_feed and bbo_feed.fresh(args.ws_stale_after):
                ws_diag_pnl, ws_diag_spreads = bbo_diagnostics(positions, quotes)

            total_pnl = rest_total_pnl
            pnl_source = "REST"
            price_marks = None

            state.update_pnl(total_pnl)

            if now - last_log >= args.log_interval or rest_due:
                ws_diag = ""
                if ws_diag_pnl is not None:
                    ws_diag = f" | BBO diag: {ws_diag_pnl:+.2f}"
                    if ws_diag_spreads:
                        ws_diag += f" ({ws_diag_spreads})"
                logger.info(
                    f"PnL: {total_pnl:+.2f} ({pnl_source}) | "
                    f"REST: {rest_total_pnl:+.2f} | State: {state.level_state} | "
                    f"Dir: {state.direction_label} | System: {system_status or 'unknown'}"
                    f"{ws_diag}"
                )
                last_log = now

            if state.pending_action:
                if state.pending_confirmed(state.level_state, state.direction):
                    state.clear_auto_pending()
                    state.reset_alerts_for_current_level()
                    state.save()
                    last_state_save = now
                elif state.pending_stale(args.pending_timeout):
                    pending = state.pending_action
                    logger.warning(f"Clearing stale legacy pending action: {pending}")
                    state.clear_auto_pending()
                    state.save()
                    last_state_save = now
                    continue
                else:
                    if now - last_state_save >= args.state_save_interval:
                        state.save()
                        last_state_save = now
                    continue

            # --- Grid threshold checks ---
            if state.is_halted:
                if now - last_state_save >= args.state_save_interval:
                    state.save()
                    last_state_save = now
                continue

            ls = state.level_state
            acted = False

            if ls == "L1":
                if total_pnl >= thresholds["L1_TP"]:
                    lot_snapshot = build_lot_snapshot(client, positions, ls, state.direction)
                    details = build_close_details("L1_TP", lot_snapshot, state, args.asset)
                    acted = act_or_alert(
                        trader, notifier, state, client, "L1_TP", total_pnl, positions,
                        details, args.asset, args.repeat_interval, price_marks
                    )
                elif total_pnl <= thresholds["L2_open"]:
                    details = build_open_details("L2_open", args.asset, state)
                    acted = act_or_alert(
                        trader, notifier, state, client, "L2_open", total_pnl, positions,
                        details, args.asset, args.repeat_interval, price_marks
                    )

            elif ls == "L1_L2":
                if total_pnl >= thresholds["L2_close"]:
                    lot_snapshot = build_lot_snapshot(client, positions, ls, state.direction)
                    details = build_close_details("L2_close", lot_snapshot, state, args.asset)
                    acted = act_or_alert(
                        trader, notifier, state, client, "L2_close", total_pnl, positions,
                        details, args.asset, args.repeat_interval, price_marks
                    )
                elif total_pnl <= thresholds["L3_open"]:
                    details = build_open_details("L3_open", args.asset, state)
                    acted = act_or_alert(
                        trader, notifier, state, client, "L3_open", total_pnl, positions,
                        details, args.asset, args.repeat_interval, price_marks
                    )

            elif ls == "L1_L2_L3":
                if total_pnl >= thresholds["L3_close"]:
                    lot_snapshot = build_lot_snapshot(client, positions, ls, state.direction)
                    details = build_close_details("L3_close", lot_snapshot, state, args.asset)
                    acted = act_or_alert(
                        trader, notifier, state, client, "L3_close", total_pnl, positions,
                        details, args.asset, args.repeat_interval, price_marks
                    )
                elif total_pnl <= thresholds["warning"] and should_send(state, "warning", args.repeat_interval):
                    logger.warning(f"Deep loss warning threshold reached: {total_pnl:+.2f} USDC")
                    state.mark_alert("warning")
                    acted = True

            if acted or now - last_state_save >= args.state_save_interval:
                state.save()
                last_state_save = now

        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            time.sleep(5)


def cmd_status(args):
    from state import GridState
    state = GridState()
    d = state.data
    print(f"State:     {d['level_state']}")
    print(f"Direction: {state.direction_label}")
    print(f"Opened:    {d['opened_at'] or 'N/A'}")
    print(f"Last PnL:  {d['last_total_pnl']:+.2f}")
    print(f"Updated:   {d['last_update'] or 'N/A'}")
    print(f"Halted:    {state.is_halted}")
    if state.is_halted:
        print(f"Reason:    {state.halt_reason or 'unknown'}")
    print(f"Alerts:    {d['alerts_sent']}")


def cmd_reset_warning(args):
    from state import GridState
    state = GridState()
    state.reset_warning()
    print("Warning alert reset.")


def cmd_resume(args):
    from state import GridState
    state = GridState()
    state.clear_halt()
    state.clear_auto_pending()
    state.save()
    print("Halt cleared. Restart monitor only after positions are manually verified.")


def main():
    parser = argparse.ArgumentParser(description="Paradex Grid v2 Monitor")
    sub = parser.add_subparsers(dest="command")

    p_mon = sub.add_parser("monitor", help="Start monitoring loop")
    p_mon.add_argument("--jwt", help="Paradex JWT (or PARADEX_JWT env)")
    p_mon.add_argument("--interval", type=int, default=5, help="REST poll interval in seconds (default: 5)")
    p_mon.add_argument("--asset", type=float, default=1000, help="Asset size in USD for threshold scaling (default: 1000)")
    p_mon.add_argument("--repeat-interval", type=int, default=600, help="Repeat actionable alerts every N seconds until state changes (default: 600)")
    p_mon.add_argument("--pending-timeout", type=int, default=180, help="Seconds to wait for a submitted action to change state (default: 180)")
    p_mon.add_argument("--ws-bbo", dest="ws_bbo", action="store_true", default=True, help="Enable websocket BBO diagnostics in logs (default)")
    p_mon.add_argument("--no-ws-bbo", dest="ws_bbo", action="store_false", help="Disable websocket BBO diagnostics")
    p_mon.add_argument("--ws-stale-after", type=float, default=10.0, help="Hide BBO diagnostics if websocket is stale for N seconds (default: 10)")
    p_mon.add_argument("--system-state-interval", type=float, default=30.0, help="Seconds between Paradex system state checks (default: 30)")
    p_mon.add_argument("--pending-check-interval", type=float, default=5.0, help="REST position check interval while an action is pending (default: 5)")
    p_mon.add_argument("--log-interval", type=float, default=30.0, help="Log PnL at most every N seconds unless REST sync runs (default: 30)")
    p_mon.add_argument("--state-save-interval", type=float, default=30.0, help="Save state at least every N seconds (default: 30)")
    p_mon.set_defaults(func=cmd_monitor)

    p = sub.add_parser("status", help="Show current state")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("reset-warning", help="Reset warning alert flag")
    p.set_defaults(func=cmd_reset_warning)

    p = sub.add_parser("resume", help="Clear halted state after manual verification")
    p.set_defaults(func=cmd_resume)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
