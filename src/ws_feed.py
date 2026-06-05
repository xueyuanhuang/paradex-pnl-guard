import json
import logging
import threading
import time
from typing import Dict, Iterable

import websocket


DEFAULT_WS_URL = "wss://ws.api.prod.paradex.trade/v1"


class BboFeed:
    def __init__(self, markets: Iterable[str], url: str = DEFAULT_WS_URL, logger: logging.Logger | None = None):
        self.markets = list(markets)
        self.url = url
        self.logger = logger or logging.getLogger(__name__)
        self._quotes: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._updated = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._ws = None
        self.connected = False
        self.last_message_at = 0.0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="ParadexBboFeed", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    def wait_for_update(self, timeout):
        updated = self._updated.wait(timeout)
        if updated:
            self._updated.clear()
        return updated

    def snapshot(self):
        with self._lock:
            return {market: dict(quote) for market, quote in self._quotes.items()}

    def ready(self):
        with self._lock:
            return all(market in self._quotes for market in self.markets)

    def fresh(self, max_age_seconds):
        now = time.time()
        with self._lock:
            for market in self.markets:
                quote = self._quotes.get(market)
                if not quote:
                    return False
                if now - quote.get("received_at", 0) > max_age_seconds:
                    return False
        return True

    def stale(self, max_age_seconds):
        if not self.last_message_at:
            return True
        return time.time() - self.last_message_at > max_age_seconds

    def _run(self):
        while not self._stop.is_set():
            try:
                self._connect_once()
            except Exception as exc:
                self.logger.error(f"BBO websocket error: {exc}", exc_info=True)

            self.connected = False
            self._ws = None
            if not self._stop.is_set():
                time.sleep(3)

    def _connect_once(self):
        def on_open(ws):
            self.connected = True
            self.logger.info("BBO websocket connected.")
            for idx, market in enumerate(self.markets, start=1):
                ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "method": "subscribe",
                    "params": {"channel": f"bbo.{market}"},
                    "id": idx,
                }))

        def on_message(ws, message):
            self._handle_message(message)

        def on_error(ws, error):
            self.logger.error(f"BBO websocket callback error: {error}")

        def on_close(ws, status_code, message):
            self.connected = False
            self.logger.warning(f"BBO websocket closed: {status_code} {message}")

        self._ws = websocket.WebSocketApp(
            self.url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        self._ws.run_forever(ping_interval=30, ping_timeout=10)

    def _handle_message(self, message):
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return

        params = payload.get("params") or {}
        channel = params.get("channel", "")
        if not channel.startswith("bbo."):
            return

        data = params.get("data") or {}
        market = data.get("market")
        if market not in self.markets:
            return

        bid = _to_float(data.get("bid"))
        ask = _to_float(data.get("ask"))
        if bid <= 0 or ask <= 0:
            return

        quote = {
            "bid": bid,
            "ask": ask,
            "bid_size": _to_float(data.get("bid_size")),
            "ask_size": _to_float(data.get("ask_size")),
            "last_updated_at": data.get("last_updated_at"),
            "received_at": time.time(),
        }
        with self._lock:
            self._quotes[market] = quote
            self.last_message_at = quote["received_at"]
        self._updated.set()


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
