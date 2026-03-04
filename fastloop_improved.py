#!/usr/bin/env python3
"""
Polymarket FastLoop Trader — Direct CLOB Edition
=================================================
Multi-signal momentum strategy for Polymarket 5-minute fast markets.

Simmer SDK replaced with Polymarket's official py-clob-client.
Auth is now private-key based (signs orders on-chain via Polygon).

Improvements over original:
  - Funding rate confirmation (Binance perps)
  - Order book imbalance confirmation
  - Accurate fee-adjusted EV with configurable buffer
  - Time-of-day filtering (skip low-liquidity hours)
  - Volatility-adjusted position sizing
  - Win rate tracking and calibration reporting
  - Smart market selection (target time window, not just soonest)
  - Raised default momentum threshold (1.0% vs 0.5%)

Usage:
    python fastloop_improved.py              # Paper mode (default)
    python fastloop_improved.py --live       # Real trades
    python fastloop_improved.py --stats      # P&L + calibration report
    python fastloop_improved.py --resolve    # Fetch real outcomes for open trades
    python fastloop_improved.py --positions  # Show open CLOB positions
    python fastloop_improved.py --config     # Show current config
    python fastloop_improved.py --quiet      # For cron/heartbeat
    python fastloop_improved.py --set KEY=VALUE
    python fastloop_improved.py --setup-creds  # One-time: derive & save CLOB API creds

Required env vars:
    POLYMARKET_PK          Your Polygon wallet private key (0x...)
    CLOB_API_KEY           CLOB API key   (auto-derived if missing — run --setup-creds)
    CLOB_SECRET            CLOB secret
    CLOB_PASS_PHRASE       CLOB passphrase

Install:
    pip install py-clob-client
"""

import os
import sys
import json
import math
import argparse
import re
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

sys.stdout.reconfigure(line_buffering=True)

# =============================================================================
# Configuration
# =============================================================================

CONFIG_SCHEMA = {
    "entry_threshold":   {"default": 0.05,    "env": "SPRINT_ENTRY",       "type": float,
                          "help": "Min price divergence from 50¢ to trigger"},
    "min_momentum_pct":  {"default": 1.0,     "env": "SPRINT_MOMENTUM",    "type": float,
                          "help": "Min % price move to generate signal"},
    "max_position":      {"default": 5.0,     "env": "SPRINT_MAX_POSITION","type": float,
                          "help": "Max $ per trade (before vol adjustment)"},
    "signal_source":     {"default": "binance","env": "SPRINT_SIGNAL",     "type": str,
                          "help": "binance or coingecko"},
    "lookback_minutes":  {"default": 5,       "env": "SPRINT_LOOKBACK",    "type": int,
                          "help": "Minutes of candle history for momentum"},
    "min_time_remaining":{"default": 60,      "env": "SPRINT_MIN_TIME",    "type": int,
                          "help": "Hard floor: skip markets with less than N seconds left"},
    "target_time_min":   {"default": 90,      "env": None,                  "type": int,
                          "help": "Prefer markets with >= N seconds left"},
    "target_time_max":   {"default": 210,     "env": None,                  "type": int,
                          "help": "Prefer markets with <= N seconds left"},
    "asset":             {"default": "BTC",   "env": "SPRINT_ASSET",       "type": str,
                          "help": "BTC, ETH, or SOL"},
    "window":            {"default": "5m",    "env": "SPRINT_WINDOW",      "type": str,
                          "help": "5m or 15m market window"},
    "volume_confidence": {"default": True,    "env": "SPRINT_VOL_CONF",    "type": bool,
                          "help": "Skip signals with volume < 0.5x average"},
    "require_funding":   {"default": False,   "env": None,                  "type": bool,
                          "help": "Require funding rate to confirm momentum direction"},
    "require_orderbook": {"default": False,   "env": None,                  "type": bool,
                          "help": "Require order book imbalance to confirm direction"},
    "time_filter":       {"default": True,    "env": None,                  "type": bool,
                          "help": "Skip 02:00–06:00 UTC low-liquidity window"},
    "vol_sizing":        {"default": True,    "env": None,                  "type": bool,
                          "help": "Scale position size down during high volatility"},
    "fee_buffer":        {"default": 0.05,    "env": None,                  "type": float,
                          "help": "Extra divergence required above fee breakeven"},
    "daily_budget":      {"default": 10.0,    "env": "SPRINT_DAILY_BUDGET","type": float,
                          "help": "Max real $ spend per UTC day"},
    "starting_balance":  {"default": 1000.0,  "env": None,                  "type": float,
                          "help": "Paper portfolio starting balance"},
}

TRADE_SOURCE       = "fastloop-direct"
LEDGER_FILE        = "fastloop_ledger.json"
ASSET_SYMBOLS      = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
ASSET_PERP_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
ASSET_PATTERNS     = {"BTC": ["bitcoin up or down"], "ETH": ["ethereum up or down"], "SOL": ["solana up or down"]}
COINGECKO_IDS      = {"BTC": "bitcoin",  "ETH": "ethereum", "SOL": "solana"}
MIN_SHARES         = 5
SMART_SIZING_PCT   = 0.05
LOW_LIQ_HOURS      = set(range(2, 7))   # 02:00–06:00 UTC
CLOB_HOST          = "https://clob.polymarket.com"
POLYGON_CHAIN_ID   = 137
_automaton_reported = False


def _load_config(config_file=None):
    if config_file is None:
        config_file = os.path.join(os.path.dirname(__file__), "config.json")
    file_cfg = {}
    if os.path.exists(config_file):
        try:
            with open(config_file) as f:
                file_cfg = json.load(f)
        except Exception:
            pass
    result = {}
    for key, spec in CONFIG_SCHEMA.items():
        if key in file_cfg:
            result[key] = file_cfg[key]
        elif spec.get("env") and os.environ.get(spec["env"]):
            val = os.environ[spec["env"]]
            t = spec.get("type", str)
            try:
                result[key] = (val.lower() in ("true", "1", "yes")) if t == bool else t(val)
            except (ValueError, TypeError):
                result[key] = spec["default"]
        else:
            result[key] = spec["default"]
    return result


