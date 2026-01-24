#!/usr/bin/env python3
"""
Scheduled content scanning.

Runs scans at configured times and notifies via Telegram.

Usage:
    python scripts/cron_runner.py          # Run with scheduler
    python scripts/cron_runner.py --once   # Run once and exit
"""
import os
import sys
import time
import asyncio
import signal
import argparse
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schedule

from src.scanner import Scanner
from src.brain import Brain
from src.queue import QueueManager
from src.bot import send_notification
from src.utils import logger


class ContentScanner:
    """Scheduled content scanner with Telegram notifications."""

    def __init__(self):
        self.scanner = Scanner()
        self.brain = Brain()
        self.queue = QueueManager()
        self.running = True

    def shutdown(self, signum, frame):
        """Handle graceful shutdown."""
        logger.info("Shutting down scheduler...")
        self.running = False

    async def scan(self, max_items: int = 3):
        """Run a content scan and generate posts."""
        logger.info("Starting content scan...")

        try:
            # Fetch trending topics and news
            items = self.scanner.scan_all(max_items=max_items)
            logger.info(f"Found {len(items)} items to process")

            processed = 0
            skipped = 0

            for item in items:
                # Skip duplicates
                if self.queue.content_exists(item["content_hash"]):
                    skipped += 1
                    continue

                # Generate content with Gemini
                try:
                    content = self.brain.generate_content(item)
                    if content:
                        self.queue.add_content(content)
                        processed += 1
                        logger.info(f"Generated: {item['topic'][:50]}...")
                    else:
                        logger.warning(f"No content generated for: {item['topic'][:50]}")
                except Exception as e:
                    logger.error(f"Failed to process item: {e}")

                # Rate limiting between Gemini calls (free tier: 15 req/min)
                time.sleep(5)

            logger.info(f"Scan complete: {processed} new, {skipped} skipped")

            # Notify via Telegram
            if processed > 0:
                await send_notification(
                    f"📬 *{processed} new draft(s) ready!*\n\n"
                    f"Use /next to start reviewing"
                )

            return processed

        except Exception as e:
            logger.error(f"Scan failed: {e}")
            await send_notification(f"❌ *Scan failed*\n\n`{str(e)[:100]}`")
            return 0

    async def send_daily_summary(self):
        """Send daily summary of queue status."""
        try:
            stats = self.queue.get_stats()
            await send_notification(
                f"📊 *Daily Summary*\n\n"
                f"⏳ Pending: {stats['pending']}\n"
                f"✅ Approved: {stats['approved']}\n"
                f"📤 Posted: {stats['posted']}\n"
                f"❌ Rejected: {stats.get('rejected', 0)}"
            )
        except Exception as e:
            logger.error(f"Failed to send summary: {e}")

    async def expire_old_drafts(self):
        """Expire drafts older than 48 hours."""
        try:
            expired = self.queue.expire_old_pending(hours=48)
            if expired > 0:
                logger.info(f"Expired {expired} old drafts")
        except Exception as e:
            logger.error(f"Failed to expire drafts: {e}")

    def run_scan(self):
        """Wrapper for scheduled scan."""
        asyncio.run(self.scan())

    def run_summary(self):
        """Wrapper for scheduled summary."""
        asyncio.run(self.send_daily_summary())

    def run_expire(self):
        """Wrapper for scheduled expiry."""
        asyncio.run(self.expire_old_drafts())

    def run_once(self):
        """Run a single scan and exit."""
        logger.info("Running single scan...")
        asyncio.run(self.scan())
        logger.info("Done.")

    def run_scheduler(self):
        """Run the scheduled scanner."""
        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

        logger.info("Starting Content Machine scheduler...")

        # Schedule scans (3x daily)
        schedule.every().day.at("08:00").do(self.run_scan)
        schedule.every().day.at("14:00").do(self.run_scan)
        schedule.every().day.at("19:00").do(self.run_scan)

        # Schedule daily summary
        schedule.every().day.at("21:00").do(self.run_summary)

        # Schedule draft expiry check (once daily)
        schedule.every().day.at("00:00").do(self.run_expire)

        logger.info("Schedule:")
        logger.info("  Scans: 08:00, 14:00, 19:00")
        logger.info("  Summary: 21:00")
        logger.info("  Expiry check: 00:00")

        # Run initial scan
        logger.info("Running initial scan...")
        self.run_scan()

        # Main loop
        logger.info("Scheduler running. Press Ctrl+C to stop.")
        while self.running:
            schedule.run_pending()
            time.sleep(60)

        logger.info("Scheduler stopped.")


def main():
    parser = argparse.ArgumentParser(description="Content Machine Scheduler")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan and exit"
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Send daily summary and exit"
    )
    args = parser.parse_args()

    scanner = ContentScanner()

    if args.once:
        scanner.run_once()
    elif args.summary:
        asyncio.run(scanner.send_daily_summary())
    else:
        scanner.run_scheduler()


if __name__ == "__main__":
    main()
