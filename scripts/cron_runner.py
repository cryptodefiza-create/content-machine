#!/usr/bin/env python3
"""Scheduled content scanning with Telegram notifications."""
import os
import sys
import time
import asyncio
import signal
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schedule

from src.scanner import Scanner
from src.pipeline import ContentPipeline
from src.runtime_config import get_dry_run
from src.settings import load_settings
from src.queue import QueueManager
from src.bot import send_notification
from src.utils import logger, truncate


class ContentScanner:

    def __init__(self):
        self.scanner = Scanner()
        self.pipeline = ContentPipeline(settings=load_settings())
        self.queue = QueueManager()
        self.running = True

        if self.scanner.sources.get("_fallback", False):
            try:
                asyncio.run(send_notification(
                    "⚠️ *Scanner fallback in use*\n\n"
                    "sources.json missing/invalid. Using minimal defaults."
                ))
            except Exception as e:
                logger.error(f"Failed to send fallback notification: {e}")
        try:
            asyncio.run(self.send_health_alert())
        except Exception as e:
            logger.error(f"Failed to send health alert: {e}")

    def shutdown(self, signum, frame):
        logger.info("Shutting down scheduler...")
        self.running = False

    async def scan(self, max_items: int = 8):
        logger.info("Starting content scan...")

        try:
            items = self.scanner.scan_all(max_items=max_items)
            logger.info(f"Found {len(items)} items to process")

            processed = 0
            skipped = 0

            for item in items:
                if self.queue.content_exists(item["content_hash"]):
                    skipped += 1
                    continue

                try:
                    result = self.pipeline.run(item)
                    if result.content_pack:
                        if result.dry_run:
                            logger.info(f"Run {result.run_id}: dry run for {item['topic'][:50]}...")
                        else:
                            processed += 1
                            logger.info(f"Run {result.run_id}: generated {item['topic'][:50]}...")
                    else:
                        logger.warning(f"No content generated for: {item['topic'][:50]}")
                except Exception as e:
                    logger.error(f"Failed to process item: {e}")

                time.sleep(5)

            logger.info(f"Scan complete: {processed} new, {skipped} skipped")

            try:
                if get_dry_run(default_dry_run=load_settings()[\"runtime\"].get(\"dry_run\", False)):
                    await send_notification(
                        f\"🧪 *Dry run scan complete*\\n\\n\"
                        f\"0 queued (dry run), {len(items)} scanned\"
                    )
                elif processed > 0:
                    await send_notification(
                        f\"📬 *{processed} new draft(s) ready!*\\n\\n\"
                        f\"Use /next to start reviewing\"
                    )
                else:
                    await send_notification(
                        f\"📡 *Scan complete*\\n\\n\"
                        f\"0 new drafts ({skipped} skipped, {len(items)} scanned)\"
                    )
            except Exception as e:
                logger.error(f\"Failed to send scan notification: {e}\")

            return processed

        except Exception as e:
            logger.error(f"Scan failed: {e}")
            try:
                await send_notification(f"❌ *Scan failed*\n\n`{str(e)[:100]}`")
            except Exception:
                logger.error("Failed to send failure notification")
            return 0

    async def send_daily_summary(self):
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

    async def send_summary_with_trends(self):
        try:
            stats = self.queue.get_stats()
            items = self.scanner.scan_all(max_items=20)
            top = items[:10]

            lines = [
                "📊 *Daily Summary + Trends*",
                "",
                f"⏳ Pending: {stats['pending']}",
                f"✅ Approved: {stats['approved']}",
                f"📤 Posted: {stats['posted']}",
                f"❌ Rejected: {stats.get('rejected', 0)}",
                "",
                "🔥 *Top 10 Trends*",
                ""
            ]
            for idx, item in enumerate(top, start=1):
                score = item.get("trend_score", 0)
                title = truncate(item.get("topic", ""), 90)
                source = item.get("source", "unknown")
                lines.append(f"{idx}. ({score}) {title} — _{source}_")

            await send_notification("\n".join(lines), chat_ids=self._digest_chat_ids())
        except Exception as e:
            logger.error(f"Failed to send summary+trends: {e}")

    async def send_trend_digest(self):
        try:
            items = self.scanner.scan_all(max_items=20)
            if not items:
                await send_notification("📡 *Trend Digest*\n\nNo items found.", chat_ids=self._digest_chat_ids())
                return

            top = items[:10]
            lines = ["🔥 *Daily Trend Digest (Top 10)*", ""]
            for idx, item in enumerate(top, start=1):
                score = item.get("trend_score", 0)
                title = truncate(item.get("topic", ""), 90)
                source = item.get("source", "unknown")
                lines.append(f"{idx}. ({score}) {title} — _{source}_")

            await send_notification("\n".join(lines), chat_ids=self._digest_chat_ids())
        except Exception as e:
            logger.error(f"Failed to send trend digest: {e}")

    async def send_health_alert(self):
        chat_ids = self._health_chat_ids()
        if not chat_ids:
            return
        try:
            db_ok = self.queue.ping()
        except Exception:
            db_ok = False
        msg = (
            "🩺 *Health Check*\n\n"
            f"DB: {'ok' if db_ok else 'down'}\n"
            "Scheduler: running"
        )
        await send_notification(msg, chat_ids=chat_ids)

    def _digest_chat_ids(self):
        raw = get_env("TREND_DIGEST_CHAT_ID", "")
        if not raw:
            return None
        try:
            return [int(cid.strip()) for cid in raw.split(",") if cid.strip()]
        except ValueError:
            logger.error("Invalid TREND_DIGEST_CHAT_ID format")
            return None

    def _health_chat_ids(self):
        raw = get_env("HEALTH_ALERT_CHAT_ID", "")
        if not raw:
            return None
        try:
            return [int(cid.strip()) for cid in raw.split(",") if cid.strip()]
        except ValueError:
            logger.error("Invalid HEALTH_ALERT_CHAT_ID format")
            return None

    async def expire_old_drafts(self):
        try:
            expired = self.queue.expire_old_pending(hours=48)
            if expired > 0:
                logger.info(f"Expired {expired} old drafts")
        except Exception as e:
            logger.error(f"Failed to expire drafts: {e}")

    def run_scan(self):
        asyncio.run(self.scan())

    def run_summary(self):
        asyncio.run(self.send_daily_summary())

    def run_expire(self):
        asyncio.run(self.expire_old_drafts())

    def run_scheduler(self):
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

        logger.info("Scheduler (UTC): scans 06/10/13, summary+trends 14:30, expiry 22:00")

        schedule.every().day.at("06:00").do(self.run_scan)
        schedule.every().day.at("10:00").do(self.run_scan)
        schedule.every().day.at("13:00").do(self.run_scan)
        schedule.every().day.at("14:30").do(lambda: asyncio.run(self.send_summary_with_trends()))
        schedule.every().day.at("22:00").do(self.run_expire)

        self.run_scan()

        while self.running:
            schedule.run_pending()
            time.sleep(60)

        logger.info("Scheduler stopped.")


def main():
    parser = argparse.ArgumentParser(description="Content Machine Scheduler")
    parser.add_argument("--once", action="store_true", help="Run a single scan and exit")
    parser.add_argument("--summary", action="store_true", help="Send daily summary and exit")
    args = parser.parse_args()

    scanner = ContentScanner()

    if args.once:
        scanner.run_scan()
    elif args.summary:
        asyncio.run(scanner.send_daily_summary())
    else:
        scanner.run_scheduler()


if __name__ == "__main__":
    main()
