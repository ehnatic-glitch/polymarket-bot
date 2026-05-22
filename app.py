# ============================================================================
# POLYMARKET SNIPER v2.0 — Lean & Mean
# ----------------------------------------------------------------------------
# Single-file Flask app · deploy na Render
# Framework: v2.0 PDF — Tier A=30, B=15, C<=10 USDC · Max 3 CORE + 4 SANDBOX
# APIs: gamma-api.polymarket.com (markets) + data-api.polymarket.com (positions/trades)
# Bankroll: 500 USDC fixne · Reserve: 150 · Max exposure: 200
# ============================================================================

from flask import Flask, jsonify, request
import requests
import json
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

APP_CONFIG = {
    "version": "v2.0 Lean",
    "title": "Polymarket Sniper v2.0",

    # Bankroll (v2.0 fixne)
    "bankroll_usdc": 500.0,
    "cash_reserve_usdc": 150.0,
    "max_total_exposure_usdc": 200.0,

    # Position limits
    "max_core_positions": 3,
    "max_sandbox_positions": 4,

    # Tier sizing (v2.0 PDF)
    "tier_a_stake": 30.0,
    "tier_b_stake": 15.0,
    "tier_c_stake_max": 10.0,
    "tier_c_stake_default": 8.0,  # pre centovky < 5¢

    # Scanning defaults
    "default_min_liquidity": 75000.0,
    "default_min_volume24": 15000.0,

    # Whale detection
    "whale_trade_min_notional": 200000.0,
    "whale_wallet_recent_sum": 500000.0,
}

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"


# ============================================================================
# UTILITIES — parsing, formatting
# ============================================================================

def to_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def safe_num_or_none(value):
    try:
        if value is None or value == "":
            return None
        n = float(value)
        if n != n:  # NaN check
            return None
        return n
    except Exception:
        return None


def parse_json_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            return []
    return []


def parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        return None


def short_wallet(addr):
    if not addr or not isinstance(addr, str) or len(addr) < 12:
        return addr or ""
    return f"{addr[:6]}...{addr[-4:]}"


def format_ts(ts):
    try:
        ts = int(float(ts))
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return None


def clamp_price(x):
    if x is None:
        return None
    return max(0.01, min(0.99, round(x, 3)))


def round_usdc(x):
    return int(round(x))


def add_days_iso(days_from_now):
    """Vráti ISO date stringu N dní od teraz."""
    return (datetime.now(timezone.utc) + timedelta(days=days_from_now)).date().isoformat()


# ============================================================================
# POLYMARKET API CLIENT
# ============================================================================

def get_yes_no_prices(market):
    """Vráti (yes_price, no_price) z markets endpointu (handles multiple formats)."""
    prices = parse_json_list(market.get("outcomePrices"))
    yes_price = None
    no_price = None

    if len(prices) >= 2:
        yes_price = safe_num_or_none(prices[0])
        no_price = safe_num_or_none(prices[1])
    elif len(prices) == 1:
        yes_price = safe_num_or_none(prices[0])
        if isinstance(yes_price, (int, float)):
            no_price = 1 - yes_price
    else:
        best_bid = safe_num_or_none(market.get("bestBid"))
        best_ask = safe_num_or_none(market.get("bestAsk"))
        last_trade = safe_num_or_none(market.get("lastTradePrice"))
        if isinstance(best_bid, (int, float)):
            yes_price = best_bid
        elif isinstance(last_trade, (int, float)):
            yes_price = last_trade
        if isinstance(best_ask, (int, float)):
            no_price = 1 - best_ask
        elif isinstance(yes_price, (int, float)):
            no_price = 1 - yes_price

    if isinstance(yes_price, (int, float)):
        yes_price = max(0.0, min(1.0, round(yes_price, 3)))
    if isinstance(no_price, (int, float)):
        no_price = max(0.0, min(1.0, round(no_price, 3)))

    return yes_price, no_price


def fetch_active_markets(limit=250):
    """Fetch active markets from gamma-api."""
    try:
        r = requests.get(
            f"{GAMMA_BASE}/markets",
            params={"limit": limit, "active": "true", "closed": "false"},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def fetch_market_by_slug(slug):
    """Fetch single market by slug."""
    markets = fetch_active_markets(limit=500)
    return next((m for m in markets if m.get("slug") == slug), None)


def fetch_positions(wallet, limit=100):
    """Fetch open positions for a wallet from data-api."""
    if not wallet:
        return []
    try:
        r = requests.get(
            f"{DATA_API_BASE}/positions",
            params={"user": wallet, "limit": limit, "sizeThreshold": 1},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])
    except Exception:
        return []


def fetch_wallet_trades_raw(wallet, limit=500, min_amount=None):
    """Fetch trades for a wallet from data-api."""
    if not wallet:
        return []
    params = {"user": wallet, "limit": limit}
    if min_amount is not None:
        params["filterType"] = "CASH"
        params["filterAmount"] = min_amount
    try:
        r = requests.get(f"{DATA_API_BASE}/trades", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])
    except Exception:
        return []


def fetch_market_trades_raw(condition_ids, limit=30, min_amount=None):
    """Fetch trades for a market (by condition IDs) from data-api."""
    if not condition_ids:
        return []
    params = {
        "limit": limit,
        "market": ",".join(condition_ids),
    }
    if min_amount is not None:
        params["filterType"] = "CASH"
        params["filterAmount"] = min_amount
    try:
        r = requests.get(f"{DATA_API_BASE}/trades", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])
    except Exception:
        return []


def fetch_leaderboard_raw(limit=10):
    """Fetch leaderboard from data-api."""
    try:
        r = requests.get(
            f"{DATA_API_BASE}/v1/leaderboard",
            params={"limit": limit},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("results") or data.get("data") or data.get("leaderboard") or []
        return []
    except Exception:
        return []


def extract_market_condition_ids(market):
    """Extract conditionIds + clobTokenIds from market for trade queries."""
    ids = []
    for key in ["conditionId", "condition_id"]:
        value = market.get(key)
        if isinstance(value, str) and value.startswith("0x") and len(value) == 66:
            ids.append(value)
    clob = market.get("clobTokenIds")
    if isinstance(clob, str):
        try:
            parsed = json.loads(clob)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, str) and item.startswith("0x") and len(item) == 66:
                        ids.append(item)
        except Exception:
            pass
    seen = []
    for x in ids:
        if x not in seen:
            seen.append(x)
    return seen


def normalize_trade(item):
    """Normalize trade dict from data-api into canonical form."""
    wallet = item.get("proxyWallet") or item.get("wallet") or item.get("maker_address") or ""
    name = item.get("name") or item.get("pseudonym") or short_wallet(wallet) or "unknown"
    price = safe_num_or_none(item.get("price"))
    size = safe_num_or_none(item.get("size"))
    timestamp = item.get("timestamp") or item.get("match_time") or item.get("last_update")
    notional = None
    if isinstance(price, (int, float)) and isinstance(size, (int, float)):
        notional = round(price * size, 2)
    return {
        "wallet": wallet,
        "walletShort": short_wallet(wallet),
        "name": name,
        "side": item.get("side") or "",
        "outcome": item.get("outcome") or "",
        "price": price,
        "size": size,
        "notional": notional,
        "timestamp": timestamp,
        "timestampIso": format_ts(timestamp),
        "title": item.get("title") or item.get("marketTitle") or "",
        "slug": item.get("slug") or item.get("eventSlug") or "",
    }


# ============================================================================
# CATEGORIZATION & EDGE DETECTION
# ============================================================================

def categorize_market(question):
    q = (question or "").lower()
    sports_kw = ["world cup", "nba finals", "nfl", "mlb", "stanley cup", "champions league",
                 "premier league", "ufc", "fifa", "win the finals", "wins the"]
    politics_kw = ["presidential", "election", "senate", "house", "democratic", "republican",
                   "nomination", "trump", "vance", "rubio", "newsom", "macron", "prime minister",
                   "parliament", "cabinet", "governor", "starmer", "merz"]
    crypto_kw = ["bitcoin", "btc", "ethereum", "eth", "solana", "xrp", "crypto", "kraken",
                 "coinbase", "ipo", "doge", "token"]
    geo_kw = ["ukraine", "nato", "china", "india", "military", "war", "troops", "ceasefire",
              "taiwan", "iran", "israel", "hezbollah", "gaza", "russia", "syria", "hormuz"]
    meme_kw = ["gta", "jesus christ", "$1m", "meme"]

    if any(k in q for k in sports_kw): return "Sports"
    if any(k in q for k in politics_kw): return "Politics"
    if any(k in q for k in crypto_kw): return "Crypto"
    if any(k in q for k in geo_kw): return "Geopolitics"
    if any(k in q for k in meme_kw): return "Narrative"
    return "Other"