def _update_config(updates, config_file=None):
    if config_file is None:
        config_file = os.path.join(os.path.dirname(__file__), "config.json")
    existing = {}
    if os.path.exists(config_file):
        try:
            with open(config_file) as f:
                existing = json.load(f)
        except Exception:
            pass
    existing.update(updates)
    with open(config_file, "w") as f:
        json.dump(existing, f, indent=2)
    return existing


cfg = _load_config()
MAX_POSITION_USD = cfg["max_position"]
_automaton_max = os.environ.get("AUTOMATON_MAX_BET")
if _automaton_max:
    MAX_POSITION_USD = min(MAX_POSITION_USD, float(_automaton_max))

# =============================================================================
# Ledger (paper + live trade log)
# =============================================================================

def _load_ledger():
    ledger_path = os.path.join(os.path.dirname(__file__), LEDGER_FILE)
    if os.path.exists(ledger_path):
        try:
            with open(ledger_path) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "starting_balance": cfg["starting_balance"],
        "paper_balance": cfg["starting_balance"],
        "trades": [],
        "daily": {},
    }


def _save_ledger(ledger):
    ledger_path = os.path.join(os.path.dirname(__file__), LEDGER_FILE)
    with open(ledger_path, "w") as f:
        json.dump(ledger, f, indent=2, default=str)


def _get_daily(ledger):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return ledger["daily"].setdefault(today, {"spent": 0.0, "trades": 0, "pnl": 0.0})


def _record_paper_trade(ledger, trade_dict):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day = ledger["daily"].setdefault(today, {"spent": 0.0, "trades": 0, "pnl": 0.0})
    day["spent"] += trade_dict["amount_usd"]
    day["trades"] += 1
    ledger["paper_balance"] -= trade_dict["amount_usd"]
    ledger["trades"].append(trade_dict)
    _save_ledger(ledger)


# =============================================================================
# HTTP helper (for Gamma API, Binance, etc.)
# =============================================================================

def _api(url, timeout=12):
    try:
        req = Request(url, headers={"User-Agent": "fastloop-direct/1.0"})
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except HTTPError as e:
        try:
            return {"error": json.loads(e.read())["msg"]}
        except Exception:
            return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# CLOB Client (replaces Simmer)
# =============================================================================

_clob_client = None


def _get_api_creds():
    """
    Build ApiCreds from environment variables.
    Returns None if any cred is missing (triggers fallback or error).
    """
    from py_clob_client.clob_types import ApiCreds
    key     = os.environ.get("CLOB_API_KEY")
    secret  = os.environ.get("CLOB_SECRET")
    phrase  = os.environ.get("CLOB_PASS_PHRASE")
    if key and secret and phrase:
        return ApiCreds(api_key=key, api_secret=secret, api_passphrase=phrase)
    return None


def get_clob_client(live=True):
    """
    Returns a fully authenticated ClobClient (Level 2).
    Requires POLYMARKET_PK + CLOB_API_KEY / CLOB_SECRET / CLOB_PASS_PHRASE.

    For paper mode (live=False) returns a Level-1-only client (no orders placed).
    """
    global _clob_client
    if _clob_client is not None:
        return _clob_client

    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        print("Error: py-clob-client not installed. Run: pip install py-clob-client")
        sys.exit(1)

    pk = os.environ.get("POLYMARKET_PK")
    if not pk:
        print("Error: POLYMARKET_PK not set. Set your Polygon wallet private key.")
        sys.exit(1)

    creds = _get_api_creds()
    if live and not creds:
        print(
            "Error: CLOB_API_KEY / CLOB_SECRET / CLOB_PASS_PHRASE not set.\n"
            "Run:  python fastloop_improved.py --setup-creds\n"
            "Then add the printed values to your environment."
        )
        sys.exit(1)

    _clob_client = ClobClient(
        host=CLOB_HOST,
        key=pk,
        chain_id=POLYGON_CHAIN_ID,
        creds=creds,
    )
    return _clob_client


def setup_clob_creds():
    """
    One-time setup: derive CLOB API credentials from private key.
    Prints the three values to add to your .env / environment.
    """
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        print("Error: py-clob-client not installed. Run: pip install py-clob-client")
        sys.exit(1)

    pk = os.environ.get("POLYMARKET_PK")
    if not pk:
        print("Error: POLYMARKET_PK not set.")
        sys.exit(1)

    client = ClobClient(host=CLOB_HOST, key=pk, chain_id=POLYGON_CHAIN_ID)
    print("🔑 Deriving CLOB API credentials from your private key...")
    try:
        creds = client.derive_api_key()
    except Exception:
        print("No existing key found — creating a new one...")
        creds = client.create_api_key()

    print("\n✅ Add these to your environment (.env file or shell):\n")
    print(f"  CLOB_API_KEY={creds.api_key}")
    print(f"  CLOB_SECRET={creds.api_secret}")
    print(f"  CLOB_PASS_PHRASE={creds.api_passphrase}")
    print("\n⚠️  Save these — they cannot be recovered later.\n")


# =============================================================================
# CLOB: Balance / Positions (replaces simmer.get_portfolio / get_positions)
# =============================================================================

def get_usdc_balance():
    """
    Returns USDC balance from the CLOB.
    Replaces: client.get_portfolio().balance_usdc
    """
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        client = get_clob_client(live=True)
        result = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        # result is a dict: {"asset_type": ..., "balance": "123.45", ...}
        return float(result.get("balance", 0))
    except Exception as e:
        print(f"  ⚠️  Could not fetch USDC balance: {e}")
        return None


