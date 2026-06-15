"""
Telegram alerts — trade approval requests, notifications, daily report.

Approval flow:
  1. Bot sends a message with ✅ Approve / ❌ Reject inline buttons.
  2. User taps a button within approval_timeout_seconds.
  3. request_approval() returns True (approved) or False (rejected/timeout).
"""
import asyncio
import os
from datetime import datetime
from typing import Optional

from loguru import logger
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from src.config import config
from src.strategy import Signal

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TIMEOUT = config.get("risk", "approval_timeout_seconds", default=60)

_bot: Optional[Bot] = None


def _get_bot() -> Optional[Bot]:
    global _bot
    if not TOKEN or not CHAT_ID:
        logger.warning("Telegram TOKEN or CHAT_ID not set — alerts disabled")
        return None
    if _bot is None:
        _bot = Bot(token=TOKEN)
    return _bot


async def request_approval(signal: Signal, shares: int, commission: float) -> bool:
    """Send trade proposal with Approve/Reject buttons. Returns True if approved."""
    if not config.get("risk", "require_discord_approval", default=True):
        return True

    bot = _get_bot()
    if bot is None:
        logger.warning("Telegram unavailable — auto-approving trade")
        return True

    paper = "📄 PAPER" if config.get("ibkr", "paper_trading", default=True) else "💰 LIVE"
    text = (
        f"*{paper} — LONG Signal: {signal.symbol}*\n"
        f"Entry: `${signal.entry_price:.2f}` | Stop: `${signal.stop_price:.2f}`\n"
        f"Shares: `{shares}` | Commission: `${commission:.2f}`\n"
        f"OR High: `${signal.or_high:.2f}` | OR Low: `${signal.or_low:.2f}`\n"
        f"_{signal.reason}_\n\n"
        f"Tap a button within {TIMEOUT}s:"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{signal.symbol}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{signal.symbol}"),
    ]])

    try:
        async with bot:
            msg = await bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            msg_id = msg.message_id

            # Poll for callback query from the user
            offset = None
            deadline = asyncio.get_event_loop().time() + TIMEOUT
            while asyncio.get_event_loop().time() < deadline:
                updates = await bot.get_updates(offset=offset, timeout=5,
                                                allowed_updates=["callback_query"])
                for update in updates:
                    offset = update.update_id + 1
                    cb = update.callback_query
                    if cb and cb.message and cb.message.message_id == msg_id:
                        approved = cb.data.startswith("approve_")
                        label = "APPROVED ✅" if approved else "REJECTED ❌"
                        await cb.answer()
                        await bot.send_message(chat_id=CHAT_ID,
                                               text=f"Trade *{signal.symbol}* {label}",
                                               parse_mode="Markdown")
                        logger.info("Trade {} {} via Telegram", signal.symbol, label)
                        return approved

            await bot.send_message(chat_id=CHAT_ID,
                                   text=f"⏱ Trade *{signal.symbol}* skipped — no response in {TIMEOUT}s.",
                                   parse_mode="Markdown")
            logger.warning("Trade {} skipped — Telegram approval timed out", signal.symbol)
            return False

    except TelegramError as exc:
        logger.error("Telegram error during approval: {}", exc)
        return False


async def send_message(text: str) -> None:
    bot = _get_bot()
    if not bot:
        return
    try:
        async with bot:
            await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")
    except TelegramError as exc:
        logger.error("Telegram send_message failed: {}", exc)


async def send_daily_report(report: str) -> None:
    header = f"📊 *Daily Report — {datetime.now().strftime('%Y-%m-%d')}*\n"
    await send_message(header + f"```\n{report}\n```")


def notify(text: str) -> None:
    """Fire-and-forget message from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(send_message(text))
        else:
            loop.run_until_complete(send_message(text))
    except Exception as exc:
        logger.error("Telegram notify failed: {}", exc)