def detect_edge_type(question, days_to_end, yes_price):
    """
    v2.0 Edge Check — detekuje edge typy:
    - text: slovíčkarenie v rules, specific entity required
    - oracle: ambiguous resolution rules s interpretation gap
    - time_decay: tight deadline + procedural inertia + milestone
    - structural: market s endDate do 90 dní kde čas pracuje proti jednej strane
    - asymmetric: centovky < 5¢
    Vracia (edge_type, edge_description) alebo (None, reason).
    """
    q = (question or "").lower()

    # Pure directional lottery indicators → no edge
    directional_kw = ["up or down", "5 minutes", "hourly", "daily", "minute",
                      "this week", "today"]
    if any(k in q for k in directional_kw):
        return None, "Smerová lotéria — žiadny edge"

    # Sports directional — "Will X win Y?" bez time/oracle edge
    # (centovky na šport sú OK cez asymmetric nižšie)
    sports_directional = ["win the 2026 fifa", "win the world cup", "win the nba",
                          "win the stanley", "win the super bowl", "wins the"]
    is_sports_directional = any(k in q for k in sports_directional)

    # Oracle edge — wording quirks v rules suggesting interpretation room
    oracle_words = ["good faith", "sole discretion", "official sources only",
                    "materially", "substantially", "called by"]
    if any(k in q for k in oracle_words):
        return "oracle", "Oracle wording umožňuje interpretačnú diskusiu"

    # Text edge — specific named entity must match exactly
    if re.search(r"h\.?r\.\s*\d+|s\.\s*\d+", q):
        return "text", "Špecifické bill number — text edge ak Senate prepíše"
    # Specific named acts/bills
    if re.search(r"(clarity act|genius act|stable act|save act|defiance act)", q):
        return "text", "Named legislation — text edge na špecifický zákon"

    # ----- TIME DECAY (explicit) -----
    # Milestone language in question
    milestone_kw = ["agreement", "deal", "signed", "ceasefire", "summit", "meeting",
                    "announcement", "vote", "resolution", "treaty", "deliver",
                    "sign into law", "approve", "pass ", "ratif", "deploy",
                    "withdraw", "return to normal", "restore", "launch", "IPO",
                    "resign", "fired", "removed", "appointed", "confirmed"]
    has_milestone = any(k in q for k in milestone_kw)

    # Explicit deadline in text
    has_text_deadline = bool(re.search(
        r"by (january|february|march|april|may|june|july|august|september|"
        r"october|november|december|\d|end of|q[1-4])", q))

    if has_milestone and has_text_deadline and days_to_end is not None and days_to_end <= 120:
        return "time_decay", f"Milestone + explicit deadline ({int(days_to_end)} dní)"

    # Out-by markets (politician/leader out by/before date)
    if ("out by" in q or "out before" in q) and days_to_end is not None and days_to_end <= 240:
        return "time_decay", f"Out-by/before deadline ({int(days_to_end)} dní) → status quo bias"

    # ----- STRUCTURAL TIME DECAY (implicit from endDate) -----
    # Market má endDate do 90 dní + milestone keyword (aj bez "by Month" v texte)
    if has_milestone and days_to_end is not None and days_to_end <= 90:
        return "time_decay", f"Milestone + implicit deadline ({int(days_to_end)} dní od endDate)"

    # Market s krátkym endDate (<=60 dní) + event-driven category
    # Nie sports directional (to je lotéria), ale geopolitics, politics, crypto — tu čas
    # pracuje proti YES outcomes (veci sa väčšinou nestihnú)
    event_categories_kw = ["ceasefire", "iran", "israel", "ukraine", "russia", "china",
                           "taiwan", "nato", "election", "nomination", "impeach",
                           "resign", "tariff", "sanction", "ban", "law", "act",
                           "regulation", "rate cut", "rate hike", "recession",
                           "diplomatic", "invasion", "annex"]
    has_event = any(k in q for k in event_categories_kw)
    if has_event and days_to_end is not None and days_to_end <= 90:
        return "time_decay", f"Event-driven + {int(days_to_end)} dní deadline → time works against YES"

    # General "by" deadline with endDate
    if has_text_deadline and days_to_end is not None and days_to_end <= 60:
        return "time_decay", f"Explicit deadline + {int(days_to_end)} dní do resolution"

    # ----- STRUCTURAL (non-time-decay) -----
    # Any market with endDate <=45 dní that is NOT a pure sports directional
    # At this point, short-dated markets have structural theta on NO side
    if (days_to_end is not None and days_to_end <= 45
            and not is_sports_directional
            and isinstance(yes_price, (int, float)) and 0.10 <= yes_price <= 0.90):
        return "structural", f"Krátky deadline ({int(days_to_end)} dní) → štrukturálna theta pre NO"

    # ----- ASYMMETRIC CENTOVKA -----
    if isinstance(yes_price, (int, float)) and 0.01 <= yes_price <= 0.05:
        return "asymmetric", f"Centovka @ {yes_price:.3f} = max risk 1, asymmetric payoff"

    return None, "Žiadny jasný text/oracle/time-decay edge"


def oracle_risk_level(question):
    """Vráti oracle risk: Low / Medium / High."""
    q = (question or "").lower()
    high_kw = ["good faith", "sole discretion", "official sources only",
               "materially", "substantially", "at any time"]
    medium_kw = ["called by", "out by", "military clash", "any country leave"]

    if any(k in q for k in high_kw): return "High"
    if any(k in q for k in medium_kw): return "Medium"
    return "Low"


def detect_catalyst(question, days_to_end):
    q = (question or "").lower()
    if any(k in q for k in ["vote", "voting", "election", "runoff"]):
        return ("Vote/Election", "High")
    if any(k in q for k in ["deadline", "by ", "before end of"]):
        return ("Deadline", "Medium")
    if any(k in q for k in ["earnings", "cpi", "report", "fomc", "fed"]):
        return ("Report/Announcement", "High")
    if any(k in q for k in ["finals", "world cup"]):
        return ("Scheduled event", "Medium")
    if days_to_end is not None and days_to_end <= 7:
        return ("Near expiry", "Medium")
    return ("Unclear", "Low")


def detect_cluster(question, category):
    """Naratívny klaster pre correlation check."""
    q = (question or "").lower()
    if "world cup" in q or "fifa" in q: return "FIFA World Cup 2026"
    if "nba finals" in q: return "NBA Finals 2026"
    if "presidential" in q or "president" in q: return "US Presidential"
    if "senate" in q: return "US Senate"
    if "bitcoin" in q or "btc" in q: return "Bitcoin"
    if "ethereum" in q or "eth" in q: return "Ethereum"
    if "ukraine" in q: return "Ukraine"
    if any(k in q for k in ["israel", "gaza", "hezbollah", "iran", "syria", "hormuz"]):
        return "Middle East"
    return f"{category}: misc"


# ============================================================================
# KILL-SWITCH (v2.0 PILLAR 1) — 3 binary questions
# ============================================================================

def kill_switch_check(market, edge_type, edge_reason, liquidity, volume24, yes_price,
                      open_clusters=None):
    """
    v2.0 Pillar 1: 3 binárne otázky. Aj jedno NIE = PASS.
    Vráti dict s q1/q2/q3 (bool + note) a overall pass_kill_switch (bool).
    """
    open_clusters = open_clusters or {}

    # Q1: Edge Check
    q1_pass = edge_type is not None
    q1_note = edge_reason if not q1_pass else f"Edge type: {edge_type} — {edge_reason}"

    # Q2: Correlation Check — pozri či máme >= 1 trade v rovnakom klastri
    cluster = detect_cluster(market.get("question", ""), categorize_market(market.get("question", "")))
    cluster_count = open_clusters.get(cluster, 0)
    q2_pass = cluster_count < 2  # max 2 v jednom klastri (CORE+SANDBOX)
    q2_note = (f"Klaster '{cluster}': {cluster_count} otvorených pozícií"
               if q2_pass else f"PORUŠENIE: {cluster_count} pozícií v klastri '{cluster}', limit 2")

    # Q3: Liquidity & Exit Check
    # Pre asymmetric centovky (1-5¢) povoľujeme extreme price, lebo to je celá podstata Tier C
    is_asymmetric_centovka = (edge_type == "asymmetric")
    if is_asymmetric_centovka:
        spread_ok = isinstance(yes_price, (int, float)) and 0.01 <= yes_price <= 0.05
    else:
        spread_ok = isinstance(yes_price, (int, float)) and 0.05 <= yes_price <= 0.95
    liq_ok = liquidity >= APP_CONFIG["default_min_liquidity"]
    vol_ok = volume24 >= APP_CONFIG["default_min_volume24"]
    q3_pass = liq_ok and vol_ok and spread_ok
    q3_parts = []
    q3_parts.append(f"likvidita {int(liquidity):,} {'✓' if liq_ok else '✗'}")
    q3_parts.append(f"vol24h {int(volume24):,} {'✓' if vol_ok else '✗'}")
    q3_parts.append(f"cena {yes_price} {'✓' if spread_ok else '✗'}")
    q3_note = " · ".join(q3_parts)

    overall = q1_pass and q2_pass and q3_pass

    return {
        "q1_edge": {"pass": q1_pass, "note": q1_note},
        "q2_correlation": {"pass": q2_pass, "note": q2_note, "cluster": cluster},
        "q3_liquidity": {"pass": q3_pass, "note": q3_note},
        "overall_pass": overall,
        "kill_reason": (None if overall else
                        ("Q1 Edge" if not q1_pass else
                         ("Q2 Correlation" if not q2_pass else "Q3 Liquidity")))
    }


# ============================================================================
# TIER CLASSIFICATION (v2.0 PILLAR 2)
# ============================================================================