def get_open_positions():
    """
    Returns open conditional token positions.
    Replaces: client.get_positions()
    Uses the CLOB /data/positions endpoint via the client.
    """
    try:
        client = get_clob_client(live=True)
        # get_trades returns recent fill history; we parse open balance per token
        result = client.get_trades()
        return result if isinstance(result, list) else []
    except Exception as e:
        print(f"  ⚠️  Could not fetch positions: {e}")
        return []


# =============================================================================
# Market Discovery (Gamma API — unchanged, but now extracts token IDs)
# =============================================================================

def _parse_end_time(question):
    """Parse ET end time from fast market question string."""
    pattern = r'(\w+ \d+),.*?-\s*(\d{1,2}:\d{2}(?:AM|PM))\s*ET'
    m = re.search(pattern, question)
    if not m:
        return None
    try:
        year = datetime.now(timezone.utc).year
        dt = datetime.strptime(f"{m.group(1)} {year} {m.group(2)}", "%B %d %Y %I:%M%p")
        return dt.replace(tzinfo=timezone.utc) + timedelta(hours=5)  # ET → UTC
    except Exception:
        return None


def discover_fast_markets(asset="BTC", window="5m"):
    """
    Finds active fast markets via Gamma API.
    Now also extracts YES/NO token IDs (clobTokenIds) needed for CLOB orders.
    """
    patterns = ASSET_PATTERNS.get(asset, ASSET_PATTERNS["BTC"])
    url = (
        "https://gamma-api.polymarket.com/markets"
        "?limit=20&closed=false&tag=crypto&order=createdAt&ascending=false"
    )
    result = _api(url)
    if not result or (isinstance(result, dict) and result.get("error")):
        return []

    markets = []
    for m in result:
        q    = (m.get("question") or "").lower()
        slug = m.get("slug", "")
        if any(p in q for p in patterns) and f"-{window}-" in slug and not m.get("closed"):
            try:
                prices = json.loads(m.get("outcomePrices", "[]"))
                yes_price = float(prices[0]) if prices else 0.5
            except Exception:
                yes_price = 0.5

            # Extract YES and NO token IDs from Gamma API
            # Gamma returns clobTokenIds as JSON string: ["yes_id", "no_id"]
            yes_token_id = None
            no_token_id  = None
            try:
                raw_ids = m.get("clobTokenIds") or m.get("tokens", "[]")
                if isinstance(raw_ids, str):
                    raw_ids = json.loads(raw_ids)
                if isinstance(raw_ids, list) and len(raw_ids) >= 2:
                    yes_token_id = raw_ids[0]
                    no_token_id  = raw_ids[1]
            except Exception:
                pass

            markets.append({
                "question":     m.get("question", ""),
                "slug":         slug,
                "condition_id": m.get("conditionId", ""),
                "end_time":     _parse_end_time(m.get("question", "")),
                "yes_price":    yes_price,
                "fee_rate_bps": int(m.get("feeRateBps") or m.get("fee_rate_bps") or 1000),
                "yes_token_id": yes_token_id,
                "no_token_id":  no_token_id,
            })
    return markets


def select_best_market(markets):
    """
    Prefer markets in the target time window.
    Falls back to any market above min_time_remaining.
    """
    now        = datetime.now(timezone.utc)
    target_min = cfg["target_time_min"]
    target_max = cfg["target_time_max"]
    min_floor  = cfg["min_time_remaining"]

    sweet_spot, fallback = [], []
    for m in markets:
        end = m.get("end_time")
        if not end:
            continue
        secs = (end - now).total_seconds()
        if secs < min_floor:
            continue
        if target_min <= secs <= target_max:
            sweet_spot.append((secs, m))
        else:
            fallback.append((secs, m))

    if sweet_spot:
        sweet_spot.sort(key=lambda x: x[0])
        return sweet_spot[0][1], True
    if fallback:
        fallback.sort(key=lambda x: x[0])
        return fallback[0][1], False
    return None, False


# =============================================================================
# Signal 1: Momentum (Binance klines) — unchanged
# =============================================================================

def get_momentum_signal(asset="BTC", source="binance", lookback=5):
    if source == "coingecko":
        cg = COINGECKO_IDS.get(asset, "bitcoin")
        r  = _api(f"https://api.coingecko.com/api/v3/simple/price?ids={cg}&vs_currencies=usd")
        price = (r or {}).get(cg, {}).get("usd") if not (r or {}).get("error") else None
        if not price:
            return None
        return {"momentum_pct": 0, "direction": "neutral", "price_now": price,
                "price_then": price, "volume_ratio": 1.0, "strong": False}

    symbol = ASSET_SYMBOLS.get(asset, "BTCUSDT")
    url    = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m&limit={lookback}"
    result = _api(url)
    if not result or isinstance(result, dict):
        return None
    try:
        candles      = result
        price_then   = float(candles[0][1])
        price_now    = float(candles[-1][4])
        momentum_pct = (price_now - price_then) / price_then * 100
        vols         = [float(c[5]) for c in candles]
        avg_vol      = sum(vols) / len(vols)
        vol_ratio    = vols[-1] / avg_vol if avg_vol > 0 else 1.0
        return {
            "momentum_pct": momentum_pct,
            "direction":    "up" if momentum_pct > 0 else "down",
            "price_now":    price_now,
            "price_then":   price_then,
            "avg_volume":   avg_vol,
            "volume_ratio": vol_ratio,
            "strong":       abs(momentum_pct) >= cfg["min_momentum_pct"],
        }
    except Exception:
        return None


# =============================================================================
# Signal 2: Funding Rate — unchanged
# =============================================================================

