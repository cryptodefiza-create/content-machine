"""Telegram bot for content approval"""
import asyncio
import time
import os
import atexit
from typing import Optional, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

from .utils import get_env, truncate, logger, generate_content_hash, get_project_root
from .queue import QueueManager, ContentStatus
from .brain import Brain
from .imagen import ImagePromptGenerator
from .pipeline import ContentPipeline
from .scanner import Scanner
from .exporter import ExportSettings, export_content_items
from .runtime_config import get_dry_run, set_dry_run
from .settings import load_settings
import uuid


_PERSONA_DISPLAY = [
    ("pro", "PRO (Head of BD)", "💼"),
    ("work", "WORK (Alpha Hunter)", "📊"),
    ("degen", "DEGEN (Vibe Coder)", "🔥"),
]

_STATUS_ACTIONS = {
    "approve": (ContentStatus.APPROVED, "✅", "approved", "approved"),
    "reject":  (ContentStatus.REJECTED, "❌", "rejected", "rejected"),
    "posted":  (ContentStatus.POSTED,   "📤", "marked as posted", "marked as posted"),
}


class ContentBot:

    def __init__(self, pipeline: Optional[ContentPipeline] = None, queue: Optional[QueueManager] = None):
        self.token = get_env("TELEGRAM_BOT_TOKEN")
        self.allowed_chat_ids = self._parse_allowed_chats()
        self.queue = queue or QueueManager()
        self._brain: Optional[Brain] = None
        self._pipeline: Optional[ContentPipeline] = pipeline
        self._scanner: Optional[Scanner] = None
        self.imagen = ImagePromptGenerator()
        self.app: Optional[Application] = None
        self.last_react_time = 0
        self.react_cooldown = 30
        self.settings = load_settings()
        self._lock_path = None

    @property
    def brain(self) -> Brain:
        if self._brain is None:
            self._brain = Brain()
        return self._brain

    @property
    def pipeline(self) -> ContentPipeline:
        if self._pipeline is None:
            self._pipeline = ContentPipeline(settings=self.settings)
        return self._pipeline

    @property
    def scanner(self) -> Scanner:
        if self._scanner is None:
            self._scanner = Scanner()
        return self._scanner

    def _parse_allowed_chats(self) -> List[int]:
        chat_id_str = get_env("TELEGRAM_CHAT_ID", "")
        if not chat_id_str:
            logger.warning("TELEGRAM_CHAT_ID not set - bot will reject all messages")
            return []
        try:
            return [int(cid.strip()) for cid in chat_id_str.split(",")]
        except ValueError as e:
            logger.error(f"Invalid TELEGRAM_CHAT_ID format: {e}")
            return []

    async def _check_auth(self, update: Update) -> bool:
        chat_id = update.effective_chat.id
        if chat_id not in self.allowed_chat_ids:
            logger.warning(f"Unauthorized access attempt from chat_id: {chat_id}")
            await update.message.reply_text(
                "⛔ Unauthorized. This bot is private.\n"
                f"Your chat ID: `{chat_id}`",
                parse_mode=ParseMode.MARKDOWN
            )
            return False
        return True

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return

        await update.message.reply_text(
            "🤖 *Content Machine v2*\n\n"
            "*Review Commands:*\n"
            "/status - Queue stats\n"
            "/pending - List drafts\n"
            "/next - Get next draft\n"
            "/prompts <id> - Image prompts\n\n"
            "*Actions:*\n"
            "/approve <id> - Approve draft\n"
            "/reject <id> - Reject draft\n"
            "/posted <id> - Mark as posted\n\n"
            "*Generate:*\n"
            "/personas - List available personas\n"
            "/generate <persona> <topic or link> - Generate draft\n"
            "/batch <persona> <N> <topic> - Generate batch\n"
            "/style <persona> <example> | <topic> - Style transfer draft\n"
            "/trends [N|today|week] - Show trending items\n"
            "/export <run_id> - Export run to CSV/Sheets\n"
            "/health - Bot health status\n"
            "/queue - List drafts awaiting approval\n"
            "/dryrun on|off - Toggle dry run\n"
            "/react <url> <tweet text> - Generate QT for tweet",
            parse_mode=ParseMode.MARKDOWN
        )

    async def personas(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        keys = self.pipeline.persona_store.keys()
        display = ", ".join([f"`{k}`" for k in keys])
        await update.message.reply_text(
            f"Available personas: {display}",
            parse_mode=ParseMode.MARKDOWN
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return

        stats = self.queue.get_stats()
        await update.message.reply_text(
            f"📊 *Queue Status*\n\n"
            f"⏳ Pending: {stats['pending']}\n"
            f"✅ Approved: {stats['approved']}\n"
            f"📤 Posted: {stats['posted']}\n"
            f"❌ Rejected: {stats.get('rejected', 0)}\n"
            f"⏰ Expired: {stats.get('expired', 0)}\n"
            f"━━━━━━━━━━\n"
            f"📦 Total: {stats['total']}",
            parse_mode=ParseMode.MARKDOWN
        )

    async def pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return

        items = self.queue.get_pending(limit=10)
        if not items:
            await update.message.reply_text("✅ No pending drafts!")
            return

        msg = f"📝 *{len(items)} Pending Drafts*\n\n"
        for item in items:
            msg += f"*#{item.id}* - {truncate(item.source_topic, 45)}\n"
        msg += "\n_Use /next to review first draft_"

        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    async def queue_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.pending(update, context)

    async def next_draft(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return

        items = self.queue.get_pending(limit=1)
        if not items:
            await update.message.reply_text("✅ No pending drafts!")
            return

        await self._send_draft(update.effective_chat.id, items[0])

    async def prompts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return

        if not context.args:
            await update.message.reply_text("Usage: `/prompts <id>`", parse_mode=ParseMode.MARKDOWN)
            return

        try:
            item_id = int(context.args[0])
            item = self.queue.get_by_id(item_id)
            if item:
                await self._send_prompts(update.effective_chat.id, item)
            else:
                await update.message.reply_text(f"❌ Draft #{item_id} not found")
        except ValueError:
            await update.message.reply_text("❌ Invalid ID. Use a number.")

    async def _status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
        if not await self._check_auth(update):
            return

        status_enum, emoji, verb, log_verb = _STATUS_ACTIONS[action]

        if not context.args:
            await update.message.reply_text(f"Usage: `/{action} <id>`", parse_mode=ParseMode.MARKDOWN)
            return

        try:
            item_id = int(context.args[0])
            if self.queue.update_status(item_id, status_enum.value):
                await update.message.reply_text(f"{emoji} Draft #{item_id} {verb}!")
                logger.info(f"Draft #{item_id} {log_verb} via Telegram")
            else:
                await update.message.reply_text(f"❌ Draft #{item_id} not found")
        except ValueError:
            await update.message.reply_text("❌ Invalid ID. Use a number.")

    async def approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._status_command(update, context, "approve")

    async def reject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._status_command(update, context, "reject")

    async def posted(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._status_command(update, context, "posted")

    async def react(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return

        if not context.args or len(context.args) < 2:
            await update.message.reply_text(
                "Usage: `/react <tweet_url> <tweet text>`\n\n"
                "Example:\n"
                "`/react https://x.com/user/status/123 Privacy is normal.`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        url = context.args[0]
        tweet_text = " ".join(context.args[1:])

        if not self._is_twitter_url(url):
            await update.message.reply_text(
                "❌ Invalid URL. Must be an X/Twitter link.\n\n"
                "Example: `https://x.com/user/status/123`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        now = time.time()
        if now - self.last_react_time < self.react_cooldown:
            remaining = int(self.react_cooldown - (now - self.last_react_time))
            await update.message.reply_text(f"⏳ Please wait {remaining}s before generating again")
            return
        self.last_react_time = now

        await update.message.reply_text("🧠 Generating QT content...")

        username = self._extract_username(url)

        topic_data = {
            "type": "kol_qt",
            "source": f"@{username}",
            "topic": f"QT @{username}",
            "details": {"content": tweet_text},
            "url": url,
            "content_hash": generate_content_hash(url + tweet_text),
        }

        result = self.pipeline.run(topic_data)
        if result.content_pack:
            if result.dry_run:
                await update.message.reply_text("🧪 Dry run enabled. Draft not queued.")
                await self._send_draft_preview(update.effective_chat.id, result)
            else:
                item = self.queue.get_pending(limit=1)[0]
                await update.message.reply_text(f"✅ Created draft #{item.id}")
                await self._send_draft(update.effective_chat.id, item)
            logger.info(f"Generated QT content for @{username}, run {result.run_id}")
        else:
            await update.message.reply_text("❌ Generation failed. Try again.")

    async def generate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        if len(context.args) < 2:
            await update.message.reply_text("Usage: `/generate <persona> <topic or link>`", parse_mode=ParseMode.MARKDOWN)
            return

        persona = context.args[0].lower()
        topic = " ".join(context.args[1:]).strip()
        topic = " ".join(topic.split())
        if not topic:
            await update.message.reply_text("❌ Topic is empty. Try `/generate <persona> <topic>`.", parse_mode=ParseMode.MARKDOWN)
            return
        if len(topic) > 500:
            await update.message.reply_text("❌ Topic too long (max 500 chars).")
            return
        if persona not in self.pipeline.persona_store.keys():
            await update.message.reply_text(f"❌ Unknown persona: {persona}. Use /personas to list.")
            return

        await update.message.reply_text("🧠 Generating draft...")
        topic_data = {
            "topic": topic,
            "type": "manual",
            "source": "telegram",
            "content_hash": f"{generate_content_hash(topic)}-{uuid.uuid4().hex[:6]}",
        }
        result = self.pipeline.run(topic_data, personas=[persona])
        if result.content_pack:
            if result.dry_run:
                await update.message.reply_text("🧪 Dry run enabled. Draft not queued.")
                await self._send_draft_preview(update.effective_chat.id, result)
            else:
                item = self.queue.get_pending(limit=1)[0]
                await update.message.reply_text(f"✅ Created draft #{item.id}")
                await self._send_draft(update.effective_chat.id, item)
        else:
            await update.message.reply_text("❌ Generation failed.")

    async def style(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        if len(context.args) < 3:
            await update.message.reply_text(
                "Usage: `/style <persona> <example> | <topic>`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        persona = context.args[0].lower()
        if persona not in self.pipeline.persona_store.keys():
            await update.message.reply_text(f"❌ Unknown persona: {persona}. Use /personas to list.")
            return

        raw = " ".join(context.args[1:]).strip()
        if "|" not in raw:
            await update.message.reply_text("❌ Missing `|` separator. Example: `/style pro <example> | <topic>`", parse_mode=ParseMode.MARKDOWN)
            return
        example, topic = [part.strip() for part in raw.split("|", 1)]
        if not example or not topic:
            await update.message.reply_text("❌ Both example and topic are required.")
            return
        if len(example) > 500 or len(topic) > 500:
            await update.message.reply_text("❌ Example/topic too long (max 500 chars each).")
            return

        await update.message.reply_text("🧠 Generating style-transfer draft...")
        topic_data = {
            "topic": topic,
            "type": "manual",
            "source": "telegram",
            "details": {"style_example": example},
            "content_hash": f"{generate_content_hash(topic + example)}-{uuid.uuid4().hex[:6]}",
        }
        result = self.pipeline.run(topic_data, personas=[persona])
        if result.content_pack:
            if result.dry_run:
                await update.message.reply_text("🧪 Dry run enabled. Draft not queued.")
                await self._send_draft_preview(update.effective_chat.id, result)
            else:
                item = self.queue.get_pending(limit=1)[0]
                await update.message.reply_text(f"✅ Created draft #{item.id}")
                await self._send_draft(update.effective_chat.id, item)
        else:
            await update.message.reply_text("❌ Generation failed.")

    async def batch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        if len(context.args) < 3:
            await update.message.reply_text("Usage: `/batch <persona> <N> <topic>`", parse_mode=ParseMode.MARKDOWN)
            return
        persona = context.args[0].lower()
        if persona not in self.pipeline.persona_store.keys():
            await update.message.reply_text(f"❌ Unknown persona: {persona}. Use /personas to list.")
            return
        try:
            count = int(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ N must be a number")
            return
        if count < 1 or count > 5:
            await update.message.reply_text("❌ N must be between 1 and 5.")
            return
        topic = " ".join(context.args[2:]).strip()
        topic = " ".join(topic.split())
        if not topic:
            await update.message.reply_text("❌ Topic is empty.")
            return
        if len(topic) > 500:
            await update.message.reply_text("❌ Topic too long (max 500 chars).")
            return

        await update.message.reply_text(f"🧠 Generating batch ({count})...")
        created = 0
        for i in range(count):
            topic_data = {
                "topic": topic,
                "type": "manual",
                "source": "telegram",
                "content_hash": f"{generate_content_hash(topic)}-{uuid.uuid4().hex[:6]}-{i}",
            }
            result = self.pipeline.run(topic_data, personas=[persona])
            if result.content_pack:
                created += 1
                if result.dry_run:
                    await self._send_draft_preview(update.effective_chat.id, result)

        if created == 0:
            await update.message.reply_text("❌ No drafts generated.")
        elif get_dry_run(default_dry_run=self.settings["runtime"].get("dry_run", False)):
            await update.message.reply_text(f"🧪 Dry run: generated {created} drafts, none queued.")
        else:
            await update.message.reply_text(f"✅ Generated {created} drafts. Use /next to review.")

    async def dryrun(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        if not context.args:
            status = get_dry_run(default_dry_run=self.settings["runtime"].get("dry_run", False))
            await update.message.reply_text(f"Dry run is {'ON' if status else 'OFF'}")
            return
        value = context.args[0].lower()
        if value not in ("on", "off"):
            await update.message.reply_text("Usage: `/dryrun on|off`", parse_mode=ParseMode.MARKDOWN)
            return
        enabled = value == "on"
        set_dry_run(enabled)
        await update.message.reply_text(f"Dry run set to {'ON' if enabled else 'OFF'}")

    async def export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: `/export <run_id>`", parse_mode=ParseMode.MARKDOWN)
            return
        run_id = context.args[0].strip()
        if len(run_id) < 6:
            await update.message.reply_text("❌ Invalid run_id.")
            return

        items = self.queue.get_by_run_id(run_id)
        if not items:
            await update.message.reply_text(f"❌ No drafts found for run_id {run_id}")
            return

        export_cfg = self.settings.get("exports", {})
        settings = ExportSettings(
            enabled=bool(export_cfg.get("enabled", True)),
            export_dir=str(export_cfg.get("export_dir", "data/exports")),
            format=str(export_cfg.get("format", "csv")),
            master_csv=bool(export_cfg.get("master_csv", True)),
            master_csv_path=str(export_cfg.get("master_csv_path", "data/exports/all_runs.csv")),
        )
        export_content_items(settings, items)
        await update.message.reply_text(f"✅ Exported run {run_id}")

    async def trends(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        await update.message.reply_text("🔎 Fetching trend digest...")
        try:
            items = self.scanner.scan_all(max_items=20)
            if not items:
                await update.message.reply_text("📡 No items found.")
                return
            count = 10
            window_hours = None
            for token in context.args:
                t = token.lower()
                if t.isdigit():
                    count = max(1, min(int(t), 20))
                elif t in ("today", "day", "24h"):
                    window_hours = 24
                elif t in ("week", "7d"):
                    window_hours = 168

            if window_hours is not None:
                items = self._filter_items_by_window(items, window_hours)

            top = items[:count]
            label = f"Top {len(top)}"
            if window_hours == 24:
                label += " (Today)"
            elif window_hours == 168:
                label += " (This Week)"

            lines = [f"🔥 *Trend Digest {label}*", ""]
            for idx, item in enumerate(top, start=1):
                score = item.get("trend_score", 0)
                title = truncate(item.get("topic", ""), 90)
                source = item.get("source", "unknown")
                lines.append(f"{idx}. ({score}) {title} — _{source}_")
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Trend digest failed: {e}")
            await update.message.reply_text("❌ Trend digest failed. Try again.")

    async def health(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_auth(update):
            return
        try:
            db_ok = self.queue.ping()
        except Exception:
            db_ok = False
        dry = get_dry_run(default_dry_run=self.settings["runtime"].get("dry_run", False))
        lock_status = "active" if self._lock_path and self._lock_path.exists() else "none"
        await update.message.reply_text(
            f"✅ Bot is running\n"
            f"DB: {'ok' if db_ok else 'down'}\n"
            f"Dry run: {'on' if dry else 'off'}\n"
            f"Lock: {lock_status}"
        )

    @staticmethod
    def _filter_items_by_window(items, window_hours: int):
        from datetime import datetime, timezone
        cutoff = datetime.now(timezone.utc)
        filtered = []
        for item in items:
            ts = item.get("published_at") or item.get("scanned_at")
            try:
                if ts:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    delta = cutoff - dt
                    if delta.total_seconds() <= window_hours * 3600:
                        filtered.append(item)
            except Exception:
                continue
        return filtered

    @staticmethod
    def _is_twitter_url(url: str) -> bool:
        from urllib.parse import urlparse
        try:
            host = urlparse(url).hostname or ""
            return host in ("x.com", "www.x.com", "twitter.com", "www.twitter.com")
        except Exception:
            return False

    @staticmethod
    def _extract_username(url: str) -> str:
        from urllib.parse import urlparse
        try:
            path = urlparse(url).path.strip("/")
            if path:
                return path.split("/")[0]
        except Exception:
            pass
        return "unknown"

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if query.message.chat_id not in self.allowed_chat_ids:
            await query.edit_message_text("⛔ Unauthorized")
            return

        try:
            action, item_id_str = query.data.split("_", 1)
            item_id = int(item_id_str)

            if action in _STATUS_ACTIONS:
                status_enum, emoji, verb, log_verb = _STATUS_ACTIONS[action]
                self.queue.update_status(item_id, status_enum.value)
                await query.edit_message_text(f"{emoji} Draft #{item_id} {verb}!")
                logger.info(f"Draft #{item_id} {log_verb} via callback")
            elif action == "prompts":
                item = self.queue.get_by_id(item_id)
                if item:
                    await self._send_prompts(query.message.chat_id, item)
                else:
                    await query.message.reply_text(f"❌ Draft #{item_id} not found")

        except Exception as e:
            logger.error(f"Callback error: {e}")
            await query.message.reply_text("❌ Action failed")

    async def _send_draft(self, chat_id: int, item):
        try:
            await self.app.bot.send_message(
                chat_id,
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📝 *Draft #{item.id}*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📰 {truncate(item.source_topic, 100)}\n"
                f"🔎 Run: `{item.run_id or 'n/a'}`",
                parse_mode=ParseMode.MARKDOWN
            )

            for prefix, label, emoji in _PERSONA_DISPLAY:
                await asyncio.sleep(0.3)
                content = getattr(item, f"{prefix}_content")
                if not content:
                    continue
                await self.app.bot.send_message(
                    chat_id,
                    f"{emoji} *{label}*\n\n`{content}`",
                    parse_mode=ParseMode.MARKDOWN
                )

            await asyncio.sleep(0.3)

            keyboard = [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_{item.id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject_{item.id}"),
                ],
                [
                    InlineKeyboardButton("🎨 Prompts", callback_data=f"prompts_{item.id}"),
                    InlineKeyboardButton("📤 Posted", callback_data=f"posted_{item.id}"),
                ]
            ]

            await self.app.bot.send_message(
                chat_id,
                "*Actions:*",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to send draft #{item.id}: {e}")

    async def _send_draft_preview(self, chat_id: int, result):
        try:
            await self.app.bot.send_message(
                chat_id,
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🧪 *Draft Preview (not queued)*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📰 {truncate(result.content_pack.get('source_topic', ''), 100)}\n"
                f"🔎 Run: `{result.run_id}`",
                parse_mode=ParseMode.MARKDOWN
            )
            for prefix, label, emoji in _PERSONA_DISPLAY:
                draft = result.per_persona.get(prefix)
                if not draft:
                    continue
                await asyncio.sleep(0.2)
                await self.app.bot.send_message(
                    chat_id,
                    f"{emoji} *{label}*\n\n`{draft.content}`",
                    parse_mode=ParseMode.MARKDOWN
                )
        except Exception as e:
            logger.error(f"Failed to send draft preview: {e}")

    async def _send_prompts(self, chat_id: int, item):
        try:
            prompts = self.imagen.generate_all_prompts(item)

            await self.app.bot.send_message(
                chat_id,
                f"🎨 *Image Prompts for Draft #{item.id}*\n\n"
                "_Copy these to Gemini (nano banana mode)_",
                parse_mode=ParseMode.MARKDOWN
            )

            for prefix, label, emoji in _PERSONA_DISPLAY:
                prompt = prompts.get(prefix)
                if prompt:
                    await asyncio.sleep(0.3)
                    style_desc = self.imagen.STYLES[prefix]["style"].capitalize()
                    await self.app.bot.send_message(
                        chat_id,
                        f"{emoji} *{label}*\n_{style_desc}_\n\n"
                        f"`{prompt.copy_paste_prompt}`",
                        parse_mode=ParseMode.MARKDOWN
                    )
        except Exception as e:
            logger.error(f"Failed to send prompts for #{item.id}: {e}")

    def run(self):
        if not self._acquire_lock():
            logger.error("Another bot instance is already running. Exiting.")
            return
        self.app = Application.builder().token(self.token).build()

        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.start))
        self.app.add_handler(CommandHandler("personas", self.personas))
        self.app.add_handler(CommandHandler("status", self.status))
        self.app.add_handler(CommandHandler("pending", self.pending))
        self.app.add_handler(CommandHandler("queue", self.queue_list))
        self.app.add_handler(CommandHandler("next", self.next_draft))
        self.app.add_handler(CommandHandler("prompts", self.prompts))
        self.app.add_handler(CommandHandler("approve", self.approve))
        self.app.add_handler(CommandHandler("reject", self.reject))
        self.app.add_handler(CommandHandler("posted", self.posted))
        self.app.add_handler(CommandHandler("react", self.react))
        self.app.add_handler(CommandHandler("generate", self.generate))
        self.app.add_handler(CommandHandler("style", self.style))
        self.app.add_handler(CommandHandler("batch", self.batch))
        self.app.add_handler(CommandHandler("dryrun", self.dryrun))
        self.app.add_handler(CommandHandler("export", self.export))
        self.app.add_handler(CommandHandler("trends", self.trends))
        self.app.add_handler(CommandHandler("health", self.health))
        self.app.add_handler(CallbackQueryHandler(self.callback))

        logger.info("Starting Telegram bot...")
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)

    def _acquire_lock(self) -> bool:
        root = get_project_root()
        lock_path = root / "data" / "bot.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.exists():
            try:
                pid_text = lock_path.read_text().strip()
                if pid_text.isdigit():
                    pid = int(pid_text)
                    if not self._pid_alive(pid):
                        lock_path.unlink()
            except Exception:
                pass
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            self._lock_path = lock_path
            atexit.register(self._release_lock)
            return True
        except FileExistsError:
            return False

    def _release_lock(self):
        if self._lock_path and self._lock_path.exists():
            try:
                self._lock_path.unlink()
            except Exception:
                pass

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False


async def send_notification(message: str, chat_ids: Optional[List[int]] = None):
    """Send a one-off Telegram notification."""
    token = get_env("TELEGRAM_BOT_TOKEN")
    if chat_ids is None:
        chat_ids = []
        raw = get_env("TELEGRAM_CHAT_ID", "")
        if raw:
            chat_ids = [int(cid.strip()) for cid in raw.split(",") if cid.strip()]

    bot = Bot(token=token)

    for chat_id in chat_ids:
        try:
            await bot.send_message(
                int(chat_id),
                message,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to send notification to {chat_id}: {e}")


if __name__ == "__main__":
    ContentBot().run()
