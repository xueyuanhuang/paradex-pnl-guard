import logging
import os
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List

from paradex_py import ParadexSubkey
from paradex_py.common.order import Order, OrderSide, OrderType

BTC_MARKET = "BTC-USD-PERP"
ETH_MARKET = "ETH-USD-PERP"
SIZE_INCREMENT = {
    BTC_MARKET: Decimal("0.00001"),
    ETH_MARKET: Decimal("0.0001"),
}


class AutoTrader:
    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger(__name__)
        self.enabled = _env_bool("AUTO_TRADE_ENABLED", False)
        self.dry_run = _env_bool("AUTO_TRADE_DRY_RUN", True)
        self.env = os.getenv("PARADEX_ENV", "prod")
        self.account_address = os.getenv("PARADEX_ACCOUNT_ADDRESS", "")
        self.private_key = os.getenv("PARADEX_SUBKEY_PRIVATE_KEY", "")
        self.client = None

        if self.enabled and not self.dry_run:
            if not self.account_address or not self.private_key:
                raise RuntimeError("AUTO_TRADE_ENABLED requires PARADEX_ACCOUNT_ADDRESS and PARADEX_SUBKEY_PRIVATE_KEY")
            sdk_logger = logging.getLogger("ParadexSDK")
            sdk_logger.setLevel(logging.WARNING)
            self.client = ParadexSubkey(
                env=self.env,
                l2_private_key=self.private_key,
                l2_address=self.account_address,
                logger=sdk_logger,
                ws_enabled=False,
            )

    def mode_label(self):
        if not self.enabled:
            return "disabled"
        return "dry-run" if self.dry_run else "live"

    def close_lot_orders(self, level, lot_snapshot, direction, client_prefix):
        lot = lot_snapshot["lots"].get(level, {})
        btc_size = lot.get(BTC_MARKET, {}).get("size", 0)
        eth_size = lot.get(ETH_MARKET, {}).get("size", 0)

        if direction == 1:
            btc_side, eth_side = OrderSide.Sell, OrderSide.Buy
        else:
            btc_side, eth_side = OrderSide.Buy, OrderSide.Sell

        return [
            self._market_order(BTC_MARKET, btc_side, btc_size, f"{client_prefix}-btc-close", reduce_only=True),
            self._market_order(ETH_MARKET, eth_side, eth_size, f"{client_prefix}-eth-close", reduce_only=True),
        ]

    def open_notional_orders(self, level, btc_notional, eth_notional, marks, direction, client_prefix):
        if BTC_MARKET not in marks or ETH_MARKET not in marks:
            raise RuntimeError("Cannot open by notional without BTC and ETH mark prices")

        btc_size = btc_notional / marks[BTC_MARKET]
        eth_size = eth_notional / marks[ETH_MARKET]

        if direction == 1:
            btc_side, eth_side = OrderSide.Buy, OrderSide.Sell
        else:
            btc_side, eth_side = OrderSide.Sell, OrderSide.Buy

        return [
            self._market_order(BTC_MARKET, btc_side, btc_size, f"{client_prefix}-btc-open-{level}", reduce_only=False),
            self._market_order(ETH_MARKET, eth_side, eth_size, f"{client_prefix}-eth-open-{level}", reduce_only=False),
        ]

    def submit_batch(self, orders: List[Order]) -> Dict:
        if self.dry_run:
            return {
                "dry_run": True,
                "orders": [order.dump_to_dict() for order in orders],
            }

        if self.client is None:
            raise RuntimeError("AutoTrader is not initialized")

        payloads = []
        for order in orders:
            order.signature = self.client.account.sign_order(order)
            payloads.append(order.dump_to_dict())

        return self.client.api_client._post_authorized(path="orders/batch", payload=payloads)

    def describe_orders(self, orders: List[Order]):
        lines = []
        for order in orders:
            action = "reduce-only " if order.reduce_only else ""
            side = "买入" if order.order_side == OrderSide.Buy else "卖出"
            lines.append(f"{order.market}: {action}{side} {format_decimal(order.size)}")
        return "\n".join(lines)

    def _market_order(self, market, side, size, client_id, reduce_only):
        dec_size = quantize_size(size, market)
        if dec_size <= 0:
            raise RuntimeError(f"Invalid order size for {market}: {size}")
        return Order(
            market=market,
            order_type=OrderType.Market,
            order_side=side,
            size=dec_size,
            client_id=client_id[:64],
            instruction="IOC",
            reduce_only=reduce_only,
        )


def quantize_size(value, market):
    increment = SIZE_INCREMENT.get(market, Decimal("0.00000001"))
    dec_value = Decimal(str(value))
    units = (dec_value / increment).to_integral_value(rounding=ROUND_DOWN)
    return units * increment


def format_decimal(value):
    return f"{Decimal(value):f}".rstrip("0").rstrip(".")


def _env_bool(key, default):
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}