def classify_tier(edge_type, oracle_risk, liquidity, volume24, catalyst_confidence,
                  yes_price, no_price, side_hint, kill_switch_pass,
                  category=None, days_to_end=None):
    """
    Klasifikuje trh do Tier A / B / C / PASS.
    """
    if not kill_switch_pass:
        return {"tier": "PASS", "stake": 0.0, "reason": "Kill-Switch fail"}

    # HARD BLOCK: Šport — smerová lotéria
    if category == "Sports":
        return {"tier": "PASS", "stake": 0.0, "reason": "Sport = smerová lotéria, 1/N tímov"}

    if oracle_risk == "High":
        return {"tier": "PASS", "stake": 0.0, "reason": "High oracle risk"}

    # R/R sanity check — drahé NO má príliš slabý upside vs downside
    # NO @ 0.80 → R/R 0.25:1 (max zisk 0.20, max strata 0.80)
    # NO @ 0.90 → R/R 0.11:1 (max zisk 0.10, max strata 0.90)
    # Hranica 0.80: vyžaduje P(NO win) >= ~83% aby bol expected value pozitívny
    if (side_hint == "NO" and isinstance(no_price, (int, float)) and no_price >= 0.80
            and edge_type != "asymmetric"):
        return {
            "tier": "PASS",
            "stake": 0.0,
            "reason": f"NO @ {no_price:.3f} = R/R {(1 - no_price) / no_price:.2f}:1 — tenké, vyžaduje P>83%"
        }

    # R/R sanity check pre YES strane — drahé YES bez special edge
    if (side_hint == "YES" and isinstance(yes_price, (int, float)) and yes_price >= 0.85
            and edge_type not in ("text", "oracle")):
        return {
            "tier": "PASS",
            "stake": 0.0,
            "reason": f"YES @ {yes_price:.3f} = tenké R/R bez špecifického edge"
        }

    excellent_liq = liquidity >= 500000 and volume24 >= 100000
    good_liq = liquidity >= 250000 and volume24 >= 50000
    ok_liq = liquidity >= 75000 and volume24 >= 15000

    # Tier C — Asymmetric centovka (< 5¢), MAX 270 dní
    if edge_type == "asymmetric" and ok_liq:
        if days_to_end is not None and days_to_end > 270:
            return {"tier": "PASS", "stake": 0.0,
                    "reason": f"Asymmetric @ {yes_price:.3f}, ale {int(days_to_end)} dní = opportunity cost"}
        return {
            "tier": "C",
            "stake": APP_CONFIG["tier_c_stake_default"],
            "reason": f"Asymetrická centovka @ {yes_price:.3f} — Tier C SANDBOX"
        }

    # Tier A — clear text/oracle edge + strong catalyst + excellent liquidity
    if (edge_type in ("text", "oracle")
            and catalyst_confidence == "High"
            and excellent_liq
            and oracle_risk == "Low"):
        return {
            "tier": "A",
            "stake": APP_CONFIG["tier_a_stake"],
            "reason": "Text/Oracle edge + strong catalyst + excellent liquidity"
        }

    # Tier A fallback — time_decay + strong catalyst + excellent liquidity
    if (edge_type == "time_decay"
            and catalyst_confidence == "High"
            and excellent_liq
            and oracle_risk == "Low"):
        return {
            "tier": "A",
            "stake": APP_CONFIG["tier_a_stake"],
            "reason": "Time-decay edge + strong catalyst + excellent liquidity"
        }

    # Tier B — time-decay or structural edge + at least ok liquidity
    if edge_type in ("text", "oracle", "time_decay", "structural") and ok_liq:
        return {
            "tier": "B",
            "stake": APP_CONFIG["tier_b_stake"],
            "reason": f"{edge_type} edge + OK liquidity"
        }

    return {
        "tier": "PASS",
        "stake": 0.0,
        "reason": "Nedosahuje Tier A/B/C kritériá"
    }


# ============================================================================
# EXIT ENGINE (v2.0 PILLAR 3)
# ============================================================================

def build_exit_plan(tier_info, side, entry_price, days_to_end):
    """
    v2.0 Pillar 3: Mechanický exit.
    - Lacné (<25¢): okamžitý free-roll sell
    - Drahé (>40¢): mandatory time-stop dátum
    - Stredné (25-40¢): štandardné TP1/TP2
    """
    if tier_info["tier"] == "PASS" or entry_price is None:
        return {"plan_type": "none", "actions": []}

    stake = tier_info["stake"]
    shares_bought = round(stake / entry_price, 2) if entry_price > 0 else 0

    actions = []

    if entry_price < 0.25:
        # FREE-ROLL: predaj N shares na vytiahnutie 100% vkladu
        # Predávame za ask, ale rátame so symetrickou cenou
        target_sell_price = min(entry_price * 2.5, 0.50)  # konzervatívny target
        shares_to_sell = round(stake / target_sell_price, 2)

        # Free-roll je v ideále zadaný okamžite za cenu, ktorá pokryje vklad
        # Najlepšie: predaj X shares pri cene Y aby X*Y >= stake
        # Conservative: cieľ 2× entry price
        ideal_sell_price = clamp_price(entry_price * 2)
        ideal_sell_shares = round(stake / ideal_sell_price, 2) if ideal_sell_price > 0 else 0

        actions.append({
            "type": "FREE_ROLL",
            "priority": 1,
            "description": f"OKAMŽITE: limit SELL {ideal_sell_shares} shares @ {ideal_sell_price} (vyťahuje 100% vkladu)",
            "shares": ideal_sell_shares,
            "price": ideal_sell_price,
            "trigger": "ihneď po BUY fill",
        })
        actions.append({
            "type": "LET_RUN",
            "priority": 2,
            "description": f"Zvyšok {round(shares_bought - ideal_sell_shares, 2)} shares = free roll, nech beží do resolution alebo do 80¢",
            "trigger": "po fill free-roll selu",
        })

    elif entry_price > 0.40:
        # DRAHÉ: mandatory time-stop
        if days_to_end is None or days_to_end > 60:
            time_stop_days = 30
        elif days_to_end > 30:
            time_stop_days = max(7, int(days_to_end * 0.5))
        else:
            time_stop_days = max(3, int(days_to_end * 0.3))

        time_stop_date = add_days_iso(time_stop_days)

        tp1_price = clamp_price(entry_price + 0.08)
        tp2_price = clamp_price(entry_price + 0.15)

        actions.append({
            "type": "TIME_STOP",
            "priority": 1,
            "description": f"MANDATORY time-stop: {time_stop_date} (za {time_stop_days} dní). Vychádzaj do bid bez ohľadu na cenu.",
            "date": time_stop_date,
            "trigger": "absolútny deadline",
        })
        actions.append({
            "type": "TP1",
            "priority": 2,
            "description": f"TP1 @ {tp1_price}: predaj 50% pozície",
            "price": tp1_price,
            "size_pct": 50,
        })
        actions.append({
            "type": "TP2",
            "priority": 3,
            "description": f"TP2 @ {tp2_price}: predaj 30% pozície",
            "price": tp2_price,
            "size_pct": 30,
        })
        actions.append({
            "type": "RUNNER",
            "priority": 4,
            "description": f"Runner 20% drží do resolution, ale pred 98¢ vychádzaj (kapitál sa odblokuje skôr)",
            "size_pct": 20,
        })

    else:
        # STREDNÉ (25-40¢): štandardné TP1/TP2 bez time-stopu
        tp1_price = clamp_price(entry_price + 0.06)
        tp2_price = clamp_price(entry_price + 0.12)

        actions.append({
            "type": "TP1",
            "priority": 1,
            "description": f"TP1 @ {tp1_price}: predaj 40% pozície",
            "price": tp1_price,
            "size_pct": 40,
        })
        actions.append({
            "type": "TP2",
            "priority": 2,
            "description": f"TP2 @ {tp2_price}: predaj 40% pozície",
            "price": tp2_price,
            "size_pct": 40,
        })
        actions.append({
            "type": "RUNNER",
            "priority": 3,
            "description": "Runner 20% — close pred resolution",
            "size_pct": 20,
        })

    # Universal kill switches (každý plan)
    actions.append({
        "type": "INVALIDATION",
        "priority": 99,
        "description": "Okamžitý FULL EXIT: nová správa, ktorá ruší tézu / nový oracle risk / spread sa rozpadne",
        "trigger": "any-time",
    })
    actions.append({
        "type": "NO_AVERAGING",
        "priority": 99,
        "description": "ZÁKAZ averaging down. Ak cena padne 30%+, NEPRIDÁVAJ.",
        "trigger": "any-time",
    })

    return {
        "plan_type": ("free_roll" if entry_price < 0.25 else
                      ("time_stop" if entry_price > 0.40 else "standard")),
        "entry_price": entry_price,
        "shares_bought": shares_bought,
        "actions": actions,
    }


# ============================================================================
# DEVIL'S ADVOCATE (v2.0 PILLAR 4)
# ============================================================================

def generate_devils_advocate(market, edge_type, oracle_risk, category, side, yes_price,
                             days_to_end):
    """Auto-generuje najreálnejší scenár ako prísť o peniaze."""
    q = (market.get("question") or "").lower()

    scenarios = []

    # Oracle traps
    if oracle_risk in ("Medium", "High"):
        scenarios.append({
            "scenario": "Oracle trap",
            "probability": "15-25%",
            "description": "Resolution rules pripúšťajú interpretáciu opačnú od tvojho čítania. UMA dispute alebo niečo medzi.",
        })

    # Surprise announcements (politics/leadership)
    if any(k in q for k in ["out by", "resignation", "called by", "deal", "agreement", "signed"]):
        scenarios.append({
            "scenario": "Surprise announcement",
            "probability": "10-15%",
            "description": "Ohlásenie zo dňa na deň → market resolvne v opačnom smere ihneď. Nestihneš zareagovať.",
        })

    # Liquidity collapse
    if days_to_end and days_to_end < 14:
        scenarios.append({
            "scenario": "Liquidity collapse pred resolution",
            "probability": "20-30%",
            "description": "Spread sa otvorí na 5¢+, depth zmizne. Exit za fair value už nie je možný.",
        })

    # Time-decay reversal
    if edge_type == "time_decay":
        scenarios.append({
            "scenario": "Catalyst sa zmaterializuje rýchlejšie ako očakávané",
            "probability": "15-20%",
            "description": "Procedurálna inertia sa zlomí — politici sa dohodnú, summit prebehne, deadline sa stihne. Tvoj time-decay edge mizne.",
        })

    # Side-specific
    if side == "NO" and isinstance(yes_price, (int, float)) and yes_price < 0.30:
        scenarios.append({
            "scenario": "Outcome surprise (YES sa stane)",
            "probability": "5-15%",
            "description": f"NO @ {1 - yes_price:.2f} predpokladá, že YES sa nestane. Ak sa stane, strata = 100%.",
        })

    # Crypto-specific
    if category == "Crypto":
        scenarios.append({
            "scenario": "Cena vyskočí na catalyst (regulácia, ETF, hack)",
            "probability": "varies",
            "description": "Crypto trhy reagujú nelineárne. 20% move per deň je bežný.",
        })

    if not scenarios:
        scenarios.append({
            "scenario": "Náhodný flow proti tebe",
            "probability": "neznáma",
            "description": "Aj bez konkrétnej news môže trh ísť proti tebe. Defaultný risk = mispricing tézy.",
        })

    # Vyber najpravdepodobnejší
    main = scenarios[0]

    return {
        "main_scenario": main,
        "all_scenarios": scenarios,
        "advice": (
            "Pred kliknutím sa spýtaj: viem konkrétne, ako sa môžem mýliť? "
            "Ak nie, PASS. Ak áno, máš správny risk-aware setup."
        )
    }