def get_funding_signal(asset="BTC"):
    symbol = ASSET_PERP_SYMBOLS.get(asset, "BTCUSDT")
    url    = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1"
    result = _api(url)
    if not result or isinstance(result, dict) and result.get("error"):
        return {"rate": None, "available": False}
    try:
        rate = float(result[0]["fundingRate"])
        if rate > 0.0001:
            bias = "long"
        elif rate < -0.0001:
            bias = "short"
        else:
            bias = "neutral"
        return {"rate": rate, "bias": bias, "available": True}
    except Exception:
        return {"rate": None, "available": False}


def funding_confirms(funding, momentum_direction):
    if not funding.get("available"):
        return None
    bias = funding["bias"]
    if momentum_direction == "up":
        return bias == "short"
    else:
        return bias == "long"


# =============================================================================
# Signal 3: Order Book Imbalance — unchanged
# =============================================================================

def get_orderbook_signal(asset="BTC", levels=20):
    symbol = ASSET_SYMBOLS.get(asset, "BTCUSDT")
    url    = f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit={levels}"
    result = _api(url)
    if not result or result.get("error"):
        return {"imbalance": None, "available": False}
    try:
        bid_depth = sum(float(b[1]) for b in result.get("bids", []))
        ask_depth = sum(float(a[1]) for a in result.get("asks", []))
        total     = bid_depth + ask_depth
        if total == 0:
            return {"imbalance": 0, "available": True}
        imbalance = (bid_depth - ask_depth) / total
        return {
            "imbalance":  round(imbalance, 4),
            "bid_depth":  bid_depth,
            "ask_depth":  ask_depth,
            "available":  True,
        }
    except Exception:
        return {"imbalance": None, "available": False}


def orderbook_confirms(ob, momentum_direction):
    if not ob.get("available") or ob.get("imbalance") is None:
        return None
    imbalance = ob["imbalance"]
    THRESHOLD = 0.10
    if momentum_direction == "up":
        return imbalance > THRESHOLD
    else:
        return imbalance < -THRESHOLD


# =============================================================================
# Volatility-Adjusted Sizing — unchanged
# =============================================================================

def get_24h_volatility(asset="BTC"):
    symbol = ASSET_SYMBOLS.get(asset, "BTCUSDT")
    url    = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=25"
    result = _api(url)
    if not result or isinstance(result, dict):
        return None
    try:
        closes = [float(c[4]) for c in result]
        if len(closes) < 2:
            return None
        returns   = [(closes[i] / closes[i-1] - 1) for i in range(1, len(closes))]
        mean      = sum(returns) / len(returns)
        variance  = sum((r - mean) ** 2 for r in returns) / len(returns)
        hourly_sd = math.sqrt(variance)
        daily_vol = hourly_sd * math.sqrt(24)
        return daily_vol
    except Exception:
        return None


def volatility_adjusted_size(max_size, asset="BTC"):
    vol = get_24h_volatility(asset)
    if vol is None or vol <= 0:
        return max_size
    target_vol = 0.02
    scale      = min(1.0, target_vol / vol)
    return round(max_size * scale, 2)


# =============================================================================
# Fee-Accurate EV — unchanged
# =============================================================================

def fee_adjusted_breakeven(entry_price, fee_rate):
    win_profit = (1 - entry_price) * (1 - fee_rate)
    if win_profit + entry_price == 0:
        return 1.0
    return entry_price / (win_profit + entry_price)


def required_divergence(entry_price, fee_rate, buffer=0.05):
    be  = fee_adjusted_breakeven(entry_price, fee_rate)
    div = (be - 0.50) + buffer
    return max(div, cfg["entry_threshold"])


# =============================================================================
# Time-of-Day Filter — unchanged
# =============================================================================

def is_low_liquidity_window():
    hour = datetime.now(timezone.utc).hour
    return hour in LOW_LIQ_HOURS


# =============================================================================
# Outcome Resolution — unchanged (Gamma API)
# =============================================================================

def resolve_trade_outcome(condition_id):
    if not condition_id:
        return None
    url    = f"https://gamma-api.polymarket.com/markets?conditionId={condition_id}"
    result = _api(url)
    if not result or isinstance(result, dict):
        return None
    try:
        market = result[0] if isinstance(result, list) else result
        if not market.get("closed"):
            return None
        prices = json.loads(market.get("outcomePrices", "[]"))
        if not prices:
            return None
        yes_price = float(prices[0])
        return "yes" if yes_price > 0.95 else "no"
    except Exception:
        return None


def resolve_open_trades(ledger):
    now      = datetime.now(timezone.utc)
    resolved = 0
    for t in ledger["trades"]:
        if t.get("status") != "open":
            continue
        end_str = t.get("end_time")
        if not end_str:
            continue
        try:
            end_time = datetime.fromisoformat(end_str)
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if now < end_time + timedelta(minutes=1):
            continue
        outcome = resolve_trade_outcome(t.get("condition_id"))
        if outcome is None:
            continue
        fee_rate    = t.get("fee_rate", 0.10)
        entry_price = t.get("entry_price", 0.5)
        amount      = t.get("amount_usd", 0)
        won         = (outcome == t.get("side"))
        payout      = (amount / entry_price) * (1 - fee_rate) if won else 0
        pnl         = payout - amount
        t["status"]      = "won" if won else "lost"
        t["pnl"]         = round(pnl, 4)
        t["outcome"]     = outcome
        t["resolved_at"] = now.isoformat()
        if t.get("mode") == "paper":
            ledger["paper_balance"] += payout
        date = t.get("date", now.strftime("%Y-%m-%d"))
        if date in ledger["daily"]:
            ledger["daily"][date]["pnl"] = ledger["daily"][date].get("pnl", 0) + pnl
        resolved += 1
    if resolved:
        _save_ledger(ledger)
    return resolved


