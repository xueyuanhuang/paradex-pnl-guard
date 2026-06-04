import requests
import logging
from datetime import datetime
from typing import List, Dict

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.last_update_id = 0

    def send_grid_alert(self, alert_type: str, total_pnl: float, state, positions: List[Dict], action_details: str = "") -> bool:
        """Send a grid-specific alert to Telegram."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dir_label = state.direction_label

        if state.direction == 1:
            btc_side, eth_side = "多", "空"
            rev_btc, rev_eth = "空", "多"
        else:
            btc_side, eth_side = "空", "多"
            rev_btc, rev_eth = "多", "空"

        templates = {
            "L1_TP": (
                f"🎯 *L1 止盈 + 反手*\n"
                f"📅 {timestamp}\n"
                f"💰 总 PnL: `{total_pnl:+.2f} USDC`\n\n"
                f"L1 浮盈达标，手动平 L1 + 反手开新 L1\n"
                f"新方向: {rev_btc} BTC / {rev_eth} ETH"
            ),
            "L2_open": (
                f"🟡 *加仓 L2*\n"
                f"📅 {timestamp}\n"
                f"💰 总 PnL: `{total_pnl:+.2f} USDC`\n\n"
                f"手动开 L2: {btc_side} BTC $2000 + {eth_side} ETH $1333\n"
                f"方向同 L1（{dir_label}）"
            ),
            "L2_close": (
                f"🟢 *平仓 L2*\n"
                f"📅 {timestamp}\n"
                f"💰 总 PnL: `{total_pnl:+.2f} USDC`\n\n"
                f"手动 reduce-only 平 L2\n"
                f"BTC $2000 + ETH $1333"
            ),
            "L3_open": (
                f"🔴 *加仓 L3*\n"
                f"📅 {timestamp}\n"
                f"💰 总 PnL: `{total_pnl:+.2f} USDC`\n\n"
                f"手动开 L3: {btc_side} BTC $3000 + {eth_side} ETH $2000\n"
                f"方向同 L1（{dir_label}）"
            ),
            "L3_close": (
                f"🟢 *平仓 L3*\n"
                f"📅 {timestamp}\n"
                f"💰 总 PnL: `{total_pnl:+.2f} USDC`\n\n"
                f"手动 reduce-only 平 L3\n"
                f"BTC $3000 + ETH $2000"
            ),
            "warning": (
                f"⚠️ *警戒 — 深度浮亏*\n"
                f"📅 {timestamp}\n"
                f"💰 总 PnL: `{total_pnl:+.2f} USDC`\n\n"
                f"合计浮亏达 $170，考虑手动减仓"
            ),
            "flat": (
                f"📭 *仓位已清空*\n"
                f"📅 {timestamp}\n\n"
                f"当前空仓，开新 L1:\n"
                f"多 BTC $1000 + 空 ETH $667\n"
                f"或\n"
                f"空 BTC $1000 + 多 ETH $667\n"
                f"（自行判断方向）"
            ),
        }

        header = templates.get(alert_type, "")
        if not header:
            return False

        lines = [header]
        if action_details:
            lines.extend(["", action_details])

        lines.extend(["", "*持仓明细:*"])
        for p in positions:
            market = p.get("market", "Unknown")
            pnl = float(p.get("unrealized_pnl", 0))
            side = p.get("side", "UNKNOWN")
            size = p.get("size", "0")
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            lines.append(f"{pnl_emoji} {market} ({side}) PnL: `{pnl:+.2f}` Size: {size}")

        sent = self._send_message("\n".join(lines))
        if sent:
            self._log_alert(alert_type, total_pnl)
        return sent

    def send_pnl_report(self, total_pnl: float, positions: List[Dict], state=None) -> bool:
        """Send a P&L report in response to /pnl command."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        emoji = "🟢" if total_pnl >= 0 else "🔴"

        lines = [
            f"📊 *Paradex P&L Report*",
            f"📅 Time: {timestamp}",
            f"{emoji} Total PnL: `{total_pnl:+.2f} USDC`",
        ]

        if state:
            level_display = state.level_state.replace("_", "+")
            lines.append(f"📐 Grid: {level_display} | {state.direction_label}")

        lines.append("")

        if not positions:
            lines.append("No open positions.")
        else:
            for p in positions:
                market = p.get("market", "Unknown")
                pnl = float(p.get("unrealized_pnl", 0))
                side = p.get("side", "UNKNOWN")
                size = p.get("size", "0")
                entry = p.get("avg_entry_price", "N/A")
                pnl_emoji = "🟢" if pnl >= 0 else "🔴"
                lines.append(
                    f"{pnl_emoji} *{market}* ({side})\n"
                    f"  PnL: `{pnl:+.2f}` | Size: {size} | Entry: {entry}"
                )

        return self._send_message("\n".join(lines))

    def poll_commands(self) -> List[str]:
        """Poll Telegram for /pnl commands."""
        commands = []
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
            params = {"offset": self.last_update_id + 1, "timeout": 0}
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            for update in data.get("result", []):
                self.last_update_id = update["update_id"]
                message = update.get("message", {})
                chat_id = str(message.get("chat", {}).get("id", ""))
                text = (message.get("text") or "").strip()

                if chat_id == self.chat_id and text.startswith("/pnl"):
                    commands.append("pnl")

        except Exception as e:
            logger.error(f"Failed to poll Telegram updates: {e}")

        return commands

    def _send_message(self, text: str, parse_mode: str = None) -> bool:
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": text
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            response = requests.post(self.api_url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("Telegram message sent.")
            return True
        except requests.exceptions.RequestException as e:
            error_details = ""
            if e.response is not None:
                error_details = f" Response: {e.response.text}"
            logger.error(f"Failed to send Telegram message: {e}.{error_details}")
            return False
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    def _log_alert(self, alert_type: str, total_pnl: float):
        """Append alert to alerts.log."""
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open("alerts.log", "a") as f:
                f.write(f"{timestamp} | {alert_type} | PnL: {total_pnl:+.2f}\n")
        except Exception as e:
            logger.error(f"Failed to write alerts.log: {e}")