# ============================================================================
# SCORING ENGINE — combines all v2.0 pillars
# ============================================================================

def score_market_v2(market, open_clusters=None):
    """Hlavná funkcia ktorá kombinuje všetky 4 piliere v2.0."""
    open_clusters = open_clusters or {}

    raw_question = market.get("question") or ""
    liquidity = to_float(market.get("liquidity"))
    volume24 = to_float(market.get("volume24hr"))
    yes_price, no_price = get_yes_no_prices(market)
    end_date = parse_date(market.get("endDate"))

    now = datetime.now(timezone.utc)
    days_to_end = None
    if end_date:
        days_to_end = (end_date - now).total_seconds() / 86400

    category = categorize_market(raw_question)
    edge_type, edge_reason = detect_edge_type(raw_question, days_to_end, yes_price)
    oracle_risk = oracle_risk_level(raw_question)
    catalyst_type, catalyst_confidence = detect_catalyst(raw_question, days_to_end)
    cluster = detect_cluster(raw_question, category)

    # Pillar 1: Kill-Switch
    ks = kill_switch_check(market, edge_type, edge_reason, liquidity, volume24,
                           yes_price, open_clusters)

    # Side selection FIRST — v2.0 logic with NO bias (~75% Polymarket markets resolve NO)
    if edge_type == "asymmetric":
        side = "YES"
        entry_price = yes_price
    elif edge_type in ("time_decay", "structural"):
        # NO bias — base rate Polymarket = 75% NO wins
        side = "NO"
        entry_price = no_price
    elif edge_type in ("text", "oracle"):
        # Pre text/oracle edge: default NO (text edge typicky exploituje slovíčko
        # ktoré spôsobí NO resolution aj keď udalosť "morálne" prebehla)
        side = "NO"
        entry_price = no_price
    elif isinstance(yes_price, (int, float)) and yes_price > 0.50:
        # Generic NO bias pre drahé YES
        side = "NO"
        entry_price = no_price
    else:
        # Fallback
        side = "NO"
        entry_price = no_price

    # Pillar 2: Tier (with side hint for R/R check)
    tier_info = classify_tier(edge_type, oracle_risk, liquidity, volume24,
                              catalyst_confidence, yes_price, no_price, side,
                              ks["overall_pass"],
                              category=category, days_to_end=days_to_end)

    # Pillar 3: Exit Plan
    exit_plan = build_exit_plan(tier_info, side, entry_price, days_to_end)

    # Pillar 4: Devil's Advocate
    devils = generate_devils_advocate(market, edge_type, oracle_risk, category,
                                      side, yes_price, days_to_end)

    final_decision = "PASS" if tier_info["tier"] == "PASS" else f"BUY {side}"

    return {
        # Market basics
        "question": raw_question,
        "slug": market.get("slug"),
        "yesPrice": yes_price,
        "noPrice": no_price,
        "liquidity": liquidity,
        "volume24": volume24,
        "daysToEnd": days_to_end,
        "endDate": market.get("endDate"),
        "category": category,
        "cluster": cluster,

        # Pillar 1
        "killSwitch": ks,

        # Pillar 2
        "tier": tier_info["tier"],
        "tierStake": tier_info["stake"],
        "tierReason": tier_info["reason"],

        # Edge analysis
        "edgeType": edge_type,
        "edgeReason": edge_reason,
        "oracleRisk": oracle_risk,
        "catalystType": catalyst_type,
        "catalystConfidence": catalyst_confidence,

        # Pillar 3
        "side": side,
        "entryPrice": entry_price,
        "exitPlan": exit_plan,

        # Pillar 4
        "devilsAdvocate": devils,

        # Final
        "finalDecision": final_decision,
    }


# ============================================================================
# DISCIPLINE LAYER — averaging-down + portfolio capacity
# ============================================================================

def get_open_positions_summary(wallet, min_value=1.0):
    """Spočíta otvorené pozície a expozíciu."""
    raw = fetch_positions(wallet) if wallet else []
    open_pos = []
    total_value = 0.0
    clusters_count = defaultdict(int)

    for item in raw:
        size = to_float(item.get("size") or item.get("shares"))
        value = to_float(item.get("value") or item.get("currentValue"))
        if size < 0.5 or value < min_value:
            continue
        title = item.get("title") or ""
        cluster = detect_cluster(title, categorize_market(title))
        clusters_count[cluster] += 1
        open_pos.append({
            "title": title,
            "outcome": item.get("outcome") or "",
            "size": round(size, 2),
            "value": round(value, 2),
            "avgPrice": safe_num_or_none(item.get("avgPrice") or item.get("averagePrice")),
            "currentPrice": safe_num_or_none(item.get("currentPrice") or item.get("price")),
            "cashPnl": safe_num_or_none(item.get("cashPnl") or item.get("pnl")),
            "slug": item.get("slug") or "",
            "cluster": cluster,
        })
        total_value += value

    return {
        "count": len(open_pos),
        "total_value_usdc": round(total_value, 2),
        "positions": open_pos,
        "clusters_count": dict(clusters_count),
    }


def check_averaging_down(wallet, market_slug, intended_side, intended_price):
    """Detekuje averaging-down vs. existujúce buys."""
    if not wallet or not market_slug or intended_price is None:
        return {"is_averaging_down": False, "reason": "Missing inputs", "has_prior_position": False}

    trades = fetch_wallet_trades_raw(wallet, limit=500)
    buys, sells = [], []
    for t in trades:
        t_slug = t.get("slug") or t.get("eventSlug") or ""
        if t_slug != market_slug:
            continue
        outcome = (t.get("outcome") or "").upper()
        if outcome != intended_side.upper():
            continue
        price = safe_num_or_none(t.get("price"))
        size = safe_num_or_none(t.get("size")) or 0
        if price is None or size <= 0:
            continue
        side = (t.get("side") or "").upper()
        if side == "BUY":
            buys.append({"price": price, "size": size})
        elif side == "SELL":
            sells.append({"price": price, "size": size})

    total_bought = sum(b["size"] for b in buys)
    total_sold = sum(s["size"] for s in sells)
    net_position = total_bought - total_sold

    if not buys or net_position <= 0.5:
        return {
            "is_averaging_down": False,
            "has_prior_position": False,
            "net_position": round(net_position, 2),
            "reason": "Žiadna otvorená pozícia v tomto markete + strane",
        }

    prior_avg = sum(b["price"] * b["size"] for b in buys) / total_bought
    drop_pct = round((prior_avg - intended_price) / prior_avg * 100, 1) if prior_avg > 0 else 0
    is_avg_down = intended_price < prior_avg * 0.95

    return {
        "is_averaging_down": is_avg_down,
        "has_prior_position": True,
        "net_position": round(net_position, 2),
        "prior_avg_price": round(prior_avg, 4),
        "prior_buy_count": len(buys),
        "intended_price": round(intended_price, 4),
        "drop_pct": drop_pct,
        "reason": (
            f"Net pozícia {round(net_position, 1)} sh @ avg {prior_avg:.3f}, "
            f"plánovaný vstup {intended_price:.3f} = {drop_pct}% nižšie"
            if is_avg_down else "Nie je averaging down"
        ),
    }


# ============================================================================
# ENDPOINTS
# ============================================================================

@app.route("/")
def home():
    return jsonify({
        "app": APP_CONFIG["title"],
        "version": APP_CONFIG["version"],
        "endpoints": [
            "/health",
            "/markets",
            "/analyze-market (POST)",
            "/pre-trade-check (POST)",
            "/portfolio-status?wallet=0x...",
            "/market-trades?slug=...",
            "/wallet-history?wallet=0x...",
            "/leaderboard",
            "/dashboard",
        ],
        "config": APP_CONFIG,
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "version": APP_CONFIG["version"]})


