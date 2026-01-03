import time
import logging
import signal
import sys
from config import config
from paradex import ParadexClient
from notifier import TelegramNotifier

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("ParadexGuard")

def signal_handler(sig, frame):
    logger.info("Gracefully shutting down...")
    sys.exit(0)

def main():
    # Register signal handler for Ctrl+C
    signal.signal(signal.SIGINT, signal_handler)
    
    logger.info("Starting Paradex P&L Monitor...")
    logger.info(f"Configuration: {config}")
    
    client = ParadexClient(config.jwt)
    notifier = TelegramNotifier(config.tg_bot_token, config.tg_chat_id)
    
    # Initialize trade reminder time
    next_trade_reminder_at = None
    if config.trade_reminder_interval > 0:
        next_trade_reminder_at = time.time() + config.trade_reminder_interval
        logger.info(f"Trade reminder enabled. Interval: {config.trade_reminder_interval}s. Next reminder at: {time.ctime(next_trade_reminder_at)}")
    else:
        logger.info("Trade reminder disabled (interval=0).")
    
    while True:
        try:
            start_time = time.time()
            
            # --- 0. Trade Reminder Check ---
            if next_trade_reminder_at and start_time >= next_trade_reminder_at:
                try:
                    notifier.send_trade_reminder()
                except Exception as e:
                    logger.error(f"Failed to send trade reminder: {e}")
                
                # Advance next reminder time
                while next_trade_reminder_at <= start_time:
                    next_trade_reminder_at += config.trade_reminder_interval
                logger.debug(f"Next trade reminder at: {time.ctime(next_trade_reminder_at)}")

            # 1. Fetch positions
            positions = client.get_open_positions()
            
            if positions is not None:
                # 2. Calculate Total Unrealized P&L
                total_pnl = sum(float(p.get("unrealized_pnl", 0)) for p in positions)
                
                logger.info(f"Current Total Unrealized P&L: {total_pnl:.2f} USDC (Markets: {len(positions)})")
                
                # 3. Check thresholds
                threshold_triggered = None
                if total_pnl >= config.upper_threshold:
                    threshold_triggered = config.upper_threshold
                    logger.info(f"Upper threshold triggered: {total_pnl:.2f} >= {config.upper_threshold}")
                elif total_pnl <= config.lower_threshold:
                    threshold_triggered = config.lower_threshold
                    logger.info(f"Lower threshold triggered: {total_pnl:.2f} <= {config.lower_threshold}")
                
                # 4. Send Alert
                if threshold_triggered is not None:
                    notifier.send_alert(total_pnl, threshold_triggered, positions)
            
            # 6. Sleep
            elapsed = time.time() - start_time
            sleep_time = max(0, config.interval - elapsed)
            logger.debug(f"Sleeping for {sleep_time:.2f}s")
            time.sleep(sleep_time)
            
        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
            # Sleep a bit to avoid rapid error looping if something is persistently wrong
            time.sleep(5)

if __name__ == "__main__":
    main()
