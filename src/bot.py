"""Telegram bot for content approval with authentication"""
import asyncio
from typing import Optional, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

from .utils import get_env, truncate, logger
from .queue import QueueManager, ContentStatus
from .brain import Brain
from .imagen import ImagePromptGenerator


class ContentBot:
    """
    Telegram bot for content approval workflow.

    Commands:
    - /start, /help - Show help
    - /status - Queue statistics
    - /pending - List pending drafts
    - /next - Get next draft for review
    - /prompts <id> - Get image prompts
    - /approve <id> - Approve draft
    - /reject <id> - Reject draft
    - /posted <id> - Mark as posted
    - /react <url> - Generate QT for tweet
    """

    def __init__(self):
        self.token = get_env("TELEGRAM_BOT_TOKEN")
        self.allowed_chat_ids = self._parse_allowed_chats()
        self.queue = QueueManager()
        self._brain: Optional[Brain] = None  # Lazy init
        self.imagen = ImagePromptGenerator()
        self.app: Optional[Application] = None

        # Rate limiting for /react command
        self.last_react_time = 0
        self.react_cooldown = 30  # seconds between /react commands

    @property
    def brain(self) -> Brain:
        """Lazy initialization of Brain (requires GEMINI_API_KEY)"""
        if self._brain is None:
            self._brain = Brain()
        return self._brain

    def _parse_allowed_chats(self) -> List[int]:
        """Parse allowed chat IDs from environment"""
        chat_id_str = get_env("TELEGRAM_CHAT_ID", "")
        if not chat_id_str:
            logger.warning("TELEGRAM_CHAT_ID not set - bot will reject all messages")
            return []

        try:
            # Support comma-separated list of chat IDs
            return [int(cid.strip()) for cid in chat_id_str.split(",")]
        except ValueError as e:
            logger.error(f"Invalid TELEGRAM_CHAT_ID format: {e}")
            return []

    async def _check_auth(self, update: Update) -> bool:
        """Check if user is authorized"""
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
        """Show help message"""
        if not await self._check_auth(update):
            return

        await update.message.reply_text(
            "🤖 *Content Machine v3*\n\n"
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
            "/react <url> - Generate QT for tweet",
            parse_mode=ParseMode.MARKDOWN
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show queue statistics"""
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
        """List pending drafts"""
        if not await self._check_auth(update):
            return

        items = self.queue.get_pending(limit=10)
        if not items:
            await update.message.reply_text("✅ No pending drafts!")
            return

        msg = f"📝 *{len(items)} Pending Drafts*\n\n"
        for item in items:
            topic = truncate(item.source_topic, 45)
            msg += f"*#{item.id}* - {topic}\n"
        msg += "\n_Use /next to review first draft_"

        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    async def next_draft(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Get next pending draft for review"""
        if not await self._check_auth(update):
            return

        items = self.queue.get_pending(limit=1)
        if not items:
            await update.message.reply_text("✅ No pending drafts!")
            return

        await self._send_draft(update.effective_chat.id, items[0])

    async def prompts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Get image prompts for a draft"""
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

    async def approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Approve a draft"""
        if not await self._check_auth(update):
            return

        if not context.args:
            await update.message.reply_text("Usage: `/approve <id>`", parse_mode=ParseMode.MARKDOWN)
            return

        try:
            item_id = int(context.args[0])
            success = self.queue.update_status(item_id, ContentStatus.APPROVED.value)

            if success:
                await update.message.reply_text(f"✅ Draft #{item_id} approved!")
                logger.info(f"Draft #{item_id} approved via Telegram")
            else:
                await update.message.reply_text(f"❌ Draft #{item_id} not found")
        except ValueError:
            await update.message.reply_text("❌ Invalid ID. Use a number.")

    async def reject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reject a draft"""
        if not await self._check_auth(update):
            return

        if not context.args:
            await update.message.reply_text("Usage: `/reject <id>`", parse_mode=ParseMode.MARKDOWN)
            return

        try:
            item_id = int(context.args[0])
            success = self.queue.update_status(item_id, ContentStatus.REJECTED.value)

            if success:
                await update.message.reply_text(f"❌ Draft #{item_id} rejected")
                logger.info(f"Draft #{item_id} rejected via Telegram")
            else:
                await update.message.reply_text(f"❌ Draft #{item_id} not found")
        except ValueError:
            await update.message.reply_text("❌ Invalid ID. Use a number.")

    async def posted(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mark a draft as posted"""
        if not await self._check_auth(update):
            return

        if not context.args:
            await update.message.reply_text("Usage: `/posted <id>`", parse_mode=ParseMode.MARKDOWN)
            return

        try:
            item_id = int(context.args[0])
            success = self.queue.update_status(item_id, ContentStatus.POSTED.value)

            if success:
                await update.message.reply_text(f"📤 Draft #{item_id} marked as posted!")
                logger.info(f"Draft #{item_id} marked as posted via Telegram")
            else:
                await update.message.reply_text(f"❌ Draft #{item_id} not found")
        except ValueError:
            await update.message.reply_text("❌ Invalid ID. Use a number.")

    async def react(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Generate QT content for a tweet"""
        if not await self._check_auth(update):
            return

        if not context.args:
            await update.message.reply_text(
                "Usage: `/react <tweet_url>`\n\n"
                "Example:\n"
                "`/react https://x.com/user/status/123`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        url = context.args[0]

        # Validate URL
        if "x.com" not in url and "twitter.com" not in url:
            await update.message.reply_text(
                "❌ Invalid URL. Must be an X/Twitter link.\n\n"
                "Example: `https://x.com/user/status/123`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # Rate limiting
        import time
        now = time.time()
        if now - self.last_react_time < self.react_cooldown:
            remaining = int(self.react_cooldown - (now - self.last_react_time))
            await update.message.reply_text(f"⏳ Please wait {remaining}s before generating again")
            return
        self.last_react_time = now

        await update.message.reply_text("🧠 Generating QT content...")

        # Extract username from URL
        username = self._extract_username(url)

        # Note: We can't fetch the actual tweet content without X API
        # Using placeholder - user should paste the tweet content if needed
        content = self.brain.generate_qt_content({
            "username": username,
            "content": f"[Tweet from @{username} - paste actual content for better results]",
            "url": url
        })

        if content:
            try:
                item = self.queue.add_content(content)
                await update.message.reply_text(f"✅ Created draft #{item.id}")
                await self._send_draft(update.effective_chat.id, item)
                logger.info(f"Generated QT content for @{username}, draft #{item.id}")
            except Exception as e:
                logger.error(f"Failed to save QT content: {e}")
                await update.message.reply_text("❌ Failed to save draft")
        else:
            await update.message.reply_text("❌ Generation failed. Try again.")

    def _extract_username(self, url: str) -> str:
        """Extract username from X/Twitter URL"""
        try:
            # Handle both x.com and twitter.com
            url = url.replace("https://", "").replace("http://", "")
            parts = url.split("/")
            if len(parts) >= 2:
                return parts[1].split("?")[0]
        except Exception:
            pass
        return "unknown"

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard callbacks"""
        query = update.callback_query
        await query.answer()

        # Check auth for callbacks too
        if query.message.chat_id not in self.allowed_chat_ids:
            await query.edit_message_text("⛔ Unauthorized")
            return

        data = query.data

        try:
            if data.startswith("approve_"):
                item_id = int(data.split("_")[1])
                self.queue.update_status(item_id, ContentStatus.APPROVED.value)
                await query.edit_message_text(f"✅ Draft #{item_id} approved!")
                logger.info(f"Draft #{item_id} approved via callback")

            elif data.startswith("reject_"):
                item_id = int(data.split("_")[1])
                self.queue.update_status(item_id, ContentStatus.REJECTED.value)
                await query.edit_message_text(f"❌ Draft #{item_id} rejected")
                logger.info(f"Draft #{item_id} rejected via callback")

            elif data.startswith("prompts_"):
                item_id = int(data.split("_")[1])
                item = self.queue.get_by_id(item_id)
                if item:
                    await self._send_prompts(query.message.chat_id, item)
                else:
                    await query.message.reply_text(f"❌ Draft #{item_id} not found")

            elif data.startswith("posted_"):
                item_id = int(data.split("_")[1])
                self.queue.update_status(item_id, ContentStatus.POSTED.value)
                await query.edit_message_text(f"📤 Draft #{item_id} marked as posted!")
                logger.info(f"Draft #{item_id} marked as posted via callback")

        except Exception as e:
            logger.error(f"Callback error: {e}")
            await query.message.reply_text("❌ Action failed")

    async def _send_draft(self, chat_id: int, item):
        """Send draft content as separate messages for easy copying"""
        # Header
        await self.app.bot.send_message(
            chat_id,
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 *Draft #{item.id}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📰 {truncate(item.source_topic, 100)}",
            parse_mode=ParseMode.MARKDOWN
        )

        # Small delay to prevent rate limiting
        await asyncio.sleep(0.3)

        # PRO post
        await self.app.bot.send_message(
            chat_id,
            f"💼 *PRO (Head of BD)*\n\n"
            f"`{item.pro_content}`",
            parse_mode=ParseMode.MARKDOWN
        )

        await asyncio.sleep(0.3)

        # WORK post
        await self.app.bot.send_message(
            chat_id,
            f"📊 *WORK (Alpha Hunter)*\n\n"
            f"`{item.work_content}`",
            parse_mode=ParseMode.MARKDOWN
        )

        await asyncio.sleep(0.3)

        # DEGEN post
        await self.app.bot.send_message(
            chat_id,
            f"🔥 *DEGEN (Vibe Coder)*\n\n"
            f"`{item.degen_content}`",
            parse_mode=ParseMode.MARKDOWN
        )

        await asyncio.sleep(0.3)

        # Action buttons
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

    async def _send_prompts(self, chat_id: int, item):
        """Send image prompts for manual generation"""
        prompts = self.imagen.generate_all_prompts(item)

        await self.app.bot.send_message(
            chat_id,
            f"🎨 *Image Prompts for Draft #{item.id}*\n\n"
            "_Copy these to Gemini (nano banana mode)_",
            parse_mode=ParseMode.MARKDOWN
        )

        await asyncio.sleep(0.3)

        persona_info = [
            ("pro", "💼 PRO", "Clean professional style"),
            ("work", "📊 WORK", "Dark trading terminal style"),
            ("degen", "🔥 DEGEN", "Cyberpunk glitch style"),
        ]

        for key, label, style in persona_info:
            prompt = prompts.get(key)
            if prompt:
                await self.app.bot.send_message(
                    chat_id,
                    f"{label}\n_{style}_\n\n"
                    f"`{prompt.copy_paste_prompt}`",
                    parse_mode=ParseMode.MARKDOWN
                )
                await asyncio.sleep(0.3)

    async def notify_new_drafts(self, count: int):
        """Send notification about new drafts"""
        if self.app and self.allowed_chat_ids:
            for chat_id in self.allowed_chat_ids:
                try:
                    await self.app.bot.send_message(
                        chat_id,
                        f"🔔 *{count} new draft(s) ready for review!*\n\n"
                        f"Use /next to start reviewing",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logger.error(f"Failed to notify chat {chat_id}: {e}")

    def run(self):
        """Start the bot"""
        self.app = Application.builder().token(self.token).build()

        # Add command handlers
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.start))
        self.app.add_handler(CommandHandler("status", self.status))
        self.app.add_handler(CommandHandler("pending", self.pending))
        self.app.add_handler(CommandHandler("next", self.next_draft))
        self.app.add_handler(CommandHandler("prompts", self.prompts))
        self.app.add_handler(CommandHandler("approve", self.approve))
        self.app.add_handler(CommandHandler("reject", self.reject))
        self.app.add_handler(CommandHandler("posted", self.posted))
        self.app.add_handler(CommandHandler("react", self.react))

        # Add callback handler for inline buttons
        self.app.add_handler(CallbackQueryHandler(self.callback))

        logger.info("Starting Telegram bot...")
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)


async def send_notification(message: str):
    """
    One-off notification (for use from cron jobs).

    Usage:
        import asyncio
        from src.bot import send_notification
        asyncio.run(send_notification("🔔 New drafts ready!"))
    """
    token = get_env("TELEGRAM_BOT_TOKEN")
    chat_ids = get_env("TELEGRAM_CHAT_ID", "").split(",")

    bot = Bot(token=token)

    for chat_id in chat_ids:
        try:
            await bot.send_message(
                int(chat_id.strip()),
                message,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to send notification to {chat_id}: {e}")


# Alias for convenience
notify = send_notification


if __name__ == "__main__":
    ContentBot().run()