@app.route("/markets")
def markets():
    """Hlavný scanner kandidátov s v2.0 framework gating."""
    limit = safe_int(request.args.get("limit", "80"), 80)
    min_liquidity = to_float(request.args.get("min_liquidity",
                                              str(APP_CONFIG["default_min_liquidity"])))
    hide_pass = request.args.get("hide_pass", "true").lower() == "true"
    category_filter = request.args.get("category", "").strip()
    wallet = request.args.get("wallet", "").strip()

    raw_markets = fetch_active_markets(limit=500)

    # Open clusters from wallet (pre Correlation Check)
    open_clusters = {}
    portfolio_summary = None
    if wallet:
        portfolio_summary = get_open_positions_summary(wallet)
        open_clusters = portfolio_summary["clusters_count"]

    scored_list = []
    now = datetime.now(timezone.utc)
    for m in raw_markets:
        if m.get("active") is not True or m.get("closed") is True:
            continue
        if to_float(m.get("liquidity")) < min_liquidity:
            continue

        # Pre-filter: skip Sports (vždy PASS = strata času)
        if categorize_market(m.get("question", "")) == "Sports":
            continue

        # Pre-filter: skip markets with >270 days to end (opportunity cost)
        end = parse_date(m.get("endDate"))
        if end:
            days = (end - now).total_seconds() / 86400
            if days > 270:
                continue

        try:
            scored = score_market_v2(m, open_clusters=open_clusters)
        except Exception as e:
            continue

        if hide_pass and scored["tier"] == "PASS":
            continue
        if category_filter and scored["category"] != category_filter:
            continue

        scored_list.append(scored)

    # Sort: Tier A first, B, C, then PASS. Geopolitics+Politics priority.
    tier_order = {"A": 0, "B": 1, "C": 2, "PASS": 3}
    category_priority = {"Geopolitics": 0, "Politics": 0, "Crypto": 1,
                         "Other": 2, "Narrative": 3, "Sports": 9}
    scored_list.sort(key=lambda x: (
        tier_order.get(x["tier"], 99),
        category_priority.get(x.get("category", "Other"), 5),
        -to_float(x["liquidity"]),
    ))
    return jsonify({
        "count": len(scored_list[:limit]),
        "markets": scored_list[:limit],
        "portfolio": portfolio_summary,
        "filters": {
            "min_liquidity": min_liquidity,
            "hide_pass": hide_pass,
            "category": category_filter,
        },
    })


@app.route("/analyze-market", methods=["POST"])
def analyze_market_endpoint():
    """Analyze a specific market by slug or raw market data."""
    payload = request.get_json(force=True, silent=True) or {}
    slug = (payload.get("slug") or "").strip()
    wallet = (payload.get("wallet") or "").strip()

    if slug:
        market = fetch_market_by_slug(slug)
        if not market:
            return jsonify({"error": f"Market '{slug}' not found"}), 404
    elif payload.get("market"):
        market = payload["market"]
    else:
        return jsonify({"error": "Provide 'slug' or 'market' in body"}), 400

    open_clusters = {}
    if wallet:
        portfolio = get_open_positions_summary(wallet)
        open_clusters = portfolio["clusters_count"]

    return jsonify(score_market_v2(market, open_clusters=open_clusters))


@app.route("/pre-trade-check", methods=["POST"])
def pre_trade_check():
    """Hlavný gate. Mixed enforcement: hard blocks pre averaging+capacity, soft warn pre sizing."""
    payload = request.get_json(force=True, silent=True) or {}
    market_slug = (payload.get("market_slug") or "").strip()
    intended_side = (payload.get("intended_side") or "").upper().strip()
    intended_stake = to_float(payload.get("intended_stake_usdc"))
    intended_price = safe_num_or_none(payload.get("intended_price"))
    wallet = (payload.get("wallet") or "").strip()

    blocks, warnings = [], []

    if not market_slug or intended_side not in ("YES", "NO") or intended_stake <= 0:
        return jsonify({
            "verdict": "BLOCK",
            "blocks": ["Invalid input"],
            "warnings": [],
        }), 400

    market = fetch_market_by_slug(market_slug)
    if not market:
        return jsonify({
            "verdict": "BLOCK",
            "blocks": [f"Market '{market_slug}' nenájdený / zavretý"],
            "warnings": [],
        }), 404

    # Score market (with portfolio context for correlation check)
    open_clusters = {}
    portfolio = None
    if wallet:
        portfolio = get_open_positions_summary(wallet)
        open_clusters = portfolio["clusters_count"]

    scored = score_market_v2(market, open_clusters=open_clusters)

    if intended_price is None:
        intended_price = scored["yesPrice"] if intended_side == "YES" else scored["noPrice"]

    # === Engine decision check ===
    if scored["tier"] == "PASS":
        kill_reason = scored["killSwitch"]["kill_reason"]
        blocks.append(f"Engine PASS. Kill-Switch fail: {kill_reason}.")
    elif scored["finalDecision"] != f"BUY {intended_side}":
        blocks.append(f"Engine odporúča {scored['finalDecision']}, ty ideš BUY {intended_side}.")

    # === Sizing check (SOFT warning per user preference) ===
    sizing = {"intended": intended_stake, "recommended": scored["tierStake"], "ratio": None}
    if scored["tierStake"] > 0:
        ratio = intended_stake / scored["tierStake"]
        sizing["ratio"] = round(ratio, 2)
        if ratio > 2.0:
            warnings.append(f"SOFT WARN: oversize {ratio:.1f}× recommended ({intended_stake} vs {scored['tierStake']})")
        elif ratio > 1.3:
            warnings.append(f"Mierne oversize {ratio:.1f}×")
        elif ratio < 0.5:
            warnings.append(f"Undersize {ratio:.1f}× recommended")

    # === Portfolio capacity (HARD block) ===
    cap_check = {"current": 0, "max": APP_CONFIG["max_core_positions"], "exposure": 0,
                 "max_exposure": APP_CONFIG["max_total_exposure_usdc"]}
    if wallet and portfolio:
        cap_check["current"] = portfolio["count"]
        cap_check["exposure"] = portfolio["total_value_usdc"]

        # Check if user already holds this exact position (then it's add-on, not new slot)
        already = any(p.get("slug") == market_slug for p in portfolio["positions"])
        new_pos_count = portfolio["count"] + (0 if already else 1)
        new_exposure = portfolio["total_value_usdc"] + intended_stake

        max_pos = APP_CONFIG["max_core_positions"] + APP_CONFIG["max_sandbox_positions"]
        if not already and portfolio["count"] >= max_pos:
            blocks.append(f"HARD BLOCK: {portfolio['count']}/{max_pos} pozícií už otvorených")
        elif not already and new_pos_count > max_pos:
            blocks.append(f"HARD BLOCK: nový trade dá {new_pos_count}/{max_pos}")

        if new_exposure > APP_CONFIG["max_total_exposure_usdc"]:
            blocks.append(f"HARD BLOCK EXPOSURE: total {new_exposure:.0f} > limit {APP_CONFIG['max_total_exposure_usdc']:.0f} USDC")

    # === Averaging-down (HARD block) ===
    avg_down = {}
    if wallet:
        avg_down = check_averaging_down(wallet, market_slug, intended_side, intended_price or 0)
        if avg_down.get("is_averaging_down"):
            blocks.append(
                f"HARD BLOCK AVERAGING DOWN: {avg_down['prior_buy_count']} predošlých BUY @ avg "
                f"{avg_down['prior_avg_price']:.3f}, vstup o {avg_down['drop_pct']}% nižšie. ZÁKAZ v2.0."
            )

    verdict = "BLOCK" if blocks else ("WARN" if warnings else "APPROVED")

    return jsonify({
        "verdict": verdict,
        "blocks": blocks,
        "warnings": warnings,
        "scored": scored,
        "checks": {
            "sizing": sizing,
            "capacity": cap_check,
            "averaging_down": avg_down,
        },
        "intended": {
            "side": intended_side,
            "stake_usdc": intended_stake,
            "price": intended_price,
        },
    })


@app.route("/portfolio-status")
def portfolio_status():
    """Snapshot portfolio voči v2.0 limitom."""
    wallet = request.args.get("wallet", "").strip()
    if not wallet:
        return jsonify({
            "wallet": "",
            "framework": APP_CONFIG["version"],
            "limits": {
                "max_active_positions": APP_CONFIG["max_core_positions"] + APP_CONFIG["max_sandbox_positions"],
                "max_total_exposure_usdc": APP_CONFIG["max_total_exposure_usdc"],
                "bankroll_total_usdc": APP_CONFIG["bankroll_usdc"],
                "cash_reserve_usdc": APP_CONFIG["cash_reserve_usdc"],
            },
            "current": {"position_count": 0, "total_exposure_usdc": 0,
                        "available_slots": APP_CONFIG["max_core_positions"] + APP_CONFIG["max_sandbox_positions"],
                        "available_exposure_usdc": APP_CONFIG["max_total_exposure_usdc"]},
            "compliance": {"overall_ok": True},
            "positions": [],
            "error": None,
            "note": "Wallet nie je nastavená — portfolio monitoring vypnutý",
        })

    portfolio = get_open_positions_summary(wallet)
    max_pos = APP_CONFIG["max_core_positions"] + APP_CONFIG["max_sandbox_positions"]
    pos_ok = portfolio["count"] <= max_pos
    exp_ok = portfolio["total_value_usdc"] <= APP_CONFIG["max_total_exposure_usdc"]

    return jsonify({
        "wallet": short_wallet(wallet),
        "framework": APP_CONFIG["version"],
        "limits": {
            "max_active_positions": max_pos,
            "max_total_exposure_usdc": APP_CONFIG["max_total_exposure_usdc"],
            "bankroll_total_usdc": APP_CONFIG["bankroll_usdc"],
            "cash_reserve_usdc": APP_CONFIG["cash_reserve_usdc"],
        },
        "current": {
            "position_count": portfolio["count"],
            "total_exposure_usdc": portfolio["total_value_usdc"],
            "available_slots": max(0, max_pos - portfolio["count"]),
            "available_exposure_usdc": max(0, APP_CONFIG["max_total_exposure_usdc"] - portfolio["total_value_usdc"]),
        },
        "compliance": {
            "position_count_ok": pos_ok,
            "exposure_ok": exp_ok,
            "overall_ok": pos_ok and exp_ok,
        },
        "positions": portfolio["positions"],
        "clusters_count": portfolio["clusters_count"],
    })


