# stock_robot — ORB Trading Bot

Autonomous Opening Range Breakout + Retest trading bot for Interactive Brokers.
Runs 24/7 on a cloud VPS. Human approves every trade via Discord. Dashboard accessible from any phone or browser.

---

## How It Works

1. At 09:30 the bot records the **High and Low** of each stock for the first N minutes (default: 5).
2. After that window, it watches for a **Breakout** above the OR High.
3. It then waits for a **Retest** — price returns to the breakout level.
4. A **Discord message** is sent asking for your ✅ approval.
5. You react ✅ → order placed. React ❌ or ignore → trade skipped.
6. At 16:00 all positions close and a daily P&L report is posted to Discord.

---

## Full Setup Guide (Step by Step)

### Prerequisites
- A Linux VPS (Hetzner CX21 €5/mo or DigitalOcean $6/mo recommended)
- Docker + Docker Compose installed on the VPS
- An IBKR account with paper trading enabled
- A Discord server where you are admin

---

### Step 1 — Get a VPS

Sign up at [Hetzner](https://hetzner.com) or [DigitalOcean](https://digitalocean.com).
Choose **Ubuntu 22.04**, at least 2 GB RAM.

SSH into your VPS:
```bash
ssh root@<your-vps-ip>
```

Install Docker:
```bash
curl -fsSL https://get.docker.com | sh
apt install docker-compose-plugin -y
```

---

### Step 2 — Upload the Project

From your PC (in the `stock_robot` folder):
```bash
scp -r . root@<your-vps-ip>:/opt/stock_robot
```

Or clone from GitHub if you push it there:
```bash
git clone https://github.com/orisegal10/stock_robot /opt/stock_robot
```

---

### Step 3 — Configure Credentials

On the VPS:
```bash
cd /opt/stock_robot
cp .env.example .env
nano .env
```

Fill in:
```
IBKR_USERNAME=your_ibkr_username
IBKR_PASSWORD=your_ibkr_paper_password
DISCORD_TOKEN=your_discord_bot_token
DISCORD_CHANNEL_ID=your_channel_id
```

Save with `Ctrl+X → Y → Enter`.

---

### Step 4 — Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` → choose a name (e.g. `ORB Trading Bot`) and a username (e.g. `orb_myname_bot`)
3. BotFather replies with a **token** — copy it into `.env` as `TELEGRAM_TOKEN`
4. Search for **@userinfobot** in Telegram → send `/start` → copy the **Id** number into `.env` as `TELEGRAM_CHAT_ID`

That's it — no developer portal, no permissions setup.

---

### Step 5 — Configure IBKR Connection

The bot connects to **IB Gateway** running in Docker.

In `config.yaml`:
```yaml
ibkr:
  host: "127.0.0.1"
  port: 4002          # paper trading port
  paper_trading: true
```

When ready to go live, change to:
```yaml
  port: 4001
  paper_trading: false
```

In `docker-compose.yml`, change `TRADING_MODE: paper` to `TRADING_MODE: live`.

---

### Step 6 — Update the Stock Universe

Edit `universe/universe.csv` to add/remove/update stocks.

Columns:
| Column | Description |
|---|---|
| `symbol` | Stock ticker (e.g. MU) |
| `volatility_hv` | 30-day Historical Volatility (%) |
| `stop_loss_pct` | Stop loss % below entry (e.g. 1.15) |
| `atr_14` | 14-day ATR in dollars |
| `min_volume` | Minimum daily volume filter |
| `rsi_14` | Current RSI-14 value (update every few days) |
| `max_position_usd` | Max dollars to invest in this stock per trade |
| `notes` | Your notes |
| `active` | True = include, False = skip |

**Only trade stocks with volatility_hv between 70–110** (set in `config.yaml` under `filters`).

---

### Step 7 — Start the Bot

```bash
cd /opt/stock_robot
docker compose up -d
```

Check logs:
```bash
docker compose logs -f orb-bot
```

---

### Step 8 — Open the Dashboard

Open your browser (phone or desktop):
```
http://<your-vps-ip>:8501
```

You'll see live status, trade history, log stream, and current config.

---

### Step 9 — Approve Trades via Telegram

When the bot detects a valid signal, you'll get a Telegram message like:

> **📄 PAPER — LONG Signal: MU**
> Entry: `$192.50` | Stop: `$190.28`
> Shares: `78` | Commission: `$0.78`
> OR High: `$191.80` | OR Low: `$189.40`
> _ORB breakout + retest of 191.80_
>
> [ ✅ Approve ]  [ ❌ Reject ]

Tap **Approve** to place the order. Tap **Reject** or ignore for 60 seconds to skip.

---

### Step 10 — Paper Trading First

Run on paper trading for **at least 1 week** before going live.
Review the daily reports posted to Discord each evening.
When satisfied, switch to live (Step 5).

---

## Adjusting Parameters

All parameters are in `config.yaml`. Edit and restart the bot:

```bash
nano /opt/stock_robot/config.yaml
docker compose restart orb-bot
```

Key parameters to tune:
- `opening_range.duration_minutes` — 1, 3, 5, 10, 15
- `strategy.retest_window_minutes` — how long to wait for retest
- `risk.risk_per_trade_percent` — size of each trade relative to portfolio
- `risk.max_daily_loss_percent` — kill switch for the day
- `risk.require_discord_approval` — set to `false` for fully automatic mode

---

## Running Tests

On your PC (with Python installed):
```bash
cd stock_robot
pip install -r requirements.txt
pytest tests/ -v
```

All tests run without an IBKR connection.

---

## File Structure

```
stock_robot/
├── main.py              # bot entry point
├── config.yaml          # all parameters
├── .env                 # secrets (never commit this)
├── docker-compose.yml   # IB Gateway + bot + dashboard
├── universe/universe.csv  # your stock list
├── logs/                # daily log files
├── data/trades.db       # trade history (SQLite)
├── src/                 # trading engine
│   ├── config.py
│   ├── connection.py    # IBKR connection
│   ├── data_feed.py     # real-time prices + OR capture
│   ├── strategy.py      # ORB + retest logic
│   ├── risk_manager.py  # position sizing & limits
│   ├── execution.py     # order placement
│   ├── alerts.py        # Discord bot
│   ├── reporting.py     # daily P&L report
│   └── universe.py      # loads universe.csv
└── ui/
    └── streamlit_app.py # web dashboard
```

---

## Important Safety Notes

- Always start with **paper trading** (`paper_trading: true` in config.yaml)
- `max_daily_loss_percent: 3.0` — bot stops trading for the day if losses exceed 3%
- `max_open_positions: 4` — never holds more than 4 positions at once
- `max_position_usd` per stock — hard cap in universe.csv
- `require_discord_approval: true` — you approve every trade before it executes
- IB Gateway paper account is completely separate from your real money account
