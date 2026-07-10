"""
Telegram alerts — all bot messages go through here and to the log file.

Messages sent:
  - Startup: which stocks we're following
  - OR captured: high/low for each symbol
  - Trade approval request (Approve/Reject buttons)
  - Bought confirmation
  - Sold confirmation
  - Day summary
  - Error / reconnect notices
"""
import asyncio
import os
from datetime import datetime
from typing import Dict, List, Optional

from loguru import logger
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from src.config import config
from src.strategy import Signal, Action

TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
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


# ------------------------------------------------------------------
# Core send helper
# ------------------------------------------------------------------

async def _send(text: str) -> None:
    bot = _get_bot()
    if not bot:
        return
    try:
        async with bot:
            await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")
    except TelegramError as exc:
        logger.error("Telegram send failed: {}", exc)


def notify(text: str) -> None:
    """Fire-and-forget from sync context. Also logs the message."""
    logger.info("[Telegram] {}", text)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_send(text))
        else:
            loop.run_until_complete(_send(text))
    except Exception as exc:
        logger.error("Telegram notify failed: {}", exc)


# ------------------------------------------------------------------
# Structured messages
# ------------------------------------------------------------------

def send_startup(symbols: List[str]) -> None:
    text = (
        f"🤖 *ORB Bot started* — {datetime.now().strftime('%Y-%m-%d')}\n"
        f"Following: {', '.join(symbols)}"
    )
    notify(text)


def send_or_captured(or_data: Dict[str, dict]) -> None:
    """or_data = {'MU': {'high': 145.3, 'low': 139.6}, ...}"""
    lines = ["📐 *Opening Range captured:*"]
    for sym, data in or_data.items():
        lines.append(f"  {sym} — H: `${data['high']:.2f}`  L: `${data['low']:.2f}`")
    notify("\n".join(lines))


def send_bought(symbol: str, shares: int, price: float,
                target: float, gain_pct: float) -> None:
    text = (
        f"🟢 *Bought {symbol}*\n"
        f"{shares} share{'s' if shares > 1 else ''} @ `${price:.2f}`\n"
        f"Target: `${target:.2f}` | Potential gain: `{gain_pct:.1f}%`"
    )
    notify(text)


def send_sold(symbol: str, shares: int, entry: float,
              exit_price: float) -> None:
    pnl = (exit_price - entry) * shares
    pct = ((exit_price - entry) / entry) * 100
    emoji = "🏁" if pnl >= 0 else "🔴"
    text = (
        f"{emoji} *Sold {symbol}*\n"
        f"{shares} share{'s' if shares > 1 else ''} @ `${exit_price:.2f}`\n"
        f"Gained: `${pnl:+.2f}` (`{pct:+.1f}%`)"
    )
    notify(text)


async def request_data_continue(latency_seconds: float) -> bool:
    """
    Alert user that data is delayed. Ask whether to continue or skip trades.
    Returns True = continue trading, False = skip trades until data recovers.
    """
    minutes = int(latency_seconds // 60)
    seconds = int(latency_seconds % 60)
    latency_str = f"{minutes} minute{'s' if minutes != 1 else ''}" if minutes > 0 else f"{seconds} seconds"

    text = (
        f"⚠️ *Data latency warning*\n"
        f"Market data is *{latency_str} behind* real time.\n\n"
        f"This may affect OR capture and entry accuracy.\n"
        f"Should we continue trading or skip until data recovers?"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Continue", callback_data="latency_continue"),
        InlineKeyboardButton("⏭ Skip trades", callback_data="latency_skip"),
    ]])

    logger.warning("[Telegram] Data latency alert: {} behind", latency_str)

    bot = _get_bot()
    if bot is None:
        logger.warning("Telegram unavailable — continuing despite latency")
        return True

    try:
        async with bot:
            msg = await bot.send_message(
                chat_id=CHAT_ID, text=text,
                parse_mode="Markdown", reply_markup=keyboard,
            )
            msg_id = msg.message_id
            offset = None
            deadline = asyncio.get_event_loop().time() + TIMEOUT

            while asyncio.get_event_loop().time() < deadline:
                updates = await bot.get_updates(
                    offset=offset, timeout=5,
                    allowed_updates=["callback_query"]
                )
                for update in updates:
                    offset = update.update_id + 1
                    cb = update.callback_query
                    if cb and cb.message and cb.message.message_id == msg_id:
                        cont = cb.data == "latency_continue"
                        label = "Continuing ✅" if cont else "Skipping trades ⏭"
                        await cb.answer()
                        await bot.send_message(chat_id=CHAT_ID,
                                               text=f"*{label}* — data is {latency_str} behind.",
                                               parse_mode="Markdown")
                        logger.info("Latency decision: {}", label)
                        return cont

            # No response — default to skip (safer)
            await bot.send_message(chat_id=CHAT_ID,
                                   text=f"⏱ No response — *skipping trades* until data recovers.",
                                   parse_mode="Markdown")
            logger.warning("Latency alert timed out — defaulting to skip")
            return False

    except TelegramError as exc:
        logger.error("Telegram latency alert failed: {}", exc)
        return True   # fail open — continue trading


