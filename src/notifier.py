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

    def send_alert(self, total_pnl: float, threshold: float, positions: List[Dict]):
        """
        Sends an alert to Telegram with P&L details.
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Determine emoji based on P&L
        emoji = "🟢" if total_pnl >= 0 else "🔴"
        
        message_lines = [
            f"{emoji} **Paradex P&L Alert**",
            f"📅 Time: {timestamp}",
            f"💰 Total Unrealized P&L: `{total_pnl:+.2f} USDC`",
            f"⚠️ Threshold Triggered: `{threshold}`",
            "",
            "**Market Details:**"
        ]
        
        for p in positions:
            market = p.get("market", "Unknown")
            pnl = float(p.get("unrealized_pnl", 0))
            side = p.get("side", "UNKNOWN")
            size = p.get("size", "0")
            liq = p.get("liquidation_price", "N/A")
            
            # Format P&L for individual positions
            pnl_str = f"{pnl:+.2f}"
            
            line = (
                f"- **{market}** ({side})\n"
                f"  P&L: `{pnl_str}`\n"
                f"  Size: {size} | Liq: {liq}"
            )
            message_lines.append(line)
            
        message = "\n".join(message_lines)
        
        self._send_message(message)

    def send_trade_reminder(self):
        """
        Sends a trade reminder.
        """
        message = (
            "🕐 Hourly trade reminder: place at least 1 trade.\n"
            "If you already traded, ignore this. If you didn’t, stop pretending you’re waiting for confirmation."
        )
        self._send_message(message)
        logger.info("Hourly trade reminder sent.")

    def _send_message(self, text: str):
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }
            response = requests.post(self.api_url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("Telegram notification sent successfully.")
        except requests.exceptions.RequestException as e:
            error_details = ""
            if e.response is not None:
                error_details = f" Response: {e.response.text}"
            logger.error(f"Failed to send Telegram notification: {e}.{error_details}")
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")
