import argparse
import os
import sys
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def parse_args():
    parser = argparse.ArgumentParser(description='Paradex Total Unrealized P&L Monitor')
    
    parser.add_argument('--jwt', type=str, help='Paradex JWT Token (overrides PARADEX_JWT env var)')
    parser.add_argument('--interval', type=int, default=60, help='Check interval in seconds (default: 60)')
    parser.add_argument('--upper', type=float, default=20.0, help='Upper P&L threshold (default: +20)')
    parser.add_argument('--lower', type=float, default=-20.0, help='Lower P&L threshold (default: -20)')
    parser.add_argument('--trade-reminder-interval', type=int, default=3600, help='Trade reminder interval in seconds (default: 3600, 0 to disable)')

    return parser.parse_args()

class Config:
    def __init__(self):
        args = parse_args()
        
        # JWT
        self.jwt = args.jwt or os.getenv('PARADEX_JWT')
        if not self.jwt:
            print("Error: JWT must be provided via --jwt or PARADEX_JWT env var.")
            sys.exit(1)
            
        # Telegram
        self.tg_bot_token = os.getenv('TG_BOT_TOKEN')
        self.tg_chat_id = os.getenv('TG_CHAT_ID')
        
        if not self.tg_bot_token or not self.tg_chat_id:
            print("Error: TG_BOT_TOKEN and TG_CHAT_ID must be set in .env file.")
            sys.exit(1)
            
        # Other settings
        self.interval = args.interval
        self.upper_threshold = args.upper
        self.lower_threshold = args.lower
        
        self.trade_reminder_interval = args.trade_reminder_interval
        if self.trade_reminder_interval < 0:
            print(f"Warning: trade-reminder-interval {self.trade_reminder_interval} is invalid. Resetting to 0 (disabled).")
            self.trade_reminder_interval = 0

    def __repr__(self):
        return (f"Config(interval={self.interval}, upper={self.upper_threshold}, "
                f"lower={self.lower_threshold}, "
                f"trade_reminder_interval={self.trade_reminder_interval})")

# Singleton instance
try:
    config = Config()
except SystemExit:
    # Allow importing without exiting if just checking syntax or during tests (though sys.exit above handles runtime)
    # This try-except is mainly for REPL or test runners that might import this module.
    # However, for the main script, we want it to exit.
    # We'll rely on the sys.exit(1) calls above.
    pass