@app.route("/market-trades")
def market_trades_endpoint():
    """Recent whale trades pre konkrétny market."""
    slug = request.args.get("slug", "").strip()
    limit = safe_int(request.args.get("limit", "20"), 20)
    min_amount = to_float(request.args.get("min_amount", str(APP_CONFIG["whale_trade_min_notional"])))

    if not slug:
        return jsonify({"count": 0, "trades": []})

    market = fetch_market_by_slug(slug)
    if not market:
        return jsonify({"count": 0, "trades": []})

    condition_ids = extract_market_condition_ids(market)
    raw_trades = fetch_market_trades_raw(condition_ids, limit=limit, min_amount=min_amount)
    trades = [normalize_trade(t) for t in raw_trades if to_float(t.get("price")) * to_float(t.get("size")) >= min_amount]

    return jsonify({
        "count": len(trades),
        "slug": slug,
        "trades": trades,
    })


@app.route("/wallet-history")
def wallet_history_endpoint():
    wallet = request.args.get("wallet", "").strip()
    limit = safe_int(request.args.get("limit", "30"), 30)

    if not wallet:
        return jsonify({"wallet": "", "trades": [], "positions": []})

    raw_trades = fetch_wallet_trades_raw(wallet, limit=limit)
    trades = [normalize_trade(t) for t in raw_trades]
    portfolio = get_open_positions_summary(wallet)

    return jsonify({
        "wallet": short_wallet(wallet),
        "trades": trades,
        "positions": portfolio["positions"],
        "count": len(trades),
    })


@app.route("/leaderboard")
def leaderboard_endpoint():
    limit = safe_int(request.args.get("limit", "10"), 10)
    raw = fetch_leaderboard_raw(limit=limit)
    leaders = []
    for item in raw[:limit]:
        wallet = item.get("wallet") or item.get("address") or item.get("proxyWallet") or ""
        leaders.append({
            "name": item.get("name") or item.get("username") or item.get("user") or short_wallet(wallet) or "unknown",
            "profit": item.get("profit") or item.get("pnl") or item.get("realized_pnl") or 0,
            "volume": item.get("volume") or item.get("trade_volume") or 0,
            "wallet": wallet,
            "walletShort": short_wallet(wallet),
        })
    return jsonify({"count": len(leaders), "leaders": leaders})


# ============================================================================
# DASHBOARD HTML/JS
# ============================================================================

