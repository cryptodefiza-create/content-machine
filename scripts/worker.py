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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.scanner import Scanner
from src.brain import Brain
from src.queue import QueueManager
from src.bot import ContentBot, notify

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
log = logging.getLogger(__name__)

scanner = Scanner()
brain = Brain()
queue = QueueManager()


def run_scan():
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
            time.sleep(2)
        log.info(f"Scan complete: {processed} new drafts")
        if processed > 0:
            asyncio.run(notify(f"*{processed} new drafts ready!*\n\nUse /next to review"))
    except Exception as e:
        log.error(f"Scan failed: {e}")


def run_scheduler():
    """Run scheduler in background thread"""
    log.info("Scheduler: 08:00, 14:00, 19:00")
    schedule.every().day.at("08:00").do(run_scan)
    schedule.every().day.at("14:00").do(run_scan)
    schedule.every().day.at("19:00").do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(60)


def main():
    log.info("Content Machine starting...")

    # Initial scan
    run_scan()

    # Start scheduler in background
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    log.info("Scheduler running")

    # Start Telegram bot (blocking)
    log.info("Starting Telegram bot...")
    bot = ContentBot()
    bot.run()


if __name__ == "__main__":
    main()
