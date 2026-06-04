import argparse
import time
import logging
import signal
import sys
import os
from dotenv import load_dotenv

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


def signal_handler(sig, frame):
    logger.info("Shutting down...")
    sys.exit(0)


def cmd_monitor(args):
    from paradex import ParadexClient
    from notifier import TelegramNotifier
    from state import GridState

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
    thresholds = compute_thresholds(args.asset)

    logger.info(f"Grid Monitor started | Asset: ${args.asset} | Interval: {args.interval}s")
    logger.info(f"Thresholds: {thresholds}")

    while True:
        try:
            start = time.time()

            # Poll Telegram /pnl commands
            for cmd in notifier.poll_commands():
                if cmd == "pnl":
                    pos = client.get_open_positions()
                    if pos is not None:
                        pnl = sum(float(p.get("unrealized_pnl", 0)) for p in pos)
                        notifier.send_pnl_report(pnl, pos, state)

            # Fetch positions
            positions = client.get_open_positions()
            if positions is None:
                time.sleep(5)
                continue

            total_pnl = sum(float(p.get("unrealized_pnl", 0)) for p in positions)

            # Auto-detect level from actual positions
            detected_level, detected_dir = detect_grid_level(positions, args.asset)

            if detected_level != state.level_state:
                old = state.level_state
                state.transition_to(detected_level, detected_dir)
                logger.info(f"Level change: {old} -> {detected_level}")
                if detected_level == "FLAT" and old != "FLAT":
                    notifier.send_grid_alert("flat", 0, state, [], build_open_details("flat", args.asset, state))

            if detected_dir != 0 and detected_dir != state.direction:
                state.data["direction"] = detected_dir

            state.update_pnl(total_pnl)

            logger.info(f"PnL: {total_pnl:+.2f} | State: {state.level_state} | Dir: {state.direction_label}")

            # --- Grid threshold checks ---
            ls = state.level_state

            if ls == "L1":
                if total_pnl >= thresholds["L1_TP"] and should_send(state, "L1_TP", args.repeat_interval):
                    lot_snapshot = build_lot_snapshot(client, positions, ls, state.direction)
                    details = build_close_details("L1_TP", lot_snapshot, state, args.asset)
                    send_and_mark(notifier, state, "L1_TP", total_pnl, positions, details)
                if total_pnl <= thresholds["L2_open"] and should_send(state, "L2_open", args.repeat_interval):
                    details = build_open_details("L2_open", args.asset, state)
                    send_and_mark(notifier, state, "L2_open", total_pnl, positions, details)

            elif ls == "L1_L2":
                if total_pnl >= thresholds["L2_close"] and should_send(state, "L2_close", args.repeat_interval):
                    lot_snapshot = build_lot_snapshot(client, positions, ls, state.direction)
                    details = build_close_details("L2_close", lot_snapshot, state, args.asset)
                    send_and_mark(notifier, state, "L2_close", total_pnl, positions, details)
                if total_pnl <= thresholds["L3_open"] and should_send(state, "L3_open", args.repeat_interval):
                    details = build_open_details("L3_open", args.asset, state)
                    send_and_mark(notifier, state, "L3_open", total_pnl, positions, details)

            elif ls == "L1_L2_L3":
                if total_pnl >= thresholds["L3_close"] and should_send(state, "L3_close", args.repeat_interval):
                    lot_snapshot = build_lot_snapshot(client, positions, ls, state.direction)
                    details = build_close_details("L3_close", lot_snapshot, state, args.asset)
                    send_and_mark(notifier, state, "L3_close", total_pnl, positions, details)
                if total_pnl <= thresholds["warning"] and should_send(state, "warning", args.repeat_interval):
                    send_and_mark(notifier, state, "warning", total_pnl, positions, "")

            state.save()

            elapsed = time.time() - start
            time.sleep(max(0, args.interval - elapsed))

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
    print(f"Alerts:    {d['alerts_sent']}")


def cmd_reset_warning(args):
    from state import GridState
    state = GridState()
    state.reset_warning()
    print("Warning alert reset.")


def main():
    parser = argparse.ArgumentParser(description="Paradex Grid v2 Monitor")
    sub = parser.add_subparsers(dest="command")

    p_mon = sub.add_parser("monitor", help="Start monitoring loop")
    p_mon.add_argument("--jwt", help="Paradex JWT (or PARADEX_JWT env)")
    p_mon.add_argument("--interval", type=int, default=30, help="Poll interval in seconds (default: 30)")
    p_mon.add_argument("--asset", type=float, default=1000, help="Asset size in USD for threshold scaling (default: 1000)")
    p_mon.add_argument("--repeat-interval", type=int, default=600, help="Repeat actionable alerts every N seconds until state changes (default: 600)")
    p_mon.set_defaults(func=cmd_monitor)

    p = sub.add_parser("status", help="Show current state")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("reset-warning", help="Reset warning alert flag")
    p.set_defaults(func=cmd_reset_warning)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
