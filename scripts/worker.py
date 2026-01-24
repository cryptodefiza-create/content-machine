#!/usr/bin/env python3
"""
Combined worker: Telegram bot + scheduled scanner
Runs as single Railway service
"""
import os
import sys
import time
import asyncio
import threading
import schedule
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.scanner import Scanner
from src.brain import Brain
from src.queue import QueueManager
from src.bot import ContentBot, notify

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# Shared instances
scanner = Scanner()
brain = Brain()
queue = QueueManager()


async def run_scan():
    """Scan for content and generate drafts"""
    log.info("Starting scan...")

    try:
        items = scanner.scan_all(max_items=6)
        processed = 0

        for item in items:
            if queue.content_exists(item["content_hash"]):
                continue

            content = brain.generate_content(item)
            if content:
                queue.add_content(content)
                processed += 1
                log.info(f"   Generated: {item['topic'][:40]}...")

            time.sleep(2)  # Rate limiting

        log.info(f"Scan complete: {processed} new drafts")

        if processed > 0:
            await notify(f"*{processed} new drafts ready!*\n\nUse /next to review")

    except Exception as e:
        log.error(f"Scan failed: {e}")


async def daily_summary():
    """Send daily stats"""
    stats = queue.get_stats()
    await notify(
        f"*Daily Summary*\n\n"
        f"Pending: {stats['pending']}\n"
        f"Approved: {stats['approved']}\n"
        f"Posted: {stats['posted']}"
    )


def sync_scan():
    """Sync wrapper for async scan"""
    asyncio.run(run_scan())


def sync_summary():
    """Sync wrapper for async summary"""
    asyncio.run(daily_summary())


def run_scheduler():
    """Run the scheduler in a separate thread"""
    log.info("Scheduler started")
    log.info("   Scans: 08:00, 14:00, 19:00")
    log.info("   Summary: 21:00")

    schedule.every().day.at("08:00").do(sync_scan)
    schedule.every().day.at("14:00").do(sync_scan)
    schedule.every().day.at("19:00").do(sync_scan)
    schedule.every().day.at("21:00").do(sync_summary)

    while True:
        schedule.run_pending()
        time.sleep(60)


def main():
    log.info("Content Machine starting...")
    log.info("=" * 40)

    # Run initial scan on startup
    log.info("Running initial scan...")
    sync_scan()

    # Start scheduler in background thread
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    log.info("Scheduler running in background")

    # Start Telegram bot in main thread (blocking)
    log.info("Starting Telegram bot...")
    bot = ContentBot()
    bot.run()  # This blocks and handles Telegram updates


if __name__ == "__main__":
    main()
