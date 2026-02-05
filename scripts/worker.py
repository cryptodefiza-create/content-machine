#!/usr/bin/env python3
"""
Combined worker: Telegram bot + scheduled scanner
Runs as single Railway service
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schedule

from scripts.cron_runner import ContentScanner
from src.bot import ContentBot
from src.utils import logger


def run_scheduler(scanner: ContentScanner):
    """Run scheduler in background thread"""
    logger.info("Scheduler (UTC): scans 06/10/13, summary+trends 14:30, expiry 22:00")
    schedule.every().day.at("06:00").do(scanner.run_scan)
    schedule.every().day.at("10:00").do(scanner.run_scan)
    schedule.every().day.at("13:00").do(scanner.run_scan)
    schedule.every().day.at("14:30").do(scanner.run_summary)
    schedule.every().day.at("22:00").do(scanner.run_expire)
    while True:
        schedule.run_pending()
        time.sleep(60)


def main():
    logger.info("Content Machine starting...")

    scanner = ContentScanner()

    try:
        scanner.run_scan()
    except Exception as e:
        logger.error(f"Initial scan failed: {e}")

    # Start scheduler in background
    scheduler_thread = threading.Thread(target=run_scheduler, args=(scanner,), daemon=True)
    scheduler_thread.start()
    logger.info("Scheduler running")

    # Start Telegram bot (blocking)
    logger.info("Starting Telegram bot...")
    bot = ContentBot()
    bot.run()


if __name__ == "__main__":
    main()
