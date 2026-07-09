"""
Watchdog — monitors bot containers and restarts them if they go down.

Rules:
- Checks every 60 seconds
- Max MAX_RESTARTS_PER_DAY per container per day
- Counter resets at midnight
- Sends a Telegram alert on every restart and when giving up
"""
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime

import docker
import requests

# ── Config ────────────────────────────────────────────────────────────────────
WATCHED = os.getenv("WATCHDOG_CONTAINERS", "stock_robot-orb-bot-1").split(",")
MAX_RESTARTS_PER_DAY = int(os.getenv("WATCHDOG_MAX_RESTARTS", "5"))
CHECK_INTERVAL = int(os.getenv("WATCHDOG_CHECK_INTERVAL", "60"))

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── State ──────────────────────────────────────────────────────────────────────
restart_counts: dict[str, int] = defaultdict(int)
gave_up: set[str] = set()
last_reset: date = date.today()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _tg(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as exc:
        print(f"[watchdog] Telegram error: {exc}", flush=True)


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[watchdog] {ts} | {msg}", flush=True)


def _reset_if_new_day() -> None:
    global last_reset
    today = date.today()
    if today != last_reset:
        _log(f"New day ({today}) — resetting restart counters")
        restart_counts.clear()
        gave_up.clear()
        last_reset = today


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        client = docker.from_env()
    except Exception as exc:
        print(f"[watchdog] Cannot connect to Docker socket: {exc}", flush=True)
        sys.exit(1)

    _log(f"Starting — watching: {', '.join(WATCHED)} | max {MAX_RESTARTS_PER_DAY} restarts/day | check every {CHECK_INTERVAL}s")
    _tg(f"🐕 *Watchdog started*\nWatching: `{', '.join(WATCHED)}`\nMax restarts/day: `{MAX_RESTARTS_PER_DAY}`")

    while True:
        _reset_if_new_day()

        for name in WATCHED:
            if name in gave_up:
                continue

            try:
                container = client.containers.get(name)
                running = container.status == "running"
            except docker.errors.NotFound:
                _log(f"Container {name} not found — skipping")
                continue
            except Exception as exc:
                _log(f"Error checking {name}: {exc}")
                continue

            if running:
                continue

            count = restart_counts[name]
            _log(f"Container {name} is DOWN (restart {count + 1}/{MAX_RESTARTS_PER_DAY})")

            if count >= MAX_RESTARTS_PER_DAY:
                _log(f"Giving up on {name} — hit daily limit of {MAX_RESTARTS_PER_DAY}")
                _tg(
                    f"🚨 *Watchdog gave up on `{name}`*\n"
                    f"Crashed {MAX_RESTARTS_PER_DAY} times today — not restarting until tomorrow.\n"
                    f"Check logs: `docker logs {name} --tail 50`"
                )
                gave_up.add(name)
                continue

            try:
                container.start()
                restart_counts[name] += 1
                _log(f"Restarted {name} (restart {restart_counts[name]}/{MAX_RESTARTS_PER_DAY})")
                _tg(
                    f"⚠️ *Watchdog restarted `{name}`*\n"
                    f"Restart {restart_counts[name]}/{MAX_RESTARTS_PER_DAY} today\n"
                    f"Time: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}`"
                )
            except Exception as exc:
                _log(f"Failed to restart {name}: {exc}")
                _tg(f"🔴 *Watchdog: could not restart `{name}`* — check VPS manually")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
