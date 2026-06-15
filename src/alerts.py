"""
Discord alerts — trade approval requests, notifications, daily report.

Trade approval flow:
  1. Bot posts a trade proposal message with ✅ / ❌ reactions.
  2. User reacts within approval_timeout_seconds.
  3. approved() returns True/False.
"""
import asyncio
import os
from datetime import datetime
from typing import Optional

import discord
from loguru import logger

from src.config import config
from src.strategy import Signal

TIMEOUT = config.get("risk", "approval_timeout_seconds", default=60)
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
TOKEN = os.getenv("DISCORD_TOKEN", "")

_client: Optional[discord.Client] = None
_channel: Optional[discord.TextChannel] = None


async def _get_channel() -> Optional[discord.TextChannel]:
    global _client, _channel
    if _channel:
        return _channel
    if not TOKEN or not CHANNEL_ID:
        logger.warning("Discord token or channel ID not set — alerts disabled")
        return None
    if _client is None or _client.is_closed():
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        _client = discord.Client(intents=intents)
        await _client.login(TOKEN)
        await _client.connect()
    _channel = await _client.fetch_channel(CHANNEL_ID)  # type: ignore
    return _channel


async def request_approval(signal: Signal, shares: int, commission: float) -> bool:
    """Post trade proposal and wait for ✅/❌ reaction. Returns True if approved."""
    if not config.get("risk", "require_discord_approval", default=True):
        return True

    channel = await _get_channel()
    if channel is None:
        logger.warning("Discord unavailable — auto-approving trade")
        return True

    paper = "📄 PAPER" if config.get("ibkr", "paper_trading", default=True) else "💰 LIVE"
    content = (
        f"**{paper} — LONG Signal: {signal.symbol}**\n"
        f"Entry: `${signal.entry_price:.2f}` | Stop: `${signal.stop_price:.2f}` | "
        f"Shares: `{shares}` | Commission est.: `${commission:.2f}`\n"
        f"OR High: `${signal.or_high:.2f}` | OR Low: `${signal.or_low:.2f}`\n"
        f"Reason: {signal.reason}\n"
        f"React ✅ to approve or ❌ to skip (timeout: {TIMEOUT}s)"
    )
    msg = await channel.send(content)
    await msg.add_reaction("✅")
    await msg.add_reaction("❌")

    def check(reaction, user):
        return (
            not user.bot
            and reaction.message.id == msg.id
            and str(reaction.emoji) in ("✅", "❌")
        )

    try:
        reaction, _ = await asyncio.wait_for(
            _client.wait_for("reaction_add", check=check),
            timeout=TIMEOUT,
        )
        approved = str(reaction.emoji) == "✅"
        status = "APPROVED" if approved else "REJECTED"
        await channel.send(f"Trade {signal.symbol} **{status}** by user reaction.")
        logger.info("Trade {} {} via Discord", signal.symbol, status)
        return approved
    except asyncio.TimeoutError:
        await channel.send(f"⏱ Trade {signal.symbol} **SKIPPED** — no response in {TIMEOUT}s.")
        logger.warning("Trade {} skipped — Discord approval timed out", signal.symbol)
        return False


async def send_message(text: str) -> None:
    channel = await _get_channel()
    if channel:
        await channel.send(text)


async def send_daily_report(report: str) -> None:
    await send_message(f"📊 **Daily Report — {datetime.now().strftime('%Y-%m-%d')}**\n```\n{report}\n```")


def notify(text: str) -> None:
    """Fire-and-forget message from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(send_message(text))
        else:
            loop.run_until_complete(send_message(text))
    except Exception as exc:
        logger.error("Discord notify failed: {}", exc)