# =============================================================================
# Stats / Calibration Report — unchanged
# =============================================================================

def show_stats(ledger):
    trades  = ledger["trades"]
    closed  = [t for t in trades if t.get("status") in ("won", "lost")]
    open_   = [t for t in trades if t.get("status") == "open"]
    live_   = [t for t in closed if t.get("mode") == "live"]
    paper_  = [t for t in closed if t.get("mode") == "paper"]

    def _stats_block(label, subset):
        if not subset:
            print(f"  {label}: no closed trades yet")
            return
        wins       = [t for t in subset if t.get("status") == "won"]
        total_pnl  = sum(t.get("pnl", 0) for t in subset)
        total_cost = sum(t.get("amount_usd", 0) for t in subset)
        win_rate   = len(wins) / len(subset) * 100
        roi        = total_pnl / total_cost * 100 if total_cost > 0 else 0
        avg_win    = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        losses     = [t for t in subset if t.get("status") == "lost"]
        avg_loss   = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        print(f"  {label}: {len(subset)} closed | WR {win_rate:.1f}% | P&L ${total_pnl:+.2f} | ROI {roi:+.1f}%")
        print(f"    avg win ${avg_win:+.2f} | avg loss ${avg_loss:+.2f}")

    print("\n⚡ FastLoop Direct — Stats & Calibration")
    print("=" * 55)
    print(f"  Paper balance:     ${ledger.get('paper_balance', cfg['starting_balance']):.2f} "
          f"(started ${ledger['starting_balance']:.2f})")
    print(f"  Open trades:       {len(open_)}")
    _stats_block("Paper", paper_)
    _stats_block("Live",  live_)

    if closed:
        print("\n  📈 Calibration: win rate by momentum strength")
        bands = [(0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 99)]
        for lo, hi in bands:
            band = [t for t in closed if lo <= abs(t.get("momentum_pct", 0)) < hi]
            if not band:
                continue
            wins = sum(1 for t in band if t.get("status") == "won")
            wr   = wins / len(band) * 100
            flag = " ✅" if wr >= 63 else " ⚠️ " if wr >= 55 else " ❌"
            print(f"    {lo:.1f}–{hi:.1f}% mom: {len(band)} trades, WR {wr:.0f}%{flag}")
        print("    (Need ≥63% WR to profit after 10% fee)")

        print("\n  🕐 Calibration: win rate by UTC hour")
        hour_data = {}
        for t in closed:
            try:
                h = datetime.fromisoformat(t["timestamp"]).hour
                hour_data.setdefault(h, []).append(t)
            except Exception:
                pass
        for h in sorted(hour_data):
            grp  = hour_data[h]
            wins = sum(1 for t in grp if t.get("status") == "won")
            wr   = wins / len(grp) * 100
            flag = "✅" if wr >= 63 else "⚠️ " if wr >= 55 else "❌"
            print(f"    UTC {h:02d}:xx  {len(grp):>3} trades  WR {wr:.0f}%  {flag}")

    print("\n  🔁 Last 10 trades:")
    for t in reversed(trades[-10:]):
        icon = {"open": "⏳", "won": "✅", "lost": "❌"}.get(t.get("status"), "?")
        pnl  = f"${t['pnl']:+.2f}" if t.get("pnl") is not None else "pending"
        mode = "[P]" if t.get("mode") == "paper" else "[L]"
        mom  = f"{t.get('momentum_pct', 0):+.2f}%"
        print(f"    {icon}{mode} {t['timestamp'][:16]}  {t.get('side','?').upper():<3}  "
              f"${t.get('amount_usd', 0):.2f}  mom={mom}  → {pnl}")

    if ledger["daily"]:
        print("\n  📅 Last 7 days (UTC):")
        for date in sorted(ledger["daily"].keys())[-7:]:
            d = ledger["daily"][date]
            print(f"    {date}  trades={d['trades']}  spent=${d['spent']:.2f}  pnl=${d.get('pnl', 0):+.2f}")


# =============================================================================
# Trade Execution (replaces Simmer import_market + trade)
# =============================================================================

def execute_trade_direct(token_id, side, amount_usd, entry_price, dry_run=True):
    """
    Places a market order directly via the Polymarket CLOB.

    In paper mode (dry_run=True): simulates the trade locally, no API call.

    In live mode: uses py-clob-client to:
      1. Build a MarketOrderArgs (FOK — Fill or Kill at market price)
      2. Sign it with your private key
      3. Post to clob.polymarket.com

    Args:
        token_id:    YES or NO token ID from Gamma API (clobTokenIds[0] or [1])
        side:        "yes" or "no"  → maps to BUY
        amount_usd:  Dollar amount to spend
        entry_price: Current YES/NO price (used for share calculation in paper mode)
        dry_run:     If True, simulate only

    Returns dict with keys: success, shares_bought, trade_id, error, simulated
    """
    if dry_run:
        # Paper simulation — no network call
        shares = round(amount_usd / entry_price, 2) if entry_price > 0 else 0
        return {
            "success":       True,
            "trade_id":      f"paper-{datetime.now(timezone.utc).strftime('%H%M%S')}",
            "shares_bought": shares,
            "error":         None,
            "simulated":     True,
        }

    if not token_id:
        return {
            "success": False, "trade_id": None, "shares_bought": 0,
            "error": "No token_id — market may not have CLOB token IDs in Gamma API yet",
            "simulated": False,
        }

    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        client = get_clob_client(live=True)

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usd,   # BUY mode: amount is in USDC
            side=BUY,
        )
        signed_order = client.create_market_order(order_args)
        resp = client.post_order(signed_order, orderType=OrderType.FOK)

        # resp is a dict from the CLOB API
        # Successful: {"orderID": "...", "status": "matched", "size_matched": "...", ...}
        if resp and not resp.get("error"):
            order_id     = resp.get("orderID") or resp.get("id", "")
            size_matched = float(resp.get("size_matched") or resp.get("sizeFilled") or 0)
            return {
                "success":       True,
                "trade_id":      order_id,
                "shares_bought": size_matched,
                "error":         None,
                "simulated":     False,
            }
        else:
            err = resp.get("error") or resp.get("message") or str(resp)
            return {"success": False, "trade_id": None, "shares_bought": 0,
                    "error": err, "simulated": False}

    except Exception as e:
        return {"success": False, "trade_id": None, "shares_bought": 0,
                "error": str(e), "simulated": False}


