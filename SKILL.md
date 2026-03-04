---
name: polymarket-fast-loop-improved
displayName: Polymarket FastLoop Trader (Direct CLOB)
description: Trade Polymarket BTC/ETH/SOL 5-minute and 15-minute fast markets using multi-signal CEX momentum. Uses Polymarket's official py-clob-client directly — no Simmer dependency. Adds funding rate confirmation, order book imbalance, time-of-day filtering, volatility-adjusted sizing, win-rate calibration, and fee-accurate EV math.
metadata: {"clawdbot":{"emoji":"⚡","requires":{"env":["POLYMARKET_PK","CLOB_API_KEY","CLOB_SECRET","CLOB_PASS_PHRASE"],"pip":["py-clob-client"]},"cron":null,"autostart":false,"automaton":{"managed":true,"entrypoint":"fastloop_improved.py"}}}
authors:
  - Based on Simmer (@simmer_markets) original, enhanced
version: "2.0.0"
published: false
---

# Polymarket FastLoop Trader — Direct CLOB Edition

An enhanced version of the FastLoop skill that trades directly via Polymarket's official `py-clob-client` — no Simmer SDK required. Signs orders with your Polygon private key and posts them straight to `clob.polymarket.com`.

> **Default is paper mode.** Use `--live` for real trades. Always run 100+ paper trades first to validate your win rate before going live.

> ⚠️ Fast markets carry Polymarket's 10% fee. Your signal needs to be right **63%+ of the time** to profit. This skill will tell you your actual win rate.

## Key Changes vs. Original (Simmer)

| Feature | Simmer Version | Direct CLOB Version |
|---------|---------------|---------------------|
| Execution | Simmer SDK → Polymarket | py-clob-client → Polymarket CLOB directly |
| Auth | SIMMER_API_KEY | Polygon private key + CLOB API creds |
| Market import step | Required (simmer.import_market) | Not needed — token IDs from Gamma API |
| Positions | simmer.get_positions() | CLOB get_balance_allowance / get_trades |
| Portfolio balance | simmer.get_portfolio() | CLOB get_balance_allowance(COLLATERAL) |
| Order type | Simmer abstraction | FOK (Fill or Kill) market order |

## Setup

### 1. Install dependency
```bash
pip install py-clob-client
```

### 2. Set your private key
```bash
export POLYMARKET_PK=0xYOUR_POLYGON_PRIVATE_KEY
```

### 3. Derive CLOB API credentials (one-time only)
```bash
python fastloop_improved.py --setup-creds
```
This prints three values — add them to your environment:
```
CLOB_API_KEY=...
CLOB_SECRET=...
CLOB_PASS_PHRASE=...
```
> ⚠️ Save these immediately — they cannot be recovered later.

### 4. Run
```bash
# Paper mode (default)
python fastloop_improved.py

# Live trading
python fastloop_improved.py --live

# Calibration stats
python fastloop_improved.py --stats

# Resolve expired paper trades against real outcomes
python fastloop_improved.py --resolve

# Quiet mode for cron / Railway
python fastloop_improved.py --live --quiet
```

## Deploying on Railway

**`railway.toml`** (place in repo root):
```toml
[deploy]
startCommand = "python fastloop_improved.py --live --quiet"
restartPolicyType = "never"

[cron]
schedule = "*/5 * * * *"
```

**Environment variables** (Railway dashboard → Variables tab):
```
POLYMARKET_PK        = 0xYOUR_PRIVATE_KEY
CLOB_API_KEY         = (from --setup-creds)
CLOB_SECRET          = (from --setup-creds)
CLOB_PASS_PHRASE     = (from --setup-creds)
SPRINT_DAILY_BUDGET  = 10
SPRINT_MAX_POSITION  = 5
AUTOMATON_MANAGED    = true
```

## How to Run on a Loop

**OpenClaw native cron:**
```bash
openclaw cron add \
  --name "FastLoop Direct" \
  --cron "*/5 * * * *" \
  --tz "UTC" \
  --session isolated \
  --message "Run fast loop: cd /path/to/skill && python fastloop_improved.py --live --quiet. Show output summary." \
  --announce
```

**Linux crontab:**
```
*/5 * * * * cd /path/to/skill && python fastloop_improved.py --live --quiet
```

## Configuration

```bash
# Raise momentum threshold (recommended: 1.0–2.0%)
python fastloop_improved.py --set min_momentum_pct=1.5

# Require order book confirmation
python fastloop_improved.py --set require_orderbook=true

# Set sweet-spot window for market selection (seconds remaining)
python fastloop_improved.py --set target_time_min=90 --set target_time_max=180

# Disable time-of-day filter (trade 24/7)
python fastloop_improved.py --set time_filter=false
```

### All Settings