def send_day_summary(trades: int, net_pnl: float, wins: int) -> None:
    emoji = "📈" if net_pnl >= 0 else "📉"
    win_rate = f"{wins}/{trades}" if trades > 0 else "0/0"
    text = (
        f"{emoji} *Day Summary — {datetime.now().strftime('%Y-%m-%d')}*\n"
        f"Trades: `{trades}` | Net P&L: `${net_pnl:+.2f}` | Win rate: `{win_rate}`"
    )
    notify(text)


# ------------------------------------------------------------------
# Trade approval (Approve / Reject buttons)
# ------------------------------------------------------------------

async def request_approval(signal: Signal, shares: int, commission: float) -> bool:
    live = not config.get("ibkr", "paper_trading", default=True)

    if not config.get("risk", "require_discord_approval", default=True):
        if live:
            logger.error("SAFETY: approval disabled but LIVE trading — rejecting {}", signal.symbol)
            return False
        return True

    bot = _get_bot()
    if bot is None:
        if live:
            logger.error("SAFETY: Telegram unavailable and LIVE trading — rejecting {}", signal.symbol)
            return False
        logger.warning("Telegram unavailable — auto-approving trade (paper)")
        return True

    paper = "📄 PAPER" if config.get("ibkr", "paper_trading", default=True) else "💰 LIVE"
    text = (
        f"*{paper} — LONG Signal: {signal.symbol}*\n"
        f"Entry: `${signal.entry_price:.2f}` | Stop: `${signal.stop_price:.2f}`\n"
        f"Target: `${signal.target_price:.2f}` | Gain: `{signal.potential_gain_pct:.1f}%`\n"
        f"Shares: `{shares}` | Commission est.: `${commission:.2f}`\n"
        f"_{signal.reason}_\n\n"
        f"Tap within {TIMEOUT}s:"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{signal.symbol}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{signal.symbol}"),
    ]])

    logger.info("[Telegram] Approval request sent for {}", signal.symbol)

    try:
        async with bot:
            msg = await bot.send_message(
                chat_id=CHAT_ID, text=text,
                parse_mode="Markdown", reply_markup=keyboard,
            )
            msg_id = msg.message_id
            offset = None
            deadline = asyncio.get_event_loop().time() + TIMEOUT

            while asyncio.get_event_loop().time() < deadline:
                updates = await bot.get_updates(
                    offset=offset, timeout=5,
                    allowed_updates=["callback_query"]
                )
                for update in updates:
                    offset = update.update_id + 1
                    cb = update.callback_query
                    if cb and cb.message and cb.message.message_id == msg_id:
                        approved = cb.data.startswith("approve_")
                        label = "APPROVED ✅" if approved else "REJECTED ❌"
                        await cb.answer()
                        await bot.send_message(
                            chat_id=CHAT_ID,
                            text=f"Trade *{signal.symbol}* {label}",
                            parse_mode="Markdown",
                        )
                        logger.info("Trade {} {} via Telegram", signal.symbol, label)
                        return approved

            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"⏱ Trade *{signal.symbol}* skipped — no response in {TIMEOUT}s.",
                parse_mode="Markdown",
            )
            logger.warning("Trade {} skipped — approval timed out", signal.symbol)
            return False

    except TelegramError as exc:
        logger.error("Telegram approval error: {}", exc)
        return False


async def send_daily_report(report: str) -> None:
    await _send(f"📊 *Daily Report — {datetime.now().strftime('%Y-%m-%d')}*\n```\n{report}\n```")