DASHBOARD_HTML = r"""<!doctype html>
<html lang="sk">
<head>
  <meta charset="utf-8">
  <title>Polymarket Sniper v2.0</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0; padding: 16px;
           background: #f5f6f8; color: #1a1a1a; font-size: 14px; line-height: 1.5; }
    h1, h2, h3, h4 { margin: 0 0 8px 0; }
    h1 { font-size: 20px; }
    h2 { font-size: 16px; color: #444; }
    h3 { font-size: 14px; color: #555; }
    .container { max-width: 1400px; margin: 0 auto; }
    .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
    .header .meta { font-size: 12px; color: #888; }
    .card { background: #fff; padding: 14px 16px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.06);
            margin-bottom: 12px; }

    /* Portfolio status bar */
    .portfolio-bar { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
    .stat { background: #f9fafb; padding: 12px; border-radius: 6px; border: 1px solid #eee; }
    .stat-label { font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }
    .stat-value { font-size: 22px; font-weight: 700; margin-top: 4px; color: #1a1a1a; }
    .stat-sub { font-size: 11px; color: #666; margin-top: 2px; }
    .stat.alert { background: #fef3f2; border-color: #fcc; }
    .stat.warn { background: #fef9e7; border-color: #fde }
    .stat.ok { background: #ecfdf5; border-color: #bef0d8; }

    /* Wallet input row */
    .wallet-row { display: flex; gap: 8px; align-items: center; margin-top: 10px; }
    .wallet-row input { flex: 1; padding: 7px 10px; border: 1px solid #ddd; border-radius: 6px;
                        font: inherit; font-size: 12px; font-family: ui-monospace, monospace; }
    .wallet-row button { padding: 7px 14px; background: #1558d6; color: #fff; border: none;
                         border-radius: 6px; font-weight: 600; cursor: pointer; font-size: 12px; }
    .wallet-row button:hover { background: #134caa; }

    /* Positions table */
    .pos-table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 10px; }
    .pos-table th { background: #fafafa; padding: 6px 8px; text-align: left; font-weight: 600;
                    color: #555; border-bottom: 1px solid #eee; }
    .pos-table td { padding: 6px 8px; border-bottom: 1px solid #f0f0f0; }
    .pos-table tr:hover { background: #fafcff; }

    /* Filters */
    .filters { display: flex; flex-wrap: wrap; gap: 10px; align-items: end; margin-bottom: 12px; }
    .filter { display: flex; flex-direction: column; gap: 3px; }
    .filter label { font-size: 11px; color: #666; font-weight: 600; }
    .filter select, .filter input { padding: 6px 8px; border: 1px solid #ddd; border-radius: 6px; font: inherit; font-size: 13px; }
    .filter button { padding: 7px 14px; background: #1558d6; color: #fff; border: none;
                     border-radius: 6px; font-weight: 600; cursor: pointer; font-size: 13px; }

    /* Candidates table */
    .cand-table { width: 100%; border-collapse: collapse; font-size: 13px; }
    .cand-table th { background: #fafafa; padding: 8px 10px; text-align: left; font-weight: 600;
                     color: #555; border-bottom: 1px solid #eee; position: sticky; top: 0; }
    .cand-table td { padding: 8px 10px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
    .cand-table tr.clickable { cursor: pointer; }
    .cand-table tr.clickable:hover { background: #f8faff; }
    .cand-table tr.selected { background: #eff5ff; }

    /* Badges */
    .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px;
             font-weight: 700; white-space: nowrap; }
    .tier-A { background: #d1f5e8; color: #047857; }
    .tier-B { background: #dbeafe; color: #1d4ed8; }
    .tier-C { background: #fef3c7; color: #b45309; }
    .tier-PASS { background: #f3f4f6; color: #6b7280; }
    .edge-text { background: #e0e7ff; color: #4338ca; }
    .edge-oracle { background: #fce7f3; color: #be185d; }
    .edge-time_decay { background: #ccfbf1; color: #0f766e; }
    .edge-structural { background: #fef3c7; color: #92400e; }
    .edge-asymmetric { background: #f3e8ff; color: #7c3aed; }
    .edge-none { background: #f3f4f6; color: #6b7280; }

    .check-yes { color: #047857; font-weight: 700; }
    .check-no { color: #b91c1c; font-weight: 700; }

    /* Detail panel */
    .detail { background: #fff; border-radius: 8px; padding: 18px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);
              margin-top: 16px; display: none; }
    .detail.open { display: block; }
    .detail h2 { margin: 0 0 12px 0; font-size: 18px; }
    .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 12px; }
    .detail-section { background: #f9fafb; padding: 12px; border-radius: 6px; }
    .detail-section h4 { margin: 0 0 8px 0; font-size: 12px; text-transform: uppercase;
                          color: #666; letter-spacing: 0.5px; }
    .ks-row { padding: 6px 0; border-bottom: 1px solid #eee; font-size: 13px; }
    .ks-row:last-child { border-bottom: none; }
    .action-list { list-style: none; padding: 0; margin: 0; }
    .action-list li { padding: 6px 0; border-bottom: 1px solid #f0f0f0; font-size: 13px; }
    .action-list li:last-child { border-bottom: none; }
    .action-type { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 10px;
                    font-weight: 700; margin-right: 6px; background: #e5e7eb; color: #374151; }
    .at-FREE_ROLL { background: #d1f5e8; color: #047857; }
    .at-TIME_STOP { background: #fee2e2; color: #b91c1c; }
    .at-TP1, .at-TP2 { background: #dbeafe; color: #1d4ed8; }
    .at-RUNNER { background: #fef3c7; color: #b45309; }
    .at-INVALIDATION, .at-NO_AVERAGING { background: #f3f4f6; color: #6b7280; }

    /* Verdict badge */
    .verdict { padding: 12px; border-radius: 6px; font-weight: 700; text-align: center;
               margin: 12px 0; font-size: 15px; }
    .v-APPROVED { background: #d1f5e8; color: #047857; }
    .v-WARN { background: #fef3c7; color: #b45309; }
    .v-BLOCK { background: #fee2e2; color: #b91c1c; }
    .verdict-list { list-style: none; padding-left: 0; margin: 8px 0; font-size: 12px;
                    font-weight: 400; text-align: left; }
    .verdict-list li { padding: 3px 0; }

    /* Execute button */
    .execute-row { display: flex; gap: 10px; align-items: center; margin-top: 12px;
                   padding-top: 12px; border-top: 1px solid #eee; }
    .execute-row input { padding: 8px 10px; border: 1px solid #ddd; border-radius: 6px;
                          font: inherit; width: 110px; }
    .execute-btn { padding: 10px 24px; border: none; border-radius: 6px; font-weight: 700;
                   font-size: 14px; cursor: pointer; }
    .execute-btn.enabled { background: #047857; color: #fff; }
    .execute-btn.enabled:hover { background: #036649; }
    .execute-btn.disabled { background: #d1d5db; color: #6b7280; cursor: not-allowed; }

    /* Side panels */
    .side-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; margin-top: 16px; }
    .side-panel h3 { font-size: 13px; color: #666; margin-bottom: 8px; }
    .compact-list { display: grid; gap: 4px; font-size: 12px; }
    .compact-list .row { display: grid; grid-template-columns: 1fr auto; gap: 6px; padding: 4px 0;
                         border-bottom: 1px solid #f0f0f0; }
    .compact-list .row:last-child { border-bottom: none; }

    /* Loading */
    .loading { text-align: center; padding: 30px; color: #888; }
    .spin { display: inline-block; width: 16px; height: 16px; border: 2px solid #e5e7eb;
            border-top-color: #1558d6; border-radius: 50%; animation: spin 0.8s linear infinite;
            vertical-align: middle; }
    @keyframes spin { to { transform: rotate(360deg); } }

    .small { font-size: 11px; color: #888; }
    .muted { color: #888; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }

    @media (max-width: 1000px) {
      .portfolio-bar { grid-template-columns: 1fr 1fr; }
      .detail-grid { grid-template-columns: 1fr; }
      .side-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div>
        <h1>Polymarket Sniper v2.0</h1>
        <div class="meta">Framework: Lean & Mean · Bankroll: 500 USDC · Max 3 CORE + 4 SANDBOX · <span id="lastUpdate"></span></div>
      </div>
      <div>
        <button onclick="loadAll()" style="padding: 8px 16px; border: 1px solid #1558d6; background: #fff; color: #1558d6; border-radius: 6px; font-weight: 600; cursor: pointer;">↻ Obnoviť</button>
      </div>
    </div>

    

    <!-- FILTERS -->
    <div class="card">
      <h2>Kandidáti</h2>
      <div class="filters">
        <div class="filter">
          <label>Kategória</label>
          <select id="categoryFilter">
            <option value="">Všetko</option>
            <option value="Politics">Politika</option>
            <option value="Geopolitics">Geopolitika</option>
            <option value="Crypto">Krypto</option>
            <option value="Sports">Šport</option>
            <option value="Other">Ostatné</option>
          </select>
        </div>
        <div class="filter">
          <label>Min likvidita</label>
          <select id="minLiquidity">
            <option value="50000">50 000 (tenké)</option>
            <option value="75000" selected>75 000 (default)</option>
            <option value="100000">100 000 (safe)</option>
            <option value="150000">150 000 (prísne)</option>
            <option value="250000">250 000 (Tier A)</option>
            <option value="500000">500 000 (excellent)</option>
          </select>
        </div>
        <div class="filter">
          <label style="visibility:hidden">.</label>
          <label style="display:flex;align-items:center;gap:6px;font-weight:400;">
            <input type="checkbox" id="hidePass" checked /> Skryť PASS
          </label>
        </div>
        <div class="filter">
          <label style="visibility:hidden">.</label>
          <button onclick="loadMarkets()">Aplikuj</button>
        </div>
      </div>

      <div id="candidatesBox">
        <div class="loading"><div class="spin"></div> Načítavam kandidátov...</div>
      </div>
    </div>

    <!-- DETAIL PANEL -->
    <div class="detail" id="detailPanel">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;">
        <h2 id="detailTitle"></h2>
        <button onclick="closeDetail()" style="background:none;border:none;font-size:18px;cursor:pointer;color:#888;">✕</button>
      </div>
      <div id="detailContent"><div class="loading"><div class="spin"></div> Analyzujem...</div></div>
    </div>

    <!-- SIDE PANELS -->
    <div class="side-grid">
      <div class="card side-panel">
        <h3>📈 Watchlist (Tier A/B kandidáti)</h3>
        <div id="watchlistBox" class="compact-list"><div class="muted small">Načítavam...</div></div>
      </div>
      <div class="card side-panel">
        <h3>🐋 Whale flow (vybraný market)</h3>
        <div id="whaleBox" class="compact-list"><div class="muted small">Klikni na market v tabuľke</div></div>
      </div>
      <div class="card side-panel">
        <h3>🏆 Leaderboard</h3>
        <div id="leaderboardBox" class="compact-list"><div class="muted small">Načítavam...</div></div>
      </div>
    </div>

    <!-- ALERTS -->
    <div class="card" id="alertsCard" style="display:none;">
      <h3>⚠️ Alerty</h3>
      <div id="alertsBox"></div>
    </div>
  </div>

<script>
// ============================================================================
// State
// ============================================================================
let walletAddr = localStorage.getItem('polywallet') || '';
let cachedMarkets = [];
let cachedPortfolio = null;
let selectedMarket = null;
let lastCheckResult = null;

// ============================================================================
// Boot
// ============================================================================
document.addEventListener('DOMContentLoaded', () => {
  loadAll();
});

async function loadAll() {
  document.getElementById('lastUpdate').textContent = 'Načítavam...';
  await Promise.all([
    loadMarkets(),
    loadLeaderboard(),
  ]);
  document.getElementById('lastUpdate').textContent = 'Aktualizované ' + new Date().toLocaleTimeString('sk-SK');
}

function setWallet() {
  walletAddr = document.getElementById('walletInput').value.trim();
  localStorage.setItem('polywallet', walletAddr);
  loadAll();
}

// ============================================================================
// Portfolio
// ============================================================================

// ============================================================================
// Candidates
// ============================================================================
async function loadMarkets() {
  const cat = document.getElementById('categoryFilter').value;
  const minLiq = document.getElementById('minLiquidity').value;
  const hidePass = document.getElementById('hidePass').checked;

  let url = '/markets?min_liquidity=' + minLiq + '&hide_pass=' + hidePass;
  if (cat) url += '&category=' + cat;
  if (walletAddr) url += '&wallet=' + encodeURIComponent(walletAddr);

  document.getElementById('candidatesBox').innerHTML =
    '<div class="loading"><div class="spin"></div> Načítavam kandidátov...</div>';

  try {
    const r = await fetch(url);
    const data = await r.json();
    cachedMarkets = data.markets || [];
    renderCandidates(cachedMarkets);
    renderWatchlist(cachedMarkets);
  } catch (err) {
    document.getElementById('candidatesBox').innerHTML =
      '<div class="loading">Chyba: ' + err.message + '</div>';
  }
}

function renderCandidates(markets) {
  if (!markets || markets.length === 0) {
    document.getElementById('candidatesBox').innerHTML =
      '<p class="muted">Žiadni kandidáti pre tieto filtre.</p>';
    return;
  }

  let html = '<table class="cand-table"><thead><tr>' +
             '<th>Tier</th><th>Edge</th><th>Otázka</th><th>YES</th><th>NO</th>' +
             '<th>Dni</th><th>Vol 24h</th><th>Likvidita</th><th>Oracle</th>' +
             '</tr></thead><tbody>';
  markets.forEach((m, idx) => {
    const tierCls = 'tier-' + m.tier;
    const edgeCls = m.edgeType ? 'edge-' + m.edgeType : 'edge-none';
    const edgeText = m.edgeType || '—';
    html += '<tr class="clickable" onclick="selectMarket(' + idx + ')">' +
            '<td><span class="badge ' + tierCls + '">' + m.tier + '</span></td>' +
            '<td><span class="badge ' + edgeCls + '">' + edgeText + '</span></td>' +
            '<td>' + escapeHtml(m.question || '').substring(0, 75) + '</td>' +
            '<td class="mono">' + fmtNum(m.yesPrice, 3) + '</td>' +
            '<td class="mono">' + fmtNum(m.noPrice, 3) + '</td>' +
            '<td>' + Math.round(m.daysToEnd || 0) + '</td>' +
            '<td>' + Math.round(m.volume24 || 0).toLocaleString('sk-SK') + '</td>' +
            '<td>' + Math.round(m.liquidity || 0).toLocaleString('sk-SK') + '</td>' +
            '<td class="' + (m.oracleRisk === 'High' ? 'check-no' : m.oracleRisk === 'Medium' ? '' : 'check-yes') + '">' +
              m.oracleRisk + '</td>' +
            '</tr>';
  });
  html += '</tbody></table>';
  document.getElementById('candidatesBox').innerHTML = html;
}

function renderWatchlist(markets) {
  const top = markets.filter(m => m.tier === 'A' || m.tier === 'B').slice(0, 5);
  const box = document.getElementById('watchlistBox');
  if (top.length === 0) {
    box.innerHTML = '<div class="muted small">Žiadni A/B kandidáti</div>';
    return;
  }
  box.innerHTML = top.map((m, idx) => {
    const realIdx = markets.indexOf(m);
    return '<div class="row" onclick="selectMarket(' + realIdx + ')" style="cursor:pointer;">' +
           '<div><strong>' + escapeHtml(m.question.substring(0, 45)) + '</strong><br>' +
           '<span class="small muted">' + m.side + ' @ ' + fmtNum(m.entryPrice, 3) + '</span></div>' +
           '<div><span class="badge tier-' + m.tier + '">' + m.tier + '</span></div>' +
           '</div>';
  }).join('');
}

// ============================================================================
// Detail panel
// ============================================================================
function selectMarket(idx) {
  selectedMarket = cachedMarkets[idx];
  if (!selectedMarket) return;

  // Highlight selected row
  document.querySelectorAll('.cand-table tr').forEach(tr => tr.classList.remove('selected'));
  document.querySelectorAll('.cand-table tr.clickable')[idx]?.classList.add('selected');

  const panel = document.getElementById('detailPanel');
  panel.classList.add('open');
  document.getElementById('detailTitle').textContent = selectedMarket.question;
  document.getElementById('detailContent').innerHTML =
    '<div class="loading"><div class="spin"></div> Analyzujem + volám pre-trade-check...</div>';

  renderDetail(selectedMarket);
  loadWhaleFlow(selectedMarket.slug);
  autoPreTradeCheck(selectedMarket);

  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function closeDetail() {
  document.getElementById('detailPanel').classList.remove('open');
  selectedMarket = null;
}

function renderDetail(m) {
  const ks = m.killSwitch || {};
  const ep = m.exitPlan || {};
  const da = m.devilsAdvocate || {};

  let html = '<div class="detail-grid">';

  // Kill-Switch
  html += '<div class="detail-section"><h4>🔒 Pillar 1: Kill-Switch</h4>';
  ['q1_edge', 'q2_correlation', 'q3_liquidity'].forEach(k => {
    const q = ks[k] || {};
    const cls = q.pass ? 'check-yes' : 'check-no';
    const sym = q.pass ? '✓' : '✗';
    const label = {q1_edge: 'Q1 Edge', q2_correlation: 'Q2 Correlation', q3_liquidity: 'Q3 Liquidity'}[k];
    html += '<div class="ks-row"><span class="' + cls + '">' + sym + '</span> <strong>' + label +
            ':</strong> <span class="small">' + escapeHtml(q.note || '') + '</span></div>';
  });
  if (!ks.overall_pass) {
    html += '<div style="margin-top:8px;padding:8px;background:#fee2e2;border-radius:4px;font-size:12px;">' +
            '<strong>PASS</strong>: Kill-Switch zlyhal na ' + escapeHtml(ks.kill_reason || '') + '</div>';
  }
  html += '</div>';

  // Tier
  html += '<div class="detail-section"><h4>📊 Pillar 2: Tier & Sizing</h4>';
  html += '<div style="font-size:24px;font-weight:700;"><span class="badge tier-' + m.tier + '">' + m.tier + '</span></div>';
  if (m.tier !== 'PASS') {
    html += '<div style="margin-top:6px;"><strong>' + m.tierStake + ' USDC</strong> · ' +
            '<strong>BUY ' + m.side + '</strong> @ ' + fmtNum(m.entryPrice, 3) + '</div>';
  }
  html += '<div class="small muted" style="margin-top:6px;">' + escapeHtml(m.tierReason || '') + '</div>';
  html += '<div style="margin-top:8px;font-size:12px;"><strong>Edge:</strong> ' + (m.edgeType || '—') + '<br>' +
          '<strong>Catalyst:</strong> ' + (m.catalystType || '—') + ' (' + (m.catalystConfidence || '—') + ')<br>' +
          '<strong>Oracle risk:</strong> ' + (m.oracleRisk || '—') + '</div>';
  html += '</div>';

  // Exit Plan
  html += '<div class="detail-section"><h4>🚪 Pillar 3: Exit Plan';
  if (ep.plan_type) html += ' <span class="badge tier-B" style="font-size:9px;">' + ep.plan_type + '</span>';
  html += '</h4>';
  if (m.tier === 'PASS') {
    html += '<div class="muted">Žiadny plán (PASS).</div>';
  } else if (ep.actions && ep.actions.length) {
    html += '<ul class="action-list">';
    ep.actions.forEach(a => {
      html += '<li><span class="action-type at-' + (a.type || '') + '">' + (a.type || '') + '</span>' +
              escapeHtml(a.description || '') + '</li>';
    });
    html += '</ul>';
  }
  html += '</div>';

  // Devil's Advocate
  html += '<div class="detail-section"><h4>😈 Pillar 4: Devil\'s Advocate</h4>';
  const main = da.main_scenario || {};
  html += '<div><strong>' + escapeHtml(main.scenario || '—') + '</strong> ';
  if (main.probability) html += '<span class="small muted">(' + escapeHtml(main.probability) + ')</span>';
  html += '<div style="font-size:13px;margin-top:6px;">' + escapeHtml(main.description || '') + '</div></div>';
  if (da.advice) html += '<div class="small muted" style="margin-top:8px;font-style:italic;">' + escapeHtml(da.advice) + '</div>';
  html += '</div>';

  html += '</div>';

  // Pre-trade-check verdict slot
  html += '<div id="preTradeVerdict"><div class="loading"><div class="spin"></div> Spúšťam pre-trade-check...</div></div>';

  // Execute row
  html += '<div class="execute-row">' +
          '<label class="small">Stake (USDC):</label>' +
          '<input id="execStake" type="number" min="1" max="500" value="' + (m.tierStake || 15) + '" step="1" />' +
          '<button id="execBtn" class="execute-btn disabled" onclick="onExecute()" disabled>EXECUTE</button>' +
          '<span class="small muted" id="execHint">Čaká na pre-trade-check...</span>' +
          '</div>';

  document.getElementById('detailContent').innerHTML = html;
}

async function autoPreTradeCheck(m) {
  if (m.tier === 'PASS') {
    document.getElementById('preTradeVerdict').innerHTML =
      '<div class="verdict v-BLOCK">VERDIKT: PASS (engine)<ul class="verdict-list"><li>' +
      escapeHtml(m.killSwitch?.kill_reason || 'Tier PASS') + '</li></ul></div>';
    return;
  }

  const payload = {
    market_slug: m.slug,
    intended_side: m.side,
    intended_stake_usdc: m.tierStake,
    intended_price: m.entryPrice,
    wallet: walletAddr,
  };

  try {
    const r = await fetch('/pre-trade-check', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    lastCheckResult = data;
    renderVerdict(data);
  } catch (err) {
    document.getElementById('preTradeVerdict').innerHTML =
      '<div class="verdict v-BLOCK">Chyba pri pre-trade-check: ' + err.message + '</div>';
  }
}

function renderVerdict(data) {
  const v = data.verdict || 'BLOCK';
  const vCls = 'v-' + v;
  let html = '<div class="verdict ' + vCls + '">VERDIKT: ' + v;

  if (data.blocks && data.blocks.length) {
    html += '<ul class="verdict-list">';
    data.blocks.forEach(b => { html += '<li>🚫 ' + escapeHtml(b) + '</li>'; });
    html += '</ul>';
  }
  if (data.warnings && data.warnings.length) {
    html += '<ul class="verdict-list">';
    data.warnings.forEach(w => { html += '<li>⚠️ ' + escapeHtml(w) + '</li>'; });
    html += '</ul>';
  }
  if (v === 'APPROVED') {
    html += '<div class="small" style="margin-top:6px;font-weight:400;">Všetky checky prešli. Framework v2.0 povoľuje vstup.</div>';
  }
  html += '</div>';

  document.getElementById('preTradeVerdict').innerHTML = html;

  // Update Execute button
  const btn = document.getElementById('execBtn');
  const hint = document.getElementById('execHint');
  if (v === 'BLOCK') {
    btn.classList.remove('enabled'); btn.classList.add('disabled'); btn.disabled = true;
    hint.textContent = 'Hard block — execute zakázané';
  } else if (v === 'WARN') {
    btn.classList.remove('disabled'); btn.classList.add('enabled'); btn.disabled = false;
    hint.textContent = 'Warning — execute povolené, ale s opatrnosťou';
  } else {
    btn.classList.remove('disabled'); btn.classList.add('enabled'); btn.disabled = false;
    hint.textContent = 'Approved — zelená';
  }
}

function onExecute() {
  if (!selectedMarket || !lastCheckResult) return;
  if (lastCheckResult.verdict === 'BLOCK') {
    alert('BLOCK: nemôžeš execute. Skontroluj verdict.');
    return;
  }
  const stake = document.getElementById('execStake').value;
  const slug = selectedMarket.slug;
  const url = 'https://polymarket.com/event/' + slug;
  // Open in new tab — user manuálne klikne BUY na Polymarketе
  alert('OK. Otváram Polymarket v novom tabe.\nStrana: ' + selectedMarket.side + '\nStake: ' + stake + ' USDC\n\nPotvrď BUY priamo na Polymarketе.');
  window.open(url, '_blank');
}

// ============================================================================
// Whale flow
// ============================================================================
async function loadWhaleFlow(slug) {
  const box = document.getElementById('whaleBox');
  box.innerHTML = '<div class="muted small"><div class="spin"></div> Načítavam...</div>';
  try {
    const r = await fetch('/market-trades?slug=' + encodeURIComponent(slug) + '&limit=15');
    const data = await r.json();
    if (!data.trades || data.trades.length === 0) {
      box.innerHTML = '<div class="muted small">Žiadne whale trades pre tento market (>200k notional)</div>';
      return;
    }
    box.innerHTML = data.trades.slice(0, 8).map(t => {
      const sideCls = (t.side === 'BUY') ? 'check-yes' : 'check-no';
      return '<div class="row"><div>' +
             '<span class="' + sideCls + '"><strong>' + t.side + '</strong></span> ' +
             escapeHtml(t.outcome || '') + ' @ ' + fmtNum(t.price, 3) +
             '<br><span class="small muted mono">' + escapeHtml(t.walletShort || '') + '</span>' +
             '</div><div class="mono">$' + fmtNum(t.notional, 0) + '</div></div>';
    }).join('');
  } catch (err) {
    box.innerHTML = '<div class="muted small">Chyba: ' + err.message + '</div>';
  }
}

// ============================================================================
// Leaderboard
// ============================================================================
async function loadLeaderboard() {
  const box = document.getElementById('leaderboardBox');
  try {
    const r = await fetch('/leaderboard?limit=8');
    const data = await r.json();
    if (!data.leaders || data.leaders.length === 0) {
      box.innerHTML = '<div class="muted small">Leaderboard sa nepodarilo načítať</div>';
      return;
    }
    box.innerHTML = data.leaders.slice(0, 6).map((l, i) => {
      const p = Number(l.profit) || 0;
      const pCls = p >= 0 ? 'check-yes' : 'check-no';
      return '<div class="row"><div><strong>' + (i + 1) + '.</strong> ' + escapeHtml(l.name || 'unknown') +
             '<br><span class="small muted mono">' + escapeHtml(l.walletShort || '') + '</span></div>' +
             '<div class="' + pCls + '">' + (p >= 0 ? '+' : '') + Math.round(p).toLocaleString('sk-SK') + '</div></div>';
    }).join('');
  } catch (err) {
    box.innerHTML = '<div class="muted small">Chyba: ' + err.message + '</div>';
  }
}

// ============================================================================
// Utils
// ============================================================================
function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function fmtNum(v, decimals = 2) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString('sk-SK', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}
</script>
</body>
</html>"""


@app.route("/dashboard")
def dashboard():
    return DASHBOARD_HTML


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