| Setting | Default | Env Override | Description |
|---------|---------|-------------|-------------|
| `entry_threshold` | 0.05 | `SPRINT_ENTRY` | Min divergence from 50¢ |
| `min_momentum_pct` | 1.0 | `SPRINT_MOMENTUM` | Min % asset move |
| `max_position` | 5.0 | `SPRINT_MAX_POSITION` | Max $ per trade |
| `signal_source` | binance | `SPRINT_SIGNAL` | binance or coingecko |
| `lookback_minutes` | 5 | `SPRINT_LOOKBACK` | Candle lookback window |
| `min_time_remaining` | 60 | `SPRINT_MIN_TIME` | Skip if less than N seconds left |
| `target_time_min` | 90 | — | Prefer markets with ≥ N seconds left |
| `target_time_max` | 210 | — | Prefer markets with ≤ N seconds left |
| `asset` | BTC | `SPRINT_ASSET` | BTC, ETH, or SOL |
| `window` | 5m | `SPRINT_WINDOW` | 5m or 15m |
| `volume_confidence` | true | `SPRINT_VOL_CONF` | Skip low-volume signals |
| `require_funding` | false | — | Require funding rate confirmation |
| `require_orderbook` | false | — | Require order book imbalance confirmation |
| `time_filter` | true | — | Skip low-liquidity hours (02:00–06:00 UTC) |
| `vol_sizing` | true | — | Adjust size by recent volatility |
| `fee_buffer` | 0.05 | — | Extra edge required above fee breakeven |
| `daily_budget` | 10.0 | `SPRINT_DAILY_BUDGET` | Max spend per UTC day |
| `starting_balance` | 1000.0 | — | Paper portfolio starting balance |

## Signal Logic

Three signals are evaluated independently. Momentum is always required. Funding and order book are optional confirmation layers.

### 1. Momentum (always on)
- Fetch N one-minute Binance candles
- `momentum = (close_now - open_then) / open_then * 100`
- Must exceed `min_momentum_pct`

### 2. Funding Rate (optional, `require_funding=true`)
- Fetch Binance perpetual funding rate for the asset
- Positive funding + upward momentum = longs crowded → SKIP
- Negative funding + upward momentum = shorts being squeezed → TRADE
- Logic inverted for downward momentum

### 3. Order Book Imbalance (optional, `require_orderbook=true`)
- Fetch top 20 levels of Binance L2 book
- `imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)`
- Imbalance > 0.1 confirms upward momentum
- Imbalance < -0.1 confirms downward momentum

### Fee-Accurate EV
```
entry_price  = market price of chosen side (YES or NO token)
win_profit   = (1 - entry_price) × (1 - fee_rate)
breakeven    = entry_price / (win_profit + entry_price)
required_div = (breakeven - 0.50) + fee_buffer
```
Trade only fires if `actual_divergence ≥ required_div`.

### Time-of-Day Filter
Skips 02:00–06:00 UTC by default. US session (13:00–21:00 UTC) is the highest-liquidity window for crypto prediction markets.

### Volatility-Adjusted Sizing
```
24h_vol = std(hourly_returns_last_24h) × √24
size    = max_position × min(1.0, 0.02 / 24h_vol)
```
High volatility → smaller position. Low volatility with strong trend → full size.

## How Direct CLOB Execution Works

The original Simmer version required a two-step process: import the market slug into Simmer to get an internal market ID, then trade against that ID. This version eliminates that entirely:

1. Gamma API returns `clobTokenIds` — the YES and NO token IDs for each market
2. The signal logic picks the correct token ID based on direction (YES for up, NO for down)
3. `py-clob-client` builds a signed `MarketOrderArgs` and posts it as a **FOK (Fill or Kill)** order directly to the CLOB
4. No import step, no middleman

## Win Rate Calibration

Every paper and live trade is logged to `fastloop_ledger.json`. After market expiry, run `--resolve` to fetch the actual Polymarket outcome. After 50+ trades, `--stats` shows real win rate broken down by momentum threshold, time of day, and asset.

## Troubleshooting

**"POLYMARKET_PK not set"**
- Set your Polygon wallet private key: `export POLYMARKET_PK=0x...`

**"CLOB_API_KEY / CLOB_SECRET / CLOB_PASS_PHRASE not set"**
- Run `python fastloop_improved.py --setup-creds` and add the output to your environment.

**"No token_id — market may not have CLOB token IDs in Gamma API yet"**
- The market was just created and Gamma hasn't propagated the CLOB token IDs yet. The script will retry on the next cron cycle.

**"Funding rate fetch failed"**
- Binance futures API may be rate-limited. Skill falls back to momentum-only automatically.

**"Order book imbalance: neutral"**
- Market is balanced — skipped if `require_orderbook=true`.

**"Time filter: low liquidity window"**
- Current UTC hour is in the 02–06 block. Set `time_filter=false` to override.