def show_positions():
    """
    Shows open CLOB positions.
    Replaces the Simmer --positions flag.
    """
    print("\n📊 Open CLOB Positions (fast markets):")
    trades = get_open_positions()
    fast = [t for t in trades if isinstance(t, dict)
            and "up or down" in (t.get("question", "") or t.get("title", "")).lower()]
    if not fast:
        print("  No open fast market positions found in recent trades.")
        print("  (CLOB positions reflect on-chain token balances — check your wallet for full history.)")
        return
    for t in fast:
        print(f"  • {str(t.get('question', t.get('market', '')))[:60]}")
        print(f"    Side: {t.get('side','?').upper()}  Size: {t.get('size', t.get('shares', '?'))}  "
              f"Price: {t.get('price', '?')}")


# =============================================================================
# Main Strategy
# =============================================================================

def run(dry_run=True, positions_only=False, show_config=False,
        smart_sizing=False, quiet=False):

    global _automaton_reported
    skip_reasons = []

    def log(msg, force=False):
        if not quiet or force:
            print(msg)

    def bail(summary, reason=None):
        global _automaton_reported
        if reason:
            skip_reasons.append(reason)
        if not quiet:
            print(f"📊 Summary: {summary}")
        if os.environ.get("AUTOMATON_MANAGED") and not _automaton_reported:
            print(json.dumps({"automaton": {
                "signals": 0, "trades_attempted": 0, "trades_executed": 0,
                "skip_reason": ", ".join(dict.fromkeys(skip_reasons)) or "no_signal"
            }}))
            _automaton_reported = True

    log("⚡ FastLoop Direct Trader (py-clob-client)")
    log("=" * 50)
    mode_label = "[DRY RUN — paper mode]" if dry_run else "[LIVE — real orders]"
    log(f"\n  {mode_label}")

    if show_config:
        log("\n⚙️  Current config:")
        for k, v in cfg.items():
            log(f"    {k}: {v}")
        log(f"\n  Config file: {os.path.join(os.path.dirname(__file__), 'config.json')}")
        return

    # Validate auth early
    if not dry_run:
        get_clob_client(live=True)   # exits with message if creds missing

    ledger = _load_ledger()

    if positions_only:
        show_positions()
        return

    log(f"\n⚙️  Config summary:")
    log(f"  Asset: {cfg['asset']} {cfg['window']} | "
        f"momentum ≥ {cfg['min_momentum_pct']}% | divergence ≥ {cfg['entry_threshold']} | "
        f"max ${MAX_POSITION_USD:.2f}")
    log(f"  Signals: momentum{'+ funding' if cfg['require_funding'] else ''}"
        f"{'+ orderbook' if cfg['require_orderbook'] else ''} | "
        f"time_filter={'on' if cfg['time_filter'] else 'off'} | "
        f"vol_sizing={'on' if cfg['vol_sizing'] else 'off'}")

    daily = _get_daily(ledger)
    remaining_budget = cfg["daily_budget"] - daily["spent"]
    log(f"  Budget: ${remaining_budget:.2f} remaining today (${daily['spent']:.2f}/${cfg['daily_budget']:.2f})")

    # ── Gate 0: Time-of-day filter ───────────────────────────────────────────
    if cfg["time_filter"] and is_low_liquidity_window():
        hour = datetime.now(timezone.utc).hour
        bail(f"Skip (low-liquidity UTC hour {hour:02d}:xx — set time_filter=false to override)",
             "low_liquidity_hour")
        return

    # ── Step 1: Discover & select market ────────────────────────────────────
    log(f"\n🔍 Discovering {cfg['asset']} {cfg['window']} fast markets...")
    markets = discover_fast_markets(cfg["asset"], cfg["window"])
    log(f"  Found {len(markets)} active markets")
    if not markets:
        bail("No markets available")
        return

    best, in_sweet_spot = select_best_market(markets)
    if not best:
        bail(f"No markets with >{cfg['min_time_remaining']}s remaining")
        return

    now       = datetime.now(timezone.utc)
    secs_left = (best["end_time"] - now).total_seconds()
    yes_price = best["yes_price"]
    fee_rate  = best["fee_rate_bps"] / 10000

    log(f"\n🎯 Market: {best['question']}")
    log(f"  Time left: {secs_left:.0f}s {'(sweet spot ✓)' if in_sweet_spot else '(fallback)'}")
    log(f"  YES price: ${yes_price:.3f} | Fee: {fee_rate:.0%}")

    yes_token_id = best.get("yes_token_id")
    no_token_id  = best.get("no_token_id")
    if yes_token_id:
        log(f"  YES token: {yes_token_id[:20]}...")
    else:
        log(f"  ⚠️  No CLOB token IDs found — live trades will fail for this market")

    # ── Step 2: Momentum ─────────────────────────────────────────────────────
    log(f"\n📈 Signal 1 — Momentum ({cfg['signal_source']})...")
    mom = get_momentum_signal(cfg["asset"], cfg["signal_source"], cfg["lookback_minutes"])
    if not mom:
        bail("Failed to fetch price data", "price_fetch_failed")
        return

    momentum_pct = abs(mom["momentum_pct"])
    direction    = mom["direction"]
    log(f"  {cfg['asset']}: ${mom['price_now']:,.2f} (was ${mom['price_then']:,.2f})")
    log(f"  Momentum: {mom['momentum_pct']:+.3f}% | Vol ratio: {mom['volume_ratio']:.2f}x | "
        f"Direction: {direction.upper()}")

    if momentum_pct < cfg["min_momentum_pct"]:
        bail(f"Skip (momentum {momentum_pct:.3f}% < min {cfg['min_momentum_pct']}%)", "weak_momentum")
        return

    if cfg["volume_confidence"] and mom["volume_ratio"] < 0.5:
        bail(f"Skip (low volume: {mom['volume_ratio']:.2f}x avg)", "low_volume")
        return

    log(f"  ✓ Momentum gate passed")

    # ── Step 3: Funding rate ─────────────────────────────────────────────────
    if cfg["require_funding"]:
        log(f"\n📉 Signal 2 — Funding rate...")
        funding = get_funding_signal(cfg["asset"])
        if funding.get("available"):
            rate_pct = (funding["rate"] or 0) * 100
            confirms = funding_confirms(funding, direction)
            log(f"  Rate: {rate_pct:+.4f}% | Bias: {funding['bias']} | "
                f"Confirms {direction.upper()}: {'✓' if confirms else '✗'}")
            if confirms is False:
                bail(f"Skip (funding rate opposes momentum: {funding['bias']} bias vs {direction} move)",
                     "funding_conflict")
                return
            if confirms is True:
                log(f"  ✓ Funding confirmation")
        else:
            log(f"  ⚠️  Funding rate unavailable — skipping this gate")
    else:
        log(f"  Signal 2 (funding): off")

    # ── Step 4: Order book ───────────────────────────────────────────────────
    ob_imbalance_val = None
    if cfg["require_orderbook"]:
        log(f"\n📊 Signal 3 — Order book imbalance...")
        ob = get_orderbook_signal(cfg["asset"])
        if ob.get("available") and ob.get("imbalance") is not None:
            ob_imbalance_val = ob["imbalance"]
            confirms = orderbook_confirms(ob, direction)
            log(f"  Imbalance: {ob['imbalance']:+.3f} | "
                f"Confirms {direction.upper()}: {'✓' if confirms else ('✗' if confirms is False else '?')}")
            if confirms is False:
                bail(f"Skip (order book opposes momentum: imbalance {ob['imbalance']:+.3f})",
                     "orderbook_conflict")
                return
            if confirms is True:
                log(f"  ✓ Order book confirmation")
        else:
            log(f"  ⚠️  Order book unavailable — skipping this gate")
    else:
        log(f"  Signal 3 (orderbook): off")

    # ── Step 5: Divergence + EV check ───────────────────────────────────────
    log(f"\n🧠 EV Analysis...")
    if direction == "up":
        side        = "yes"
        token_id    = yes_token_id
        entry_price = yes_price
        divergence  = 0.50 + cfg["entry_threshold"] - yes_price
        rationale   = f"{cfg['asset']} up {mom['momentum_pct']:+.3f}% but YES only ${yes_price:.3f}"
    else:
        side        = "no"
        token_id    = no_token_id
        entry_price = 1 - yes_price
        divergence  = yes_price - (0.50 - cfg["entry_threshold"])
        rationale   = f"{cfg['asset']} down {mom['momentum_pct']:+.3f}% but YES still ${yes_price:.3f}"

    if divergence <= 0:
        bail(f"Skip (no divergence: {divergence:.3f} — market already priced in)", "no_divergence")
        return

    req_div   = required_divergence(entry_price, fee_rate, cfg["fee_buffer"])
    breakeven = fee_adjusted_breakeven(entry_price, fee_rate)
    log(f"  Side: {side.upper()} | Entry: ${entry_price:.3f}")
    log(f"  Actual divergence:   {divergence:.3f}")
    log(f"  Required divergence: {req_div:.3f} (fee breakeven {breakeven:.1%} + {cfg['fee_buffer']:.2f} buffer)")

    if divergence < req_div:
        bail(f"Skip (divergence {divergence:.3f} < required {req_div:.3f} after {fee_rate:.0%} fee)",
             "insufficient_ev")
        return

    log(f"  ✓ EV gate passed (edge: {divergence - req_div:.3f} above threshold)", force=True)

    # ── Step 6: Position sizing ──────────────────────────────────────────────
    log(f"\n💰 Sizing...")
    if cfg["vol_sizing"] and cfg["signal_source"] == "binance":
        position_size = volatility_adjusted_size(MAX_POSITION_USD, cfg["asset"])
        vol = get_24h_volatility(cfg["asset"])
        log(f"  24h vol: {(vol or 0)*100:.2f}% | Vol-adjusted size: ${position_size:.2f}")
    else:
        position_size = MAX_POSITION_USD
        log(f"  Fixed size: ${position_size:.2f}")

    if smart_sizing:
        balance = get_usdc_balance()
        if balance is not None:
            smart = balance * SMART_SIZING_PCT
            position_size = min(position_size, smart)
            log(f"  Smart sizing (5% of ${balance:.2f}): ${smart:.2f} → using ${position_size:.2f}")

    position_size = min(position_size, remaining_budget)
    if position_size < 0.50:
        bail(f"Skip (position ${position_size:.2f} too small — daily budget ${remaining_budget:.2f} remaining)",
             "budget_exhausted")
        return

    if entry_price > 0 and (MIN_SHARES * entry_price) > position_size:
        bail(f"Skip (need ${MIN_SHARES * entry_price:.2f} for min {MIN_SHARES} shares, have ${position_size:.2f})",
             "position_too_small")
        return

    log(f"  Final size: ${position_size:.2f}", force=True)

    # ── Step 7: Signal summary ───────────────────────────────────────────────
    log(f"\n✅ All gates passed — TRADE", force=True)
    log(f"   {rationale}", force=True)
    log(f"   Divergence {divergence:.3f} | Momentum {momentum_pct:.3f}% | Size ${position_size:.2f}", force=True)

    # ── Step 8: Execute ──────────────────────────────────────────────────────
    tag = "PAPER" if dry_run else "LIVE"
    log(f"\n🔗 Placing {side.upper()} ${position_size:.2f} ({tag}) via CLOB...", force=True)

    result = execute_trade_direct(
        token_id=token_id,
        side=side,
        amount_usd=round(position_size, 2),
        entry_price=entry_price,
        dry_run=dry_run,
    )

    trade_executed = 0
    if result and result.get("success"):
        shares    = result.get("shares_bought") or 0
        trade_id  = result.get("trade_id")
        simulated = result.get("simulated", dry_run)
        log(f"  ✅ {'[PAPER] ' if simulated else ''}Bought {shares:.1f} {side.upper()} "
            f"@ ${entry_price:.3f}", force=True)
        trade_executed = 1

        trade_record = {
            "timestamp":    now.isoformat(),
            "date":         now.strftime("%Y-%m-%d"),
            "mode":         "paper" if simulated else "live",
            "asset":        cfg["asset"],
            "side":         side,
            "market":       best["question"],
            "slug":         best["slug"],
            "condition_id": best.get("condition_id", ""),
            "yes_token_id": best.get("yes_token_id"),
            "no_token_id":  best.get("no_token_id"),
            "token_id":     token_id,
            "end_time":     best["end_time"].isoformat() if best.get("end_time") else None,
            "entry_price":  entry_price,
            "amount_usd":   round(position_size, 2),
            "shares":       shares,
            "divergence":   round(divergence, 4),
            "momentum_pct": round(mom["momentum_pct"], 4),
            "volume_ratio": round(mom["volume_ratio"], 2),
            "ob_imbalance": ob_imbalance_val,
            "fee_rate":     fee_rate,
            "trade_id":     trade_id,
            "status":       "open",
            "pnl":          None,
        }
        if simulated:
            _record_paper_trade(ledger, trade_record)
        else:
            daily = _get_daily(ledger)
            daily["spent"]  += position_size
            daily["trades"] += 1
            ledger["trades"].append(trade_record)
            _save_ledger(ledger)
    else:
        err = (result.get("error") if result else "No response") or "Unknown"
        log(f"  ❌ Trade failed: {err}", force=True)

    print(f"\n📊 Summary:")
    print(f"  Market:    {best['question'][:55]}")
    print(f"  Signal:    {direction.upper()} {momentum_pct:.3f}% | YES ${yes_price:.3f}")
    print(f"  Action:    {'PAPER' if dry_run else ('TRADED' if trade_executed else 'FAILED')} "
          f"{side.upper()} ${position_size:.2f}")

    if os.environ.get("AUTOMATON_MANAGED"):
        amount = round(position_size, 2) if trade_executed else 0
        print(json.dumps({"automaton": {
            "signals": 1,
            "trades_attempted": 1,
            "trades_executed":  trade_executed,
            "amount_usd":       amount,
        }}))
        _automaton_reported = True


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FastLoop Direct Trader (py-clob-client)")
    parser.add_argument("--live",          action="store_true", help="Execute real trades via CLOB")
    parser.add_argument("--dry-run",       action="store_true", help="Paper mode (default)")
    parser.add_argument("--positions",     action="store_true", help="Show open CLOB positions")
    parser.add_argument("--config",        action="store_true", help="Show current config")
    parser.add_argument("--stats",         action="store_true", help="P&L and calibration report")
    parser.add_argument("--resolve",       action="store_true", help="Fetch real outcomes for open trades")
    parser.add_argument("--smart-sizing",  action="store_true", help="Size by CLOB USDC balance")
    parser.add_argument("--quiet", "-q",   action="store_true", help="Only print on trades/errors")
    parser.add_argument("--setup-creds",   action="store_true", help="Derive & print CLOB API credentials")
    parser.add_argument("--set",           action="append",     metavar="KEY=VALUE", help="Update config")
    args = parser.parse_args()

    if args.setup_creds:
        setup_clob_creds()
        sys.exit(0)

    if args.set:
        updates = {}
        for item in args.set:
            if "=" not in item:
                print(f"Invalid: {item}  →  use KEY=VALUE")
                sys.exit(1)
            key, val = item.split("=", 1)
            if key not in CONFIG_SCHEMA:
                print(f"Unknown key '{key}'. Valid: {', '.join(CONFIG_SCHEMA)}")
                sys.exit(1)
            t = CONFIG_SCHEMA[key]["type"]
            try:
                updates[key] = (val.lower() in ("true", "1", "yes")) if t == bool else t(val)
            except ValueError:
                print(f"Bad value for {key}: {val}")
                sys.exit(1)
        _update_config(updates)
        print(f"✅ Config updated: {json.dumps(updates)}")
        sys.exit(0)

    if args.stats:
        show_stats(_load_ledger())
        sys.exit(0)

    if args.resolve:
        ledger   = _load_ledger()
        resolved = resolve_open_trades(ledger)
        print(f"✅ Resolved {resolved} trade(s) against real Polymarket outcomes")
        if resolved:
            show_stats(ledger)
        sys.exit(0)

    run(
        dry_run=       not args.live,
        positions_only=args.positions,
        show_config=   args.config,
        smart_sizing=  args.smart_sizing,
        quiet=         args.quiet,
    )

    if os.environ.get("AUTOMATON_MANAGED") and not _automaton_reported:
        print(json.dumps({"automaton": {
            "signals": 0, "trades_attempted": 0, "trades_executed": 0,
            "skip_reason": "no_signal"
        }}))
