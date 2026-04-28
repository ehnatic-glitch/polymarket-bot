from flask import Flask, jsonify, request
import requests
import json
import os
import re
import time
import threading
from collections import defaultdict
from datetime import datetime, timezone

app = Flask(__name__)

# ---- jednoduchá in-memory cache (per-process, stačí pre Render single dyno) ----
_CACHE = {}
_CACHE_LOCK = threading.Lock()

def cache_get(namespace, key):
    full_key = (namespace, key)
    now = time.time()
    with _CACHE_LOCK:
        entry = _CACHE.get(full_key)
        if entry and entry["expires"] > now:
            return entry["value"]
        if entry:
            _CACHE.pop(full_key, None)
    return None

def cache_set(namespace, key, value, ttl=60):
    full_key = (namespace, key)
    with _CACHE_LOCK:
        _CACHE[full_key] = {"value": value, "expires": time.time() + ttl}

def cache_invalidate(namespace=None):
    with _CACHE_LOCK:
        if namespace is None:
            _CACHE.clear()
        else:
            for k in list(_CACHE.keys()):
                if k[0] == namespace:
                    _CACHE.pop(k, None)

# ---- JSON persistence (alerts, watchlist snapshot, pnl log) ----
STATE_DIR = os.environ.get("STATE_DIR", "/tmp/polymarket_state")
os.makedirs(STATE_DIR, exist_ok=True)
STATE_FILE = os.path.join(STATE_DIR, "state.json")
PNL_LOG_FILE = os.path.join(STATE_DIR, "pnl_log.jsonl")
_STATE_LOCK = threading.Lock()

def load_state():
    with _STATE_LOCK:
        if not os.path.exists(STATE_FILE):
            return {"alerts": [], "watchlistSnapshot": [], "updatedAt": None}
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"alerts": [], "watchlistSnapshot": [], "updatedAt": None}

def save_state(state):
    state["updatedAt"] = datetime.now(timezone.utc).isoformat()
    with _STATE_LOCK:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

def append_pnl_log(entry):
    """Append-only PnL log. PDF v7 sekcia 12: pri stake <= 5 USDC stačí skrátený format.

    Skrátený log obsahuje len: ts, kind, slug, question, side, price, usdc, pnl, decision.
    Plný log obsahuje celý payload (včitane ex. plan, checklist, etc.).
    """
    entry["loggedAt"] = datetime.now(timezone.utc).isoformat()

    # Detekuj stake (rozne názvy: usdc / size / stakeUSDC)
    stake = 0.0
    for key in ("usdc", "stakeUSDC", "stake", "size"):
        v = entry.get(key)
        if isinstance(v, (int, float)) and v > 0:
            stake = float(v)
            break

    if 0 < stake <= 5.0:
        # Skrátený format pre testovacie sizingy
        compact = {
            "loggedAt": entry["loggedAt"],
            "logFormat": "short",
            "kind": entry.get("kind"),
            "slug": entry.get("slug"),
            "question": entry.get("question"),
            "side": entry.get("side"),
            "price": entry.get("price"),
            "usdc": stake,
            "pnl": entry.get("pnl"),
            "decision": entry.get("decision") or entry.get("finalDecision"),
        }
        payload = compact
    else:
        entry["logFormat"] = entry.get("logFormat") or "full"
        payload = entry

    with _STATE_LOCK:
        try:
            with open(PNL_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

def read_pnl_log(limit=100):
    with _STATE_LOCK:
        if not os.path.exists(PNL_LOG_FILE):
            return []
        try:
            with open(PNL_LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
            return [json.loads(line) for line in lines if line.strip()]
        except Exception:
            return []

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"

# === v7.0 SNIPER CONFIG ===
APP_CONFIG = {
    "dashboard_title": "Polymarket Sniper v7.0",
    "default_min_liquidity": 100000.0,
    "system_version": "v7.0",
    # Bankroll a risk limity (v7)
    "bankroll_total": 500.0,
    "cash_reserve": 150.0,            # 30% nedotknuteľná rezerva
    "max_total_exposure": 200.0,      # 40% bankrollu
    "max_narrative_exposure": 100.0,  # 20% na 1 naraťív
    "max_active_positions": 4,        # 3–4, 4. iba ak prvé 3 small
    "daily_drawdown_limit_pct": 0.15,
    "loss_streak_pause": 3,
    # Edge prahy v percentuálnych bodoch (after-cost)
    # PDF v7 sekcia 10: HARD prah Momentum/Time Decay = 10pp, Resolution = 15pp
    "edge_pp_momentum_soft": 8.0,        # PDF sekcia 2 mantinel
    "edge_pp_momentum": 10.0,            # HARD prah pre Momentum (sekcia 10)
    "edge_pp_time_decay": 10.0,          # HARD prah Time Decay
    "edge_pp_resolution": 15.0,          # HARD prah Resolution/Dispute
    "edge_pp_min_after_friction": 4.0,   # spread+fees+sklz nesmú zožrať pod 4pp
    # Sizing tiery v7 (USDC)
    "sizing_centovka": (5, 10),
    "sizing_standard": (10, 18),       # 10-12pp edge
    "sizing_strong": (20, 35),         # 15+pp edge
    "sizing_extreme": (50, 60),        # extrémy, max 1 naraťív
    # Time window v7
    "preferred_days_min": 2,
    "preferred_days_max": 60,
    "far_expiry_days": 90,
    "catalyst_proximity_days": 45,
    # Whale flow
    "whale_trade_min_notional": 200000.0,
    "whale_wallet_recent_sum": 500000.0,
}


@app.route("/")
def home():
    return jsonify({
        "message": "Polymarket read-only app is running",
        "endpoints": [
            "/health",
            "/markets",
            "/markets/top",
            "/dashboard",
            "/analyze-market",
            "/leaderboard",
            "/market-trades",
            "/wallet-history",
        ],
        "config": APP_CONFIG,
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


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


def safe_num_or_none(value):
    try:
        if value is None or value == "":
            return None
        n = float(value)
        if n != n:
            return None
        return n
    except Exception:
        return None


def short_wallet(addr):
    if not addr or not isinstance(addr, str):
        return ""
    if len(addr) < 12:
        return addr
    return f"{addr[:6]}...{addr[-4:]}"


def format_ts(ts):
    try:
        ts = int(float(ts))
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return None


def get_yes_no_prices(market):
    prices = parse_json_list(market.get("outcomePrices"))
    yes_price = None
    no_price = None

    if len(prices) >= 2:
        yes_price = safe_num_or_none(prices[0])
        no_price = safe_num_or_none(prices[1])
    elif len(prices) == 1:
        yes_price = safe_num_or_none(prices[0])
        no_price = (1 - yes_price) if isinstance(yes_price, (int, float)) else None
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


def sk_category(value):
    mapping = {
        "Sports": "Šport",
        "Politics": "Politika",
        "Crypto": "Krypto",
        "Geopolitics": "Geopolitika",
        "Narrative": "Naratív",
        "Other": "Ostatné",
    }
    return mapping.get(value, value or "Ostatné")


def sk_trade_type(value):
    mapping = {
        "Momentum": "Momentum",
        "Time Decay": "Časový rozpad",
        "Resolution": "Resolution / spor",
        "Centovka": "Centovka",
        "Other": "Ostatné",
    }
    return mapping.get(value, value or "Ostatné")


def sk_oracle_risk(value):
    mapping = {
        "Low": "Nízke",
        "Medium": "Stredné",
        "High": "Vysoké",
    }
    return mapping.get(value, value or "")


def sk_friction_label(value):
    mapping = {
        "Low friction": "Nízka frikcia",
        "Manageable": "Zvládnuteľná",
        "Medium": "Stredná",
        "High friction": "Vysoká frikcia",
    }
    return mapping.get(value, value or "")


def sk_exit_label(value):
    mapping = {
        "Good exit": "Dobrý exit",
        "Okay exit": "Priemerný exit",
        "Weak exit": "Slabý exit",
    }
    return mapping.get(value, value or "")


def sk_catalyst_type(value):
    mapping = {
        "Vote/Election": "Voľby / hlasovanie",
        "Deadline": "Deadline",
        "Report/Announcement": "Report / oznámenie",
        "Scheduled event": "Naplánovaná udalosť",
        "Near expiry": "Blízko expirácie",
        "Unclear": "Nejasný",
    }
    return mapping.get(value, value or "")


def categorize_market(question):
    q = (question or "").lower()

    sports_keywords = [
        # leagues / events
        "world cup", "nba finals", "nfl", "mlb", "stanley cup",
        "champions league", "premier league", "ufc", "fifa",
        "super bowl", "world series", "nhl", "nascar", "f1", "formula 1",
        "tennis", "atp", "wta", "golf", "pga", "masters", "open champ",
        "esports", "counter-strike", "valorant", "league of legends", "lol world",
        "olympics", "euroleague", "bundesliga", "la liga", "serie a", "mls",
        "copa america", "euro 2028", "afcon", "asian cup",
        # generic event phrasings (športové — confer/cup/finals sú typické pre sport)
        "win the finals", "win the world cup", "win the western confer",
        "win the eastern confer", "win the conference",
        "win the western", "win the eastern", "win the playoffs",
        "win the divisional", "win the wild card",
        # team-name proxies (covers individual team markets)
        "lakers", "celtics", "knicks", "warriors", "heat", "nuggets",
        "thunder", "raptors", "magic", "pistons", "bucks", "76ers",
        "chiefs", "eagles", "cowboys", "49ers",
        "manchester united", "liverpool", "arsenal", "chelsea", "barcelona",
        "real madrid", "bayern", "psg",
    ]
    politics_keywords = [
        "presidential", "election", "senate", "house", "democratic",
        "republican", "nomination", "trump", "vance", "rubio", "newsom",
        "macron", "prime minister", "parliament", "cabinet", "governor"
    ]
    crypto_keywords = [
        "bitcoin", "btc", "ethereum", "eth", "solana", "xrp", "crypto",
        "kraken", "coinbase", "ipo", "microstrategy", "doge", "token"
    ]
    geopolitics_keywords = [
        "ukraine", "nato", "china", "india", "military",
        "war", "troops", "ceasefire", "taiwan", "iran", "israel", "hezbollah",
        "gaza", "russia"
    ]
    meme_keywords = [
        "gta", "jesus christ", "$1m", "meme"
    ]

    if any(k in q for k in sports_keywords):
        return "Sports"
    if any(k in q for k in politics_keywords):
        return "Politics"
    if any(k in q for k in crypto_keywords):
        return "Crypto"
    if any(k in q for k in geopolitics_keywords):
        return "Geopolitics"
    if any(k in q for k in meme_keywords):
        return "Narrative"
    return "Other"


def detect_trade_type(question, yes_price, days_to_end):
    """v7 PDF sekcia 11 — kategorizuj trh do jednej z:
    Momentum / Time Decay / Resolution / Centovka / Value / Trap / Info-Timing / Mean reversion / Other
    """
    q = (question or "").lower()

    # Centovka — čisto cenová detekcia (asymetrická 0.005–0.07)
    if isinstance(yes_price, (int, float)) and 0.005 <= yes_price <= 0.07:
        return "Centovka"

    # Trap — vágne pravidlá „sole discretion“, bez precedensu (Čierna diera oraclu)
    trap_keywords = [
        "sole discretion", "poly admin", "judges discretion", "subjective",
        "good faith",
    ]
    if any(k in q for k in trap_keywords):
        return "Trap"

    # Resolution / Dispute — textové ambiguity, UMA edge
    resolution_keywords = [
        "called by", "out by", "official sources",
        "materially", "substantially", "at any time"
    ]
    if any(k in q for k in resolution_keywords):
        return "Resolution"

    # Time Decay — blížka expirácia tlačí cenu
    if days_to_end is not None and days_to_end <= 7:
        return "Time Decay"

    # Info-Timing — mám bližšiu informáciu / report pred trhom (CPI/FOMC/earnings v okne 1–14 dňí)
    info_timing_keywords = [
        "cpi", "fomc", "jobs report", "nfp", "unemployment rate",
        "earnings call", "sec filing", "jackson hole",
    ]
    if any(k in q for k in info_timing_keywords) and days_to_end is not None and days_to_end <= 14:
        return "Info-Timing"

    # Mean reversion — trh sa odchýlil od dlhodobej hodnoty (extrémne odds bez catalyst)
    if isinstance(yes_price, (int, float)):
        if (0.85 <= yes_price <= 0.97) or (0.03 <= yes_price < 0.05):
            if days_to_end is not None and days_to_end > 14:
                return "Mean reversion"

    # Value — NO strana má lepší price/probability mismatch než YES (longshot filter z PDF sekcia 4)
    if isinstance(yes_price, (int, float)) and 0.07 < yes_price <= 0.20:
        if days_to_end is not None and days_to_end > 30:
            return "Value"

    # Momentum — rýchly repricing okolo newého eventu
    momentum_keywords = [
        "ipo", "ceasefire", "announcement", "report", "vote", "deadline", "earnings"
    ]
    if any(k in q for k in momentum_keywords):
        return "Momentum"

    return "Other"


def oracle_risk_level(question, resolution_source=None, description=None):
    """Oracle riziko z otazky + resolutionSource + description.

    Penalizuje subjektívne resolution sources (e.g. discretion, judges, twitter feed),
    bonusuje dôveryhodné (oficialne UMA, named auth., chain data).
    """
    q = (question or "").lower()
    src = (resolution_source or "").lower()
    desc = (description or "").lower()
    combined = f"{q} || {src} || {desc}"

    high_risk_keywords = [
        "good faith", "sole discretion", "official sources only",
        "materially", "substantially", "at any time",
        "subjective", "judges discretion", "poly admin",
    ]
    medium_risk_keywords = [
        "called by", "out by", "military clash", "any country leave",
        "official sources", "twitter", "social media", "news report",
    ]
    low_risk_signals = [
        "uma optimistic oracle", "on-chain", "smart contract",
        "official api", "federal reserve", "fomc",
        "bls", "sec filing", "fifa", "nba.com", "espn",
    ]

    if any(k in combined for k in high_risk_keywords):
        return "High"
    if any(k in combined for k in medium_risk_keywords) and not any(k in combined for k in low_risk_signals):
        return "Medium"
    return "Low"


def detect_catalyst(question, days_to_end):
    q = (question or "").lower()

    if any(k in q for k in ["vote", "voting", "election", "runoff"]):
        return ("Vote/Election", "High")
    if any(k in q for k in ["nomination", "nominee", "primary", "primaries", "convention", "debate"]):
        return ("Nomination/Primary", "Medium")
    if any(k in q for k in ["deadline", "by ", "before ", "by end of", "before end of"]):
        return ("Deadline", "Medium")
    if any(k in q for k in ["earnings", "cpi", "report", "announcement", "fomc", "fed"]):
        return ("Report/Announcement", "High")
    if any(k in q for k in ["finals", "world cup", "champions league", "ufc"]):
        return ("Scheduled event", "Medium")
    if days_to_end is not None and days_to_end <= 7:
        return ("Near expiry", "Medium")
    return ("Unclear", "Low")


def price_extreme_bucket(yes_price):
    if not isinstance(yes_price, (int, float)):
        return "Missing"
    if yes_price < 0.05 or yes_price > 0.95:
        return "Very Extreme"
    if yes_price < 0.12 or yes_price > 0.88:
        return "Extreme"
    if yes_price < 0.20 or yes_price > 0.80:
        return "Stretched"
    return "Balanced"


def friction_score(liquidity, volume24hr, yes_price, days_to_end):
    score = 0
    notes = []

    if liquidity >= 500000:
        score += 3
        notes.append("high_liquidity")
    elif liquidity >= 250000:
        score += 2
        notes.append("good_liquidity")
    elif liquidity >= 100000:
        score += 1
        notes.append("ok_liquidity")
    else:
        score -= 3
        notes.append("thin_liquidity")

    if volume24hr >= 250000:
        score += 3
        notes.append("high_volume")
    elif volume24hr >= 100000:
        score += 2
        notes.append("good_volume")
    elif volume24hr >= 25000:
        score += 1
        notes.append("ok_volume")
    else:
        score -= 2
        notes.append("low_volume")

    bucket = price_extreme_bucket(yes_price)
    if bucket == "Balanced":
        score += 2
        notes.append("balanced_price")
    elif bucket == "Stretched":
        score += 0
        notes.append("stretched_price")
    elif bucket == "Extreme":
        score -= 2
        notes.append("extreme_price")
    elif bucket == "Very Extreme":
        score -= 3
        notes.append("very_extreme_price")
    else:
        score -= 2
        notes.append("missing_price")

    if days_to_end is not None:
        if days_to_end < 2:
            score -= 2
            notes.append("too_close_expiry")
        elif days_to_end <= 30:
            score += 1
            notes.append("near_expiry_ok")
        elif days_to_end > 365:
            score -= 1
            notes.append("too_far_expiry")

    if score >= 6:
        label = "Low friction"
    elif score >= 3:
        label = "Manageable"
    elif score >= 0:
        label = "Medium"
    else:
        label = "High friction"

    return score, label, notes


def exit_score(liquidity, volume24hr, yes_price, days_to_end):
    score = 0
    notes = []

    if liquidity >= 500000:
        score += 3
        notes.append("deep_book_proxy")
    elif liquidity >= 250000:
        score += 2
        notes.append("solid_book_proxy")
    elif liquidity >= 100000:
        score += 1
        notes.append("acceptable_book_proxy")
    else:
        score -= 3
        notes.append("weak_book_proxy")

    if volume24hr >= 100000:
        score += 2
        notes.append("active_flow")
    elif volume24hr >= 25000:
        score += 1
        notes.append("ok_flow")
    else:
        score -= 1
        notes.append("weak_flow")

    if isinstance(yes_price, (int, float)):
        if 0.10 <= yes_price <= 0.90:
            score += 1
            notes.append("not_edge_of_book")
        else:
            score -= 1
            notes.append("edge_of_book")
    else:
        score -= 2
        notes.append("missing_price")

    if days_to_end is not None and days_to_end < 2:
        score -= 2
        notes.append("expiry_exit_risk")

    if score >= 5:
        label = "Good exit"
    elif score >= 2:
        label = "Okay exit"
    else:
        label = "Weak exit"

    return score, label, notes


def decision_bias(flag, yes_price, oracle_risk, trade_type, gate_score, fr_score, ex_score, category=None):
    if not isinstance(yes_price, (int, float)):
        return "No trade", "PASS"

    if oracle_risk == "High":
        return "No trade", "PASS"

    # v6 fix: pri sportoch a narrative nikdy negenerujeme BUY signál — max WATCH/lean,
    # lebo kategória je už penalizovaná a centovky tu väčšinou nie sú edge, len lottery ticket.
    sport_or_narrative = category in ("Sports", "Narrative")

    # Centovka by-pass: aj keď score_market dá PASS flag (lebo extreme price + far expiry penalty),
    # podla v6 sú centovky legítne lottery tickety s asymetrickým R:R. Vyžaduje len gate>=4 + Low oracle.
    centovka_qualifies = (
        trade_type == "Centovka"
        and gate_score >= 4
        and fr_score >= 1
        and ex_score >= 1
        and oracle_risk == "Low"
        and not sport_or_narrative
    )

    # Mirror centovka by-pass: yes >= 0.95 — BUY NO ako symetrický centovka
    mirror_qualifies = (
        gate_score >= 4
        and fr_score >= 1
        and ex_score >= 1
        and oracle_risk == "Low"
        and not sport_or_narrative
    )

    if flag == "PASS" and not (centovka_qualifies or mirror_qualifies):
        return "No trade", "PASS"

    # POTENCIÁL (REVIEW) trh — pri rozumnom gate dovolíme BUY signal aj mimo "WATCH" flagu
    review_qualifies = flag in ("WATCH", "REVIEW") and gate_score >= 5 and not sport_or_narrative

    # Cenovo extrémna zóna YES <= 5c — klasická v6 centovka (5–12 USDC)
    if yes_price <= 0.05:
        if centovka_qualifies:
            return "Lean YES (centovka)", "BUY YES"
        if sport_or_narrative:
            return "Lean YES (watch)", "PASS"
        return "No trade", "PASS"

    # 5–15c — lacný long-tail tip, vyžaduje gate≥5
    if yes_price < 0.15:
        if review_qualifies and trade_type in ("Centovka", "Momentum", "Resolution", "Time Decay"):
            return "Lean YES", "BUY YES"
        if sport_or_narrative:
            return "Lean NO (watch)", "PASS"
        return "Lean NO", "BUY NO"

    # 85–95c — BUY NO ako mirror centovka
    if yes_price >= 0.95:
        if mirror_qualifies:
            return "Lean NO (mirror centovka)", "BUY NO"
        return "No trade", "PASS"

    if yes_price > 0.85:
        if review_qualifies:
            return "Lean NO", "BUY NO"
        if sport_or_narrative:
            return "Lean NO (watch)", "PASS"
        return "Lean NO", "BUY NO"

    # Mid-band BUY YES (0.15–0.45)
    if 0.15 <= yes_price <= 0.45:
        if review_qualifies:
            return "Lean YES", "BUY YES"

    # Mid-band BUY NO (0.55–0.85)
    if 0.55 <= yes_price <= 0.85:
        if review_qualifies:
            return "Lean NO", "BUY NO"

    return "No trade", "PASS"


def fail_point(checklist, oracle_risk, notes):
    if oracle_risk == "High":
        return "Oracle Trap"
    if checklist and not checklist["resolutability"]["ok"]:
        return "Resolutability"
    if checklist and not checklist["oracle"]["ok"]:
        return "Oracle Trap"
    if checklist and not checklist["friction"]["ok"]:
        return "Frikcia"
    if checklist and not checklist["exit"]["ok"]:
        return "Exit"
    if checklist and not checklist["catalyst"]["ok"]:
        return "Catalyst"
    if "noise_market" in (notes or []):
        return "Noise market"
    if "sports_hype_risk" in (notes or []):
        return "Sports hype"
    return "Žiadny kritický fail"


def sizing_cap_v7(flag, trade_type, final_decision, soft_weak_count=0):
    """v7 sizing tiery (USDC) na bankrolli 500.

    PDF sekcia 6 + sekcia 10:
    - Centovka: 5–10 (1–2%)
    - Štandard: 10–18 (2–3.5%)
    - Strong:   20–35 (4–7%)
    - Extrém:   50–60 (10–12%) — len 1 naraťív

    Ak su·1–2 SOFT podmienky slabé → zníž tier o jeden stupeň (PDF sekcia 10).
    soft_weak_count >= 2 → downgrade.
    """
    if final_decision == "PASS":
        return "0 USDC"

    # Základný tier
    if trade_type == "Centovka":
        base_tier = "centovka"
    elif flag == "WATCH":
        base_tier = "strong"
    else:
        base_tier = "standard"

    # Downgrade pri 2+ slabých SOFT (PDF: zníž sizing / vyžaduj väčší edge)
    if soft_weak_count >= 2:
        downgrade = {"strong": "standard", "standard": "centovka", "centovka": "centovka"}
        base_tier = downgrade.get(base_tier, base_tier)

    return {
        "centovka": "5–10 USDC",
        "standard": "10–18 USDC",
        "strong": "20–35 USDC",
        "extreme": "50–60 USDC",
    }[base_tier]


# Backwards-compat alias — niektoré callsites možno ešte volajú starý názov
def sizing_cap_from_v6(flag, trade_type, final_decision, soft_weak_count=0):
    return sizing_cap_v7(flag, trade_type, final_decision, soft_weak_count)


def edge_threshold_pp_v7(trade_type):
    """v7 HARD edge prahy po frikcii (PDF sekcia 10).
    Resolution/Dispute = 15pp, Momentum/Time Decay = 10pp,
    Centovka = asymetria (žiadny pp prah, R:R-driven),
    Value/Mean reversion/Info-Timing/Trap/Other = default 10pp.
    """
    if trade_type == "Resolution":
        return APP_CONFIG["edge_pp_resolution"]      # 15+
    if trade_type == "Trap":
        return APP_CONFIG["edge_pp_resolution"]      # rovnaké ako Resolution — vyžaduje vyšší prah
    if trade_type == "Time Decay":
        return APP_CONFIG["edge_pp_time_decay"]      # 10
    if trade_type == "Momentum":
        return APP_CONFIG["edge_pp_momentum"]        # 10 (HARD)
    if trade_type == "Centovka":
        return 0.0                                    # R:R asymetria, nie pp edge
    if trade_type in ("Value", "Mean reversion", "Info-Timing"):
        return APP_CONFIG["edge_pp_momentum"]        # 10
    return APP_CONFIG["edge_pp_momentum"]            # default 10


def estimate_friction_pp(yes_price, no_price, liquidity, best_bid=None, best_ask=None):
    """Odhadni friction v percentuálnych bodoch (PDF sekcia 2: spread + fees + sklz).

    spread_pp: best_ask − best_bid v pp (po prevedení na centálne body x100)
    fees_pp: ~2pp (Polymarket protocol)
    slippage_pp: priblizne 1pp ak liq < 100k, 0.5pp ak < 500k, 0.2pp inak
    """
    # spread
    if isinstance(best_bid, (int, float)) and isinstance(best_ask, (int, float)) and best_ask > best_bid:
        spread_pp = (best_ask - best_bid) * 100.0
    elif isinstance(yes_price, (int, float)) and isinstance(no_price, (int, float)):
        # ak nemáme bid/ask, predpokladaj 1–2% spread podľa likvidity
        implied = abs(1.0 - (yes_price + no_price)) * 100.0
        spread_pp = max(implied, 1.0)
    else:
        spread_pp = 2.0

    fees_pp = 2.0

    liq = float(liquidity or 0)
    if liq >= 500000:
        slippage_pp = 0.2
    elif liq >= 100000:
        slippage_pp = 0.5
    elif liq >= 20000:
        slippage_pp = 1.0
    else:
        slippage_pp = 2.0

    return round(spread_pp + fees_pp + slippage_pp, 2)


def estimate_quoted_edge_pp(yes_price, no_price, trade_type, oracle_risk):
    """Odhad surového (pre-friction) edgu v pp.

    Pre dashboard nemáme pri peňažný model pravdepodobnosti, ale vieme
    proxy z míery odchylky od midpointu, kategorickej heuristiky a oracle bias-u.
    Vráti odhadnutý edge v pp (može byť 0 ak nevíme).
    """
    if not isinstance(yes_price, (int, float)):
        return 0.0

    # Centovky: edge nepočítame v pp — R:R asymetria (1c → cca 100c)
    if trade_type == "Centovka":
        # Aproximácia: edge je pomer skutočnej P k cene; bez modelu vracíme high default
        # iba ak oracle je low (ináč nie je verifikovateľný)
        return 0.0

    # Resolution / Trap — edge je v interpretácii pravidiel; nedetáme ho z ceny
    if trade_type in ("Resolution", "Trap"):
        return 0.0

    # Momentum / Time Decay / Value / Mean reversion / Info-Timing
    # Heuristika: odchýlka od 0.5 (mid) je približne proxy edge-u, keďže retail často
    # útočí na ne. Vrátime pp distance od najbližšieho „fair“ bodu (0.05/0.5/0.95).
    nearest = min([0.05, 0.5, 0.95], key=lambda x: abs(yes_price - x))
    distance_pp = abs(yes_price - nearest) * 100.0

    # Kontrolovaný return — max 30pp, než to začne klamať
    return round(min(distance_pp, 30.0), 2)


def build_hard_soft_checklist_v7(
    gate_resolutability, gate_friction, gate_exit, gate_catalyst, gate_oracle,
    fr_score, ex_score, fr_label, ex_label, catalyst_type, catalyst_confidence,
    oracle_risk, trade_type, days_to_end, category,
    yes_price=None, no_price=None, liquidity=None, best_bid=None, best_ask=None,
):
    """v7 (PDF sekcia 10): HARD podmienky musia byť OK; SOFT sú stupne (silne/stredne/slabo).

    HARD: Resolutability, Edge (po frikcii), Cash rezerva, Korelácia.
    SOFT: Frikcia, Exit, Catalyst, Oracle Trap.

    Edge prah: Momentum/TimeDecay 10pp, Resolution/Trap 15pp, Centovka R:R asymetria.
    Frikcia (PDF sekcia 2): spread + fees + sklz <= 1/2 edge prahu = "silne".
    """
    edge_threshold = edge_threshold_pp_v7(trade_type)
    quoted_edge_pp = estimate_quoted_edge_pp(yes_price, no_price, trade_type, oracle_risk)
    friction_pp = estimate_friction_pp(yes_price, no_price, liquidity, best_bid, best_ask)
    after_cost_edge_pp = round(quoted_edge_pp - friction_pp, 2)

    # HARD #1: Resolutability
    hard_resolutability_ok = gate_resolutability

    # HARD #2: Edge po frikcii >= prah (PDF sekcia 10)
    if trade_type == "Centovka":
        # Centovky: edge je R:R asymetria (1c -> ~100c). HARD = low oracle + likvidita.
        hard_edge_ok = oracle_risk == "Low" and gate_friction
        if isinstance(yes_price, (int, float)) and yes_price > 0:
            edge_note = (f"Centovka R:R — cena {yes_price:.3f}, asymetria {(1.0/yes_price):.0f}x. "
                         f"Frikcia {friction_pp}pp.")
        else:
            edge_note = f"Centovka asymetria, frikcia {friction_pp}pp."
    elif trade_type in ("Resolution", "Trap"):
        # Resolution/Trap edge je textový/pravidlový, nemerateľný z ceny.
        hard_edge_ok = gate_resolutability and oracle_risk != "High" and gate_friction
        edge_note = (f"Resolution/Trap edge je textový — vyžaduje {edge_threshold:.0f}pp prah "
                     f"po frikcii ({friction_pp}pp).")
    else:
        # Štandard: po frikcii edge >= threshold
        hard_edge_ok = after_cost_edge_pp >= edge_threshold and gate_friction
        edge_note = (f"Po frikcii edge ~ {after_cost_edge_pp}pp "
                     f"(quoted {quoted_edge_pp}pp - frikcia {friction_pp}pp), "
                     f"prah {edge_threshold:.0f}pp pre {trade_type}.")

    # HARD #3 + #4: portfoliové (dashboard predpokladá OK, /risk-status overí pri kliknutí)
    hard_cash_reserve_ok = True
    hard_correlation_ok = True

    # SOFT — stupne
    def grade(ok, score=None, mid=None, hi=None):
        if score is None:
            return "silne" if ok else "slabo"
        if hi is not None and score >= hi:
            return "silne"
        if mid is not None and score >= mid:
            return "stredne"
        return "slabo"

    # SOFT #1: Frikcia podľa PDF sekcia 2 — spread+fees+sklz <= 1/2 edge prahu
    if edge_threshold > 0:
        if friction_pp <= 0.5 * edge_threshold:
            soft_friction = "silne"
        elif friction_pp <= edge_threshold:
            soft_friction = "stredne"
        else:
            soft_friction = "slabo"
        friction_note = (f"{fr_label} — friction ~ {friction_pp}pp "
                         f"(½ prahu = {0.5*edge_threshold:.1f}pp).")
    else:
        # Centovka — fall-back na fr_score
        soft_friction = grade(gate_friction, fr_score, mid=3, hi=4)
        friction_note = f"{fr_label} (score {fr_score}), friction ~ {friction_pp}pp."

    soft_exit = grade(gate_exit, ex_score, mid=2, hi=3)
    soft_catalyst = ("silne" if catalyst_confidence == "High"
                     else "stredne" if catalyst_confidence == "Medium"
                     else "slabo")
    soft_oracle = ("silne" if oracle_risk == "Low"
                   else "stredne" if oracle_risk == "Medium"
                   else "slabo")

    hard_all_ok = (hard_resolutability_ok and hard_edge_ok
                   and hard_cash_reserve_ok and hard_correlation_ok)
    soft_weak_count = sum(1 for s in (soft_friction, soft_exit, soft_catalyst, soft_oracle)
                          if s == "slabo")

    return {
        "hard": {
            "resolutability": {"ok": hard_resolutability_ok,
                "note": "Pravidlá čisté — vieš, čo je YES/NO." if hard_resolutability_ok
                        else "Pravidlá alebo wording obsahujú ambiguities („sole discretion“, „materiality“)."},
            "edge": {
                "ok": hard_edge_ok,
                "note": edge_note,
                "thresholdPp": edge_threshold,
                "quotedEdgePp": quoted_edge_pp,
                "frictionPp": friction_pp,
                "afterCostEdgePp": after_cost_edge_pp,
            },
            "cashReserve": {"ok": hard_cash_reserve_ok,
                "note": f"Skontroluj: po vstupe min. {int(APP_CONFIG['cash_reserve'])} USDC rezerva + 10% buffer."},
            "correlation": {"ok": hard_correlation_ok,
                "note": f"Skontroluj koreláciu: max {int(APP_CONFIG['max_narrative_exposure'])} USDC na naraťív."},
        },
        "soft": {
            "friction": {"grade": soft_friction, "score": fr_score,
                "frictionPp": friction_pp,
                "note": friction_note},
            "exit": {"grade": soft_exit, "score": ex_score,
                "note": f"{ex_label} (score {ex_score})."},
            "catalyst": {"grade": soft_catalyst,
                "note": f"{sk_catalyst_type(catalyst_type)} ({sk_oracle_risk(catalyst_confidence)})."},
            "oracleTrap": {"grade": soft_oracle,
                "note": f"Oracle riziko: {sk_oracle_risk(oracle_risk)}."},
        },
        "summary": {
            "hardAllOk": hard_all_ok,
            "softWeakCount": soft_weak_count,
            "recommendation": (
                "Poď sniper — všetky HARD OK" if hard_all_ok and soft_weak_count <= 1
                else "Možný vstup s menším sizingom" if hard_all_ok and soft_weak_count == 2
                else "PASS — SOFT podmienky príliš slabé" if hard_all_ok
                else "PASS — HARD podmienka zlyhala"
            ),
        },
    }


def normalize_words(text):
    q = (text or "").lower()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    words = [w for w in q.split() if len(w) > 2]
    stop = {
        "will", "the", "2026", "2025", "2024", "with", "from", "that",
        "have", "this", "what", "when", "before", "after", "into", "over",
        "under", "their", "they", "wins", "win", "lose", "losee"
    }
    return [w for w in words if w not in stop]


def detect_cluster(question, category):
    q = (question or "").lower()

    if "world cup" in q or "fifa" in q:
        return "FIFA World Cup 2026"
    if "nba finals" in q:
        return "NBA Finals 2026"
    if "stanley cup" in q:
        return "Stanley Cup"
    if "champions league" in q:
        return "Champions League"
    if "presidential election" in q or "president" in q:
        return "US Presidential"
    if "senate" in q:
        return "US Senate"
    if "house" in q:
        return "US House"
    if "bitcoin" in q or "btc" in q:
        return "Bitcoin"
    if "ethereum" in q or "eth" in q:
        return "Ethereum"
    if "coinbase" in q:
        return "Coinbase"
    if "kraken" in q:
        return "Kraken"
    if "ukraine" in q:
        return "Ukraine"
    if "israel" in q or "gaza" in q or "hezbollah" in q:
        return "Middle East"

    words = normalize_words(question)
    if category == "Sports" and len(words) >= 3:
        return "Sports: " + " ".join(words[:3])
    if len(words) >= 2:
        return f"{category}: " + " ".join(words[:2])
    return f"{category}: misc"


def clamp_price(x):
    if x is None:
        return None
    return max(0.01, min(0.99, round(x, 3)))


def round_usdc(x):
    return int(round(x))


def compute_exit_targets(entry_side, entry_price, trade_type):
    if not isinstance(entry_price, (int, float)):
        return None, None

    if trade_type == "Centovka":
        if entry_price <= 0.01:
            return clamp_price(0.06), clamp_price(0.11)
        if entry_price <= 0.02:
            return clamp_price(0.06), clamp_price(0.11)
        if entry_price <= 0.03:
            return clamp_price(0.07), clamp_price(0.12)
        return clamp_price(entry_price + 0.04), clamp_price(entry_price + 0.08)

    if trade_type == "Momentum":
        return clamp_price(entry_price + 0.08), clamp_price(entry_price + 0.15)

    if trade_type == "Time Decay":
        return clamp_price(entry_price + 0.05), clamp_price(entry_price + 0.10)

    if trade_type == "Resolution":
        return clamp_price(entry_price + 0.10), clamp_price(entry_price + 0.18)

    return clamp_price(entry_price + 0.06), clamp_price(entry_price + 0.12)


def exit_split_for_trade(trade_type):
    if trade_type == "Centovka":
        return {"tp1Pct": 50, "tp2Pct": 30, "runnerPct": 20}
    if trade_type == "Momentum":
        return {"tp1Pct": 50, "tp2Pct": 30, "runnerPct": 20}
    if trade_type == "Time Decay":
        return {"tp1Pct": 40, "tp2Pct": 40, "runnerPct": 20}
    if trade_type == "Resolution":
        return {"tp1Pct": 40, "tp2Pct": 35, "runnerPct": 25}
    return {"tp1Pct": 50, "tp2Pct": 30, "runnerPct": 20}


def build_execution_plan(flag, trade_type, final_decision, yes_price, no_price, liquidity, volume24hr, days_to_end,
                        best_bid=None, best_ask=None):
    """v7 execution plan + live bid/ask z Polymarket order book.

    bestBid/bestAsk pochádzaju z Gamma API a vzťahujú sa na YES stranu. Pre BUY NO
    odvodíme NO stranu: noBid = 1 − yesAsk, noAsk = 1 − yesBid.
    """
    if final_decision == "PASS":
        return {
            "entrySide": "NONE",
            "limitPrice": None,
            "buyLimitPrice": None,
            "sellLimitPrice": None,
            "bestBid": best_bid,
            "bestAsk": best_ask,
            "spreadPct": None,
            "stakeUSDC": 0,
            "stakePct": "0%",
            "tranche1USDC": 0,
            "tranche2USDC": 0,
            "tranche3USDC": 0,
            "takeProfit1": "",
            "takeProfit2": "",
            "tp1Pct": 0,
            "tp2Pct": 0,
            "runnerPct": 0,
            "tp1Action": "No trade.",
            "tp2Action": "No trade.",
            "runnerRule": "No trade.",
            "timeStop": "No trade.",
            "fullExitTrigger": "No trade.",
        }

    if trade_type == "Centovka":
        stake = 8
    elif flag == "WATCH":
        stake = 30
    else:
        stake = 15

    if liquidity >= 500000 and volume24hr >= 100000:
        improve = 0.01
    elif liquidity >= 150000 and volume24hr >= 25000:
        improve = 0.02
    else:
        improve = 0.03

    if final_decision == "BUY YES":
        base_price = yes_price if isinstance(yes_price, (int, float)) else None
        limit_price = clamp_price(base_price - improve) if base_price is not None else None
        entry_side = "YES"
        target_anchor = limit_price if limit_price is not None else base_price
    else:
        base_price = no_price if isinstance(no_price, (int, float)) else None
        limit_price = clamp_price(base_price - improve) if base_price is not None else None
        entry_side = "NO"
        target_anchor = limit_price if limit_price is not None else base_price

    tp1, tp2 = compute_exit_targets(entry_side, target_anchor, trade_type)
    split = exit_split_for_trade(trade_type)

    t1 = round_usdc(stake * 0.40)
    t2 = round_usdc(stake * 0.35)
    t3 = stake - t1 - t2

    tp1_action = f"Pri cene {tp1:.3f} predať {split['tp1Pct']}% pozície a locknúť profit." if tp1 is not None else ""
    tp2_action = f"Pri cene {tp2:.3f} predať ďalších {split['tp2Pct']}% pozície." if tp2 is not None else ""

    if trade_type == "Momentum":
        runner_rule = f"Po TP2 nechaj {split['runnerPct']}% runner len ak flow ostáva silný a edge sa nepotvrdzuje proti tebe."
    elif trade_type == "Time Decay":
        runner_rule = f"Po TP2 nechaj max {split['runnerPct']}% runner; pred deadlinom znižuj agresívnejšie, ak katalyzátor neprichádza."
    elif trade_type == "Resolution":
        runner_rule = f"Po TP2 nechaj {split['runnerPct']}% runner len ak sa nezhoršuje oracle alebo dispute riziko."
    else:
        runner_rule = f"Po TP2 nechaj {split['runnerPct']}% runner len ak edge ostáva čistý a order book sa nekazí."

    if days_to_end is not None and days_to_end <= 3:
        time_stop = "Ak nepríde očakávaný pohyb rýchlo, zníž alebo zavri pozíciu ešte pred expirácou."
    else:
        time_stop = "Ak sa trh nepohne v smere tézy do 24–72 hodín po očakávanom katalyzátore, zníž alebo zavri pozíciu."

    full_exit = "Okamžitý full exit pri zrušení asymetrie, novom oracle alebo dispute riziku, faktickej chybe v téze alebo prudkom zhoršení likvidity."

    # Live bid/ask z Polymarket order book — odvod pre stranu, na ktorú vstupujeme
    if entry_side == "YES":
        side_best_bid = best_bid if isinstance(best_bid, (int, float)) else None
        side_best_ask = best_ask if isinstance(best_ask, (int, float)) else None
    else:  # NO strana — invertuj YES bid/ask
        side_best_bid = (1.0 - best_ask) if isinstance(best_ask, (int, float)) else None
        side_best_ask = (1.0 - best_bid) if isinstance(best_bid, (int, float)) else None

    if isinstance(side_best_bid, (int, float)) and isinstance(side_best_ask, (int, float)) and side_best_ask > side_best_bid:
        spread_pct = round((side_best_ask - side_best_bid) * 100.0, 2)
    else:
        spread_pct = None

    # Polymarket tick = 0.001
    TICK = 0.001

    # BUY limit (maker entry) — sedieť na bestBid (top of bid book = maker, fill po prvom takerovi)
    # Ak je spread prázdny (best_ask <= best_bid + tick), použi base limit_price
    if isinstance(side_best_bid, (int, float)) and isinstance(side_best_ask, (int, float)):
        if side_best_ask - side_best_bid > TICK + 1e-9:
            # bid + tick je ešte pod ask → môžeme byť top of bid book
            buy_limit = clamp_price(round(side_best_bid + TICK, 3))
        else:
            # spread = 1 tick → sedieť priamo na bestBid (penny stuck)
            buy_limit = clamp_price(round(side_best_bid, 3))
    elif isinstance(side_best_bid, (int, float)):
        buy_limit = clamp_price(round(side_best_bid, 3))
    elif limit_price is not None:
        buy_limit = limit_price
    else:
        buy_limit = None

    # SELL limit (TP1 maker exit) — limitka priamo na TP1 cene (chceš predaj NAD aktuálnym ask)
    if tp1 is not None:
        sell_limit = clamp_price(round(tp1, 3))
    else:
        sell_limit = None

    return {
        "entrySide": entry_side,
        "limitPrice": limit_price,
        "buyLimitPrice": buy_limit,
        "sellLimitPrice": sell_limit,
        "bestBid": round(side_best_bid, 4) if isinstance(side_best_bid, (int, float)) else None,
        "bestAsk": round(side_best_ask, 4) if isinstance(side_best_ask, (int, float)) else None,
        "spreadPct": spread_pct,
        "stakeUSDC": stake,
        "stakePct": f"{round(stake / 500 * 100, 1)}%",
        "tranche1USDC": t1,
        "tranche2USDC": t2,
        "tranche3USDC": t3,
        "takeProfit1": f"{tp1:.3f}" if tp1 is not None else "",
        "takeProfit2": f"{tp2:.3f}" if tp2 is not None else "",
        "tp1Pct": split["tp1Pct"],
        "tp2Pct": split["tp2Pct"],
        "runnerPct": split["runnerPct"],
        "tp1Action": tp1_action,
        "tp2Action": tp2_action,
        "runnerRule": runner_rule,
        "timeStop": time_stop,
        "fullExitTrigger": full_exit,
    }


def build_auto_draft(question, category, trade_type, yes_price, no_price, days_to_end,
                     oracle_risk, fr_label_sk, ex_label_sk, catalyst_type_sk,
                     catalyst_confidence_sk, flag, gate_score, notes, checklist,
                     liquidity, volume24hr, fr_score, ex_score):
    bias, final_decision = decision_bias(
        flag=flag,
        yes_price=yes_price,
        oracle_risk=oracle_risk,
        trade_type=trade_type,
        gate_score=gate_score,
        fr_score=fr_score,
        ex_score=ex_score,
        category=category,
    )

    if flag == "PASS":
        thesis = "Default zostáva PASS. Setup zatiaľ nevyzerá ako čistý after-cost edge."
    elif flag == "REVIEW":
        thesis = "Market stojí za review, ale zatiaľ nie je dosť čistý na okamžitý vstup."
    else:
        thesis = "Market je kandidát na WATCH, pretože má relatívne čistý setup, použiteľný katalyzátor a prijateľnú frikciu."

    if isinstance(yes_price, (int, float)) and yes_price < 0.15:
        mispricing = "YES je v longshot pásme pod 15%, takže najprv treba preveriť, či retail neprepláca outsidera a či nie je value skôr na NO strane."
    elif isinstance(yes_price, (int, float)) and yes_price > 0.85:
        mispricing = "YES je v pásme nad 85%, takže treba preveriť certainty bias a možnú value na NO strane."
    elif "noise_market" in notes:
        mispricing = "Trh skôr pripomína noise market bez stabilného edge-u."
    else:
        mispricing = "Mispricing môže byť v načasovaní katalyzátora, správaní retailu alebo v tom, že trh ešte plne nezacenil relevantný scenár."

    if trade_type == "Momentum":
        edge = "Pravdepodobný edge je timing alebo news edge; trh sa môže preceňovať až po potvrdení správy."
    elif trade_type == "Time Decay":
        edge = "Pravdepodobný edge je time-decay setup; čas pracuje proti jednej strane a trh to nemusí správne diskontovať."
    elif trade_type == "Resolution":
        edge = "Ak edge existuje, je hlavne resolution alebo oracle charakteru; bez 100% jasných pravidiel však treba zostať extrémne konzervatívny."
    elif trade_type == "Centovka":
        edge = "Ak edge existuje, je asymetrický; ide o centovku, kde malý sizing môže dávať zmysel len pri čistom technickom alebo scenárovom edge-i."
    else:
        edge = "Edge nie je úplne zrejmý; treba ho chápať skôr ako predbežný kandidát na ďalšie filtrovanie než hotový trade."

    catalyst = f"Hlavný katalyzátor: {catalyst_type_sk} / {catalyst_confidence_sk}."
    if days_to_end is not None:
        catalyst += f" Do expirácie ostáva približne {int(round(days_to_end))} dní."

    if oracle_risk == "High":
        resolution = "Oracle riziko je vysoké; tento setup je bližšie k trapu než k čistému tradu."
    elif oracle_risk == "Medium":
        resolution = "Oracle riziko je stredné; treba overiť wording, official source a media consensus."
    else:
        resolution = "Oracle riziko je nízke; wording zatiaľ nepôsobí ako výrazný oracle trap."

    invalidation = "Full exit alebo PASS nastáva pri novej informácii, ktorá ruší pôvodnú asymetriu, pri zhoršení likvidity, pri rozpade katalyzátora alebo pri novom oracle alebo dispute riziku."

    confidence_map = {
        "PASS": 3,
        "REVIEW": 5,
        "WATCH": 7,
    }
    confidence = confidence_map.get(flag, 4)

    sizing_hint = "Žiadny sizing."
    if final_decision in ["BUY YES", "BUY NO"]:
        if trade_type == "Centovka":
            sizing_hint = "v7 sizing: 5–10 USDC (1–2% bankroll, asymetrický lottery ticket)."
        elif flag == "WATCH":
            sizing_hint = "v7 sizing: 20–35 USDC (4–7% pri 15+pp edge a blízkom katalyzátore)."
        else:
            sizing_hint = "v7 sizing: 10–18 USDC (2–3.5% pri štandardnom 10–12pp edge po frikcii)."

    return {
        "thesis": thesis,
        "mispricing": mispricing,
        "edge": edge,
        "catalyst": catalyst,
        "resolution": resolution,
        "invalidation": invalidation,
        "bias": bias,
        "finalDecision": final_decision,
        "confidence": confidence,
        "sizingHint": sizing_hint,
    }


def entry_zone_status(final_decision, limit_price, yes_price, no_price):
    if final_decision not in ["BUY YES", "BUY NO"] or not isinstance(limit_price, (int, float)):
        return {
            "code": "none",
            "label": "Mimo plánu",
            "distance": None,
        }

    market_price = yes_price if final_decision == "BUY YES" else no_price
    if not isinstance(market_price, (int, float)):
        return {
            "code": "unknown",
            "label": "Bez ceny",
            "distance": None,
        }

    dist = round(market_price - limit_price, 3)

    if market_price <= limit_price:
        return {
            "code": "entry",
            "label": "V entry zóne",
            "distance": dist,
        }

    if dist <= 0.01:
        return {
            "code": "near",
            "label": "Blízko zóny",
            "distance": dist,
        }

    if dist <= 0.03:
        return {
            "code": "far",
            "label": "Mimo zóny",
            "distance": dist,
        }

    return {
        "code": "chase",
        "label": "Nechase",
        "distance": dist,
    }


def build_whale_signal(yes_price, days_to_end, liquidity, volume24hr, oracle_risk, auto_draft):
    late_certainty = (
        isinstance(yes_price, (int, float)) and
        yes_price >= 0.80 and
        days_to_end is not None and
        days_to_end <= 7
    )

    score = 0
    reasons = []

    if liquidity >= 500000:
        score += 1
        reasons.append("vyššia likvidita")
    if volume24hr >= 100000:
        score += 1
        reasons.append("silnejší 24h flow")
    if late_certainty:
        score += 1
        reasons.append(">80% a krátky čas do expirácie")
    if oracle_risk == "Low":
        score += 1
        reasons.append("nízke oracle riziko")

    if score >= 4:
        label = "Silný flow signál"
    elif score >= 2:
        label = "Stredný flow signál"
    else:
        label = "Slabý flow signál"

    copy_ok = False
    copy_note = "Whale signal je iba sekundárny filter; nesmie otočiť PASS na BUY."

    if auto_draft.get("finalDecision") in ["BUY YES", "BUY NO"] and late_certainty and oracle_risk == "Low":
        copy_note = "Aj pri čistom markete nad 80% je to len review signal; nie auto-copy."
    elif auto_draft.get("finalDecision") == "PASS":
        copy_note = "PASS ostáva PASS, aj keby leaderboard alebo flow vyzeral bullish."

    return {
        "label": label,
        "lateCertainty": late_certainty,
        "copyOk": copy_ok,
        "copyNote": copy_note,
        "reasons": reasons[:4],
    }


def score_market(m, strict_mode=False):
    score = 0
    notes = []

    liquidity = to_float(m.get("liquidity"))
    volume24hr = to_float(m.get("volume24hr"))
    yes_price, no_price = get_yes_no_prices(m)
    raw_question = m.get("question") or ""
    question = raw_question.lower()
    category = categorize_market(raw_question)
    end_date = parse_date(m.get("endDate"))
    oracle_risk = oracle_risk_level(
        raw_question,
        resolution_source=m.get("resolutionSource") or m.get("resolution_source"),
        description=m.get("description"),
    )

    now = datetime.now(timezone.utc)
    days_to_end = None
    if end_date:
        days_to_end = (end_date - now).total_seconds() / 86400

    trade_type = detect_trade_type(raw_question, yes_price, days_to_end)
    catalyst_type, catalyst_confidence = detect_catalyst(raw_question, days_to_end)

    fr_score, fr_label, fr_notes = friction_score(liquidity, volume24hr, yes_price, days_to_end)
    ex_score, ex_label, ex_notes = exit_score(liquidity, volume24hr, yes_price, days_to_end)

    gate_resolutability = oracle_risk == "Low"
    gate_base_rate = category in ["Politics", "Crypto", "Sports", "Other", "Geopolitics"]
    gate_friction = fr_score >= 3
    gate_exit = ex_score >= 2
    # v7: katalyzátor je platný ak je do 45 dní (proximity), centovky majú 540 dňový výnimku
    catalyst_window_days = 540 if trade_type == "Centovka" else 180
    gate_catalyst = catalyst_confidence in ["High", "Medium"] and days_to_end is not None and days_to_end <= catalyst_window_days
    gate_oracle = oracle_risk != "High"

    gate_score = sum([
        1 if gate_resolutability else 0,
        1 if gate_base_rate else 0,
        1 if gate_friction else 0,
        1 if gate_exit else 0,
        1 if gate_catalyst else 0,
        1 if gate_oracle else 0,
    ])

    score += fr_score
    score += ex_score // 2

    if days_to_end is not None:
        # v7 time window: 2–60 dňí preferred, 60–90 obchodovateľné, >90 len ak je
        # blízky katalyzátor (riešené v gate_catalyst window).
        if 2 <= days_to_end <= 60:
            score += 2
            notes.append("v7_preferred_window")
        elif 60 < days_to_end <= 90:
            score += 1
            notes.append("v7_extended_window")
        elif days_to_end < 1:
            score -= 4
            notes.append("too_close_to_expiry")
        elif days_to_end < 2:
            score -= 2
            notes.append("close_to_expiry")
        elif days_to_end > 365:
            score -= 3
            notes.append("too_far_expiry")
        elif days_to_end > 90:
            # >90d: bez blízkeho catalyst penalize, s katalyzátorom OK
            if catalyst_confidence in ["High", "Medium"]:
                score -= 1
                notes.append("far_expiry_with_catalyst")
            else:
                score -= 2
                notes.append("far_expiry_no_catalyst")
    else:
        score -= 2
        notes.append("missing_end_date")

    noise_keywords = [
        "up or down", "5 minutes", "hourly", "today", "this week",
        "daily", "minute", "opens up or down"
    ]
    if any(k in question for k in noise_keywords):
        score -= 6
        notes.append("noise_market")

    if oracle_risk == "High":
        score -= 5
        notes.append("high_oracle_risk")
    elif oracle_risk == "Medium":
        score -= 2
        notes.append("medium_oracle_risk")

    if category == "Sports":
        score -= 3
        notes.append("sports_hype_risk")

    if category == "Narrative":
        score -= 4
        notes.append("narrative_risk")

    if category == "Politics" and days_to_end is not None and days_to_end > 365:
        score -= 2
        notes.append("long_dated_politics")

    if trade_type == "Centovka":
        score += 1
        notes.append("asymmetric_centovka")

    if strict_mode:
        if category == "Sports":
            score -= 5
            notes.append("strict_sports_penalty")
        if category == "Narrative":
            score -= 4
            notes.append("strict_narrative_penalty")
        if oracle_risk == "Medium":
            score -= 2
            notes.append("strict_medium_oracle_penalty")
        if isinstance(yes_price, (int, float)) and (yes_price < 0.05 or yes_price > 0.95):
            score -= 3
            notes.append("strict_extreme_price_penalty")

    hard_reject = (
        oracle_risk == "High" or
        "noise_market" in notes or
        "thin_liquidity" in fr_notes or
        "missing_price" in fr_notes or
        yes_price is None or
        liquidity <= 0 or
        volume24hr < 0
    )

    sports_exception_ok = (
        category != "Sports" or (
            liquidity >= 400000 and
            volume24hr >= 150000 and
            isinstance(yes_price, (int, float)) and
            0.18 <= yes_price <= 0.82 and
            days_to_end is not None and
            7 <= days_to_end <= 120 and
            not strict_mode
        )
    )

    strict_watch = (
        not hard_reject and
        gate_score >= 5 and
        gate_oracle and
        gate_resolutability and
        gate_friction and
        gate_exit and
        isinstance(yes_price, (int, float)) and
        0.12 <= yes_price <= 0.88 and
        trade_type != "Resolution" and
        "narrative_risk" not in notes and
        sports_exception_ok
    )

    if strict_watch and score >= 8:
        flag = "WATCH"
    elif not hard_reject and gate_score >= 3 and score >= 1:
        flag = "REVIEW"
    else:
        flag = "PASS"

    if strict_mode:
        if category in ["Sports", "Narrative"]:
            flag = "PASS"
        if oracle_risk in ["Medium", "High"] and flag == "WATCH":
            flag = "REVIEW"

    display_flag = {
        "WATCH": "WATCH",
        "REVIEW": "POTENCIÁL",
        "PASS": "PASS",
    }.get(flag, flag)

    checklist = {
        "resolutability": {
            "ok": gate_resolutability,
            "note": "Pravidlá sú bez silnej ambiguity." if gate_resolutability else "Pravidlá alebo wording nesú ambiguities."
        },
        "baseRate": {
            "ok": gate_base_rate,
            "note": "Kategória je analyzovateľná base-rate prístupom." if gate_base_rate else "Slabý base-rate rámec."
        },
        "friction": {
            "ok": gate_friction,
            "note": f"{sk_friction_label(fr_label)}, friction score {fr_score}."
        },
        "exit": {
            "ok": gate_exit,
            "note": f"{sk_exit_label(ex_label)}, exit score {ex_score}."
        },
        "catalyst": {
            "ok": gate_catalyst,
            "note": f"{sk_catalyst_type(catalyst_type)}, confidence {sk_oracle_risk(catalyst_confidence)}."
        },
        "oracle": {
            "ok": gate_oracle,
            "note": f"Oracle riziko: {sk_oracle_risk(oracle_risk)}."
        }
    }

    # v7 HARD/SOFT checklist (zachovávame aj pre kompatibilitu legacy 6/6)
    best_bid_val = safe_num_or_none(m.get("bestBid"))
    best_ask_val = safe_num_or_none(m.get("bestAsk"))
    checklist_v7 = build_hard_soft_checklist_v7(
        gate_resolutability=gate_resolutability,
        gate_friction=gate_friction,
        gate_exit=gate_exit,
        gate_catalyst=gate_catalyst,
        gate_oracle=gate_oracle,
        fr_score=fr_score,
        ex_score=ex_score,
        fr_label=sk_friction_label(fr_label),
        ex_label=sk_exit_label(ex_label),
        catalyst_type=sk_catalyst_type(catalyst_type),
        catalyst_confidence=catalyst_confidence,
        oracle_risk=oracle_risk,
        trade_type=trade_type,
        days_to_end=days_to_end,
        category=category,
        yes_price=yes_price,
        no_price=no_price,
        liquidity=liquidity,
        best_bid=best_bid_val,
        best_ask=best_ask_val,
    )

    auto_draft = build_auto_draft(
        question=raw_question,
        category=category,
        trade_type=trade_type,
        yes_price=yes_price,
        no_price=no_price,
        days_to_end=days_to_end,
        oracle_risk=oracle_risk,
        fr_label_sk=sk_friction_label(fr_label),
        ex_label_sk=sk_exit_label(ex_label),
        catalyst_type_sk=sk_catalyst_type(catalyst_type),
        catalyst_confidence_sk=sk_oracle_risk(catalyst_confidence),
        flag=flag,
        gate_score=gate_score,
        notes=notes,
        checklist=checklist,
        liquidity=liquidity,
        volume24hr=volume24hr,
        fr_score=fr_score,
        ex_score=ex_score,
    )

    # Konzistentnosť: ak final_decision je BUY (centovka/mirror bypass), upgradni flag/displayFlag.
    # Bez tohto vidno PASS flag + BUY YES decision, čo je mütiace.
    if auto_draft["finalDecision"] in ("BUY YES", "BUY NO") and flag == "PASS":
        flag = "REVIEW"
        display_flag = "POTENCIÁL"

    execution_plan = build_execution_plan(
        flag=flag,
        trade_type=trade_type,
        final_decision=auto_draft["finalDecision"],
        yes_price=yes_price,
        no_price=no_price,
        liquidity=liquidity,
        volume24hr=volume24hr,
        days_to_end=days_to_end,
        best_bid=best_bid_val,
        best_ask=best_ask_val,
    )

    fail = fail_point(checklist, oracle_risk, notes)
    soft_weak_count = (checklist_v7.get("summary", {}) or {}).get("softWeakCount", 0)
    sizing_cap = sizing_cap_v7(flag, trade_type, auto_draft["finalDecision"], soft_weak_count=soft_weak_count)
    cluster = detect_cluster(raw_question, category)
    entry_zone = entry_zone_status(
        auto_draft["finalDecision"],
        execution_plan["limitPrice"],
        yes_price,
        no_price
    )

    why_now = []
    if catalyst_confidence in ["High", "Medium"]:
        why_now.append(f"katalyzátor {sk_catalyst_type(catalyst_type).lower()}")
    if days_to_end is not None and days_to_end <= 7:
        why_now.append("blízka expirácia")
    if entry_zone["code"] in ["entry", "near"]:
        why_now.append(entry_zone["label"].lower())
    if isinstance(yes_price, (int, float)) and yes_price < 0.15:
        why_now.append("longshot filter")
    if oracle_risk == "Low":
        why_now.append("nízke oracle riziko")

    if why_now:
        why_now_text = "Why now: " + ", ".join(why_now[:3]) + "."
    else:
        why_now_text = "Why now: bez silného aktuálneho triggera."

    is_watchlist = (
        auto_draft["finalDecision"] in ["BUY YES", "BUY NO"] or
        flag == "WATCH" or
        entry_zone["code"] in ["entry", "near"] or
        (gate_score >= 5 and flag in ["WATCH", "REVIEW"])
    )

    whale_signal = build_whale_signal(
        yes_price=yes_price,
        days_to_end=days_to_end,
        liquidity=liquidity,
        volume24hr=volume24hr,
        oracle_risk=oracle_risk,
        auto_draft=auto_draft,
    )

    return {
        "candidateScore": score if isinstance(score, (int, float)) else 0,
        "flag": flag,
        "flagLabel": display_flag,
        "notes": notes,
        "yesPrice": yes_price,
        "noPrice": no_price,
        "daysToEnd": days_to_end,
        "category": category,
        "categoryLabel": sk_category(category),
        "tradeType": trade_type,
        "tradeTypeLabel": sk_trade_type(trade_type),
        "oracleRisk": oracle_risk,
        "oracleRiskLabel": sk_oracle_risk(oracle_risk),
        "gateScore": gate_score,
        "frictionScore": fr_score,
        "frictionLabel": fr_label,
        "frictionLabelSk": sk_friction_label(fr_label),
        "exitScore": ex_score,
        "exitLabel": ex_label,
        "exitLabelSk": sk_exit_label(ex_label),
        "catalystType": catalyst_type,
        "catalystTypeLabel": sk_catalyst_type(catalyst_type),
        "catalystConfidence": catalyst_confidence,
        "catalystConfidenceLabel": sk_oracle_risk(catalyst_confidence),
        "checklist": checklist,
        "checklistV7": checklist_v7,
        "edgeThresholdPp": edge_threshold_pp_v7(trade_type),
        "gate": {
            "resolutability": gate_resolutability,
            "baseRate": gate_base_rate,
            "friction": gate_friction,
            "exit": gate_exit,
            "catalyst": gate_catalyst,
            "oracle": gate_oracle,
        },
        "autoDraft": auto_draft,
        "executionPlan": execution_plan,
        "failPoint": fail,
        "sizingCap": sizing_cap,
        "cluster": cluster,
        "entryZone": entry_zone,
        "whyNow": why_now_text,
        "isWatchlist": is_watchlist,
        "strictModeApplied": strict_mode,
        "whaleSignal": whale_signal,
    }


def build_market_row(m, strict_mode=False):
    scored = score_market(m, strict_mode=strict_mode)

    return {
        "question": m.get("question"),
        "slug": m.get("slug"),
        "active": m.get("active"),
        "closed": m.get("closed"),
        "volume": m.get("volume"),
        "volume24hr": m.get("volume24hr"),
        "liquidity": m.get("liquidity"),
        "endDate": m.get("endDate"),
        "outcomes": parse_json_list(m.get("outcomes")),
        "outcomePrices": parse_json_list(m.get("outcomePrices")),
        "bestBid": m.get("bestBid"),
        "bestAsk": m.get("bestAsk"),
        "lastTradePrice": m.get("lastTradePrice"),
        "candidateScore": scored["candidateScore"],
        "flag": scored["flag"],
        "flagLabel": scored["flagLabel"],
        "notes": scored["notes"],
        "yesPrice": scored["yesPrice"],
        "noPrice": scored["noPrice"],
        "daysToEnd": scored["daysToEnd"],
        "category": scored["category"],
        "categoryLabel": scored["categoryLabel"],
        "tradeType": scored["tradeType"],
        "tradeTypeLabel": scored["tradeTypeLabel"],
        "oracleRisk": scored["oracleRisk"],
        "oracleRiskLabel": scored["oracleRiskLabel"],
        "gateScore": scored["gateScore"],
        "gate": scored["gate"],
        "frictionScore": scored["frictionScore"],
        "frictionLabel": scored["frictionLabel"],
        "frictionLabelSk": scored["frictionLabelSk"],
        "exitScore": scored["exitScore"],
        "exitLabel": scored["exitLabel"],
        "exitLabelSk": scored["exitLabelSk"],
        "catalystType": scored["catalystType"],
        "catalystTypeLabel": scored["catalystTypeLabel"],
        "catalystConfidence": scored["catalystConfidence"],
        "catalystConfidenceLabel": scored["catalystConfidenceLabel"],
        "checklist": scored["checklist"],
        "checklistV7": scored.get("checklistV7"),
        "edgeThresholdPp": scored.get("edgeThresholdPp"),
        "autoDraft": scored["autoDraft"],
        "executionPlan": scored["executionPlan"],
        "failPoint": scored["failPoint"],
        "sizingCap": scored["sizingCap"],
        "cluster": scored["cluster"],
        "entryZone": scored["entryZone"],
        "whyNow": scored["whyNow"],
        "isWatchlist": scored["isWatchlist"],
        "strictModeApplied": scored["strictModeApplied"],
        "whaleSignal": scored["whaleSignal"],
    }


def flag_priority(label):
    if label == "WATCH":
        return 0
    if label == "POTENCIÁL":
        return 1
    return 2


def decision_priority(value):
    if value == "BUY YES":
        return 0
    if value == "BUY NO":
        return 1
    return 2


def oracle_priority(level):
    if level == "Low":
        return 0
    if level == "Medium":
        return 1
    return 2


def watchlist_priority(row):
    if row.get("autoDraft", {}).get("finalDecision") in ["BUY YES", "BUY NO"]:
        return 0
    if row.get("flag") == "WATCH":
        return 1
    if row.get("entryZone", {}).get("code") in ["entry", "near"]:
        return 2
    return 3


def apply_diversity(rows, diversify=True, max_per_category=None, max_per_cluster=3):
    """Per-kategória diverzifikacia. Sport/Narrative obmedzené (úží cap),
    ostatné majú široký priestor (Politics 2028 nominees maď cca 100 trhov)."""
    if not diversify:
        return rows

    if max_per_category is None or isinstance(max_per_category, int):
        # backwards compat: ak je int, použije sa global cap, ale Sports/Narrative obmedzíme
        global_cap = max_per_category if isinstance(max_per_category, int) else 8
        max_per_category = {
            "Sports": 3,
            "Narrative": 2,
            "Politics": global_cap,
            "Crypto": global_cap,
            "Geopolitics": global_cap,
            "Other": global_cap,
        }

    default_cap = max(max_per_category.values()) if max_per_category else 8

    category_counts = defaultdict(int)
    cluster_counts = defaultdict(int)
    selected = []

    for row in rows:
        cat = row.get("category", "Other")
        cluster = row.get("cluster", "misc")
        cap = max_per_category.get(cat, default_cap)

        if category_counts[cat] >= cap:
            continue
        if cluster_counts[cluster] >= max_per_cluster:
            continue

        selected.append(row)
        category_counts[cat] += 1
        cluster_counts[cluster] += 1

    return selected


def top_non_sports(rows, limit=3):
    non_sports = [r for r in rows if r.get("category") != "Sports"]
    return non_sports[:limit]


def build_watchlist(rows, limit=12, max_per_category=None):
    """Watchlist s per-kategória stropom — default max 3 sport, 2 narrative,
    aby zoznam neovládla jedna kategória (v praxi WC tituly)."""
    if max_per_category is None:
        max_per_category = {"Sports": 3, "Narrative": 2}
    watch = [r for r in rows if r.get("isWatchlist")]
    watch.sort(
        key=lambda x: (
            watchlist_priority(x),
            decision_priority(x.get("autoDraft", {}).get("finalDecision")),
            flag_priority(x.get("flagLabel")),
            -to_float(x.get("gateScore")),
            -to_float(x.get("candidateScore")),
        )
    )
    out = []
    cat_counts = defaultdict(int)
    for r in watch:
        cat = r.get("category") or ""
        cap = max_per_category.get(cat)
        if cap is not None and cat_counts[cat] >= cap:
            continue
        out.append(r)
        cat_counts[cat] += 1
        if len(out) >= limit:
            break
    # Ak po kategorizácii bolo málo výsledkov, doplni zvyškom (iž nad cap), aby UI nebolo prázdne
    if len(out) < limit:
        seen = {id(r) for r in out}
        for r in watch:
            if id(r) in seen:
                continue
            out.append(r)
            if len(out) >= limit:
                break
    return out


def extract_market_condition_ids(market):
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


def normalize_trade_item(item):
    wallet = item.get("proxyWallet") or item.get("wallet") or item.get("maker_address") or ""
    name = item.get("name") or item.get("pseudonym") or short_wallet(wallet) or "unknown"

    price = safe_num_or_none(item.get("price"))
    size = safe_num_or_none(item.get("size"))
    timestamp = item.get("timestamp") or item.get("match_time") or item.get("last_update")

    title = item.get("title") or item.get("marketTitle") or item.get("question") or ""
    slug = item.get("slug") or item.get("eventSlug") or ""
    outcome = item.get("outcome") or ""
    side = item.get("side") or ""
    tx = item.get("transactionHash") or item.get("transaction_hash") or ""

    notional = None
    if isinstance(price, (int, float)) and isinstance(size, (int, float)):
        notional = round(price * size, 2)

    return {
        "wallet": wallet,
        "walletShort": short_wallet(wallet),
        "name": name,
        "side": side,
        "price": price,
        "size": size,
        "notional": notional,
        "timestamp": timestamp,
        "timestampIso": format_ts(timestamp),
        "title": title,
        "slug": slug,
        "outcome": outcome,
        "txHash": tx,
        "profileImage": item.get("profileImageOptimized") or item.get("profileImage") or "",
    }


def summarize_whale_wallet(trades):
    whale_trade_min = APP_CONFIG["whale_trade_min_notional"]
    whale_sum_min = APP_CONFIG["whale_wallet_recent_sum"]

    notionals = [to_float(x.get("notional"), 0) for x in trades if to_float(x.get("notional"), 0) > 0]
    recent_sum = round(sum(notionals), 2)
    max_trade = max(notionals) if notionals else 0.0
    whale_trade_count = sum(1 for x in notionals if x >= whale_trade_min)

    is_whale = (max_trade >= whale_trade_min) or (recent_sum >= whale_sum_min)

    return {
        "isWhale": is_whale,
        "whaleTradeMin": whale_trade_min,
        "recentSumThreshold": whale_sum_min,
        "recentSum": recent_sum,
        "maxTrade": round(max_trade, 2),
        "whaleTradeCount": whale_trade_count,
        "label": "Whale" if is_whale else "Large trader",
    }


def fetch_recent_trades_for_market(condition_ids=None, limit=20, min_amount=None):
    if not condition_ids:
        return []

    if min_amount is None:
        min_amount = APP_CONFIG["whale_trade_min_notional"]

    url = f"{DATA_API_BASE}/trades"
    params = {
        "limit": limit,
        "market": ",".join(condition_ids),
        "filterType": "CASH",
        "filterAmount": min_amount,
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        rows = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
        normalized = [normalize_trade_item(x) for x in rows]
        normalized = [x for x in normalized if to_float(x.get("notional"), 0) >= min_amount]
        normalized.sort(key=lambda x: safe_int(x.get("timestamp"), 0), reverse=True)
        return normalized[:limit]
    except Exception:
        return []


def fetch_wallet_trades(wallet, limit=25, min_amount=None):
    if not wallet:
        return []

    if min_amount is None:
        min_amount = APP_CONFIG["whale_trade_min_notional"]

    url = f"{DATA_API_BASE}/trades"
    params = {
        "limit": limit,
        "user": wallet,
        "filterType": "CASH",
        "filterAmount": min_amount,
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        rows = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
        normalized = [normalize_trade_item(x) for x in rows]
        normalized = [x for x in normalized if to_float(x.get("notional"), 0) >= min_amount]
        normalized.sort(key=lambda x: safe_int(x.get("timestamp"), 0), reverse=True)
        return normalized[:limit]
    except Exception:
        return []


def fetch_wallet_activity(wallet, limit=25):
    if not wallet:
        return []

    url = f"{DATA_API_BASE}/activity"
    params = {
        "user": wallet,
        "limit": limit,
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        rows = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
        out = []

        for item in rows[:limit]:
            out.append({
                "type": item.get("type") or item.get("activityType") or "",
                "timestamp": item.get("timestamp") or item.get("createdAt") or item.get("time"),
                "timestampIso": format_ts(item.get("timestamp") or item.get("createdAt") or item.get("time")),
                "title": item.get("title") or item.get("marketTitle") or "",
                "side": item.get("side") or "",
                "outcome": item.get("outcome") or "",
                "price": safe_num_or_none(item.get("price")),
                "size": safe_num_or_none(item.get("size")),
                "txHash": item.get("transactionHash") or item.get("transaction_hash") or "",
            })

        out.sort(key=lambda x: safe_int(x.get("timestamp"), 0), reverse=True)
        return out
    except Exception:
        return []


def fetch_wallet_positions(wallet, limit=15):
    if not wallet:
        return []

    url = f"{DATA_API_BASE}/positions"
    params = {
        "user": wallet,
        "limit": limit,
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        rows = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
        out = []

        for item in rows[:limit]:
            out.append({
                "title": item.get("title") or "",
                "outcome": item.get("outcome") or "",
                "size": safe_num_or_none(item.get("size") or item.get("shares")),
                "avgPrice": safe_num_or_none(item.get("avgPrice") or item.get("averagePrice") or item.get("price")),
                "value": safe_num_or_none(item.get("value") or item.get("currentValue")),
                "cashPnl": safe_num_or_none(item.get("cashPnl") or item.get("realizedPnl") or item.get("pnl")),
            })

        return out
    except Exception:
        return []


def fetch_leaderboard(limit=8):
    url = f"{DATA_API_BASE}/v1/leaderboard"
    params = {"limit": limit}
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("results") or data.get("data") or data.get("leaderboard") or []
        else:
            rows = []

        output = []
        for item in rows[:limit]:
            wallet = item.get("wallet") or item.get("address") or item.get("proxyWallet") or ""
            output.append({
                "name": item.get("name") or item.get("username") or item.get("user") or short_wallet(wallet) or "unknown",
                "profit": item.get("profit") or item.get("pnl") or item.get("realized_pnl") or 0,
                "volume": item.get("volume") or item.get("trade_volume") or 0,
                "wallet": wallet,
                "walletShort": short_wallet(wallet),
            })
        return output
    except Exception:
        return []


@app.route("/leaderboard")
def leaderboard():
    limit = safe_int(request.args.get("limit", "8"), 8)
    data = fetch_leaderboard(limit=limit)
    return jsonify({"count": len(data), "leaders": data})


@app.route("/market-trades")
def market_trades():
    slug = request.args.get("slug", "").strip()
    limit = safe_int(request.args.get("limit", "20"), 20)
    min_amount = to_float(request.args.get("min_amount", str(APP_CONFIG["whale_trade_min_notional"])))

    if not slug:
        return jsonify({"count": 0, "trades": []})

    params = {"limit": 250, "active": "true", "closed": "false"}
    url = f"{GAMMA_BASE}/markets"
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    markets = r.json()

    market = None
    for m in markets:
        if m.get("slug") == slug:
            market = m
            break

    if not market:
        return jsonify({"count": 0, "trades": []})

    condition_ids = extract_market_condition_ids(market)
    trades = fetch_recent_trades_for_market(condition_ids=condition_ids, limit=limit, min_amount=min_amount)

    return jsonify({
        "count": len(trades),
        "slug": slug,
        "conditionIds": condition_ids,
        "whaleMinNotional": min_amount,
        "trades": trades,
    })


@app.route("/wallet-history")
def wallet_history():
    wallet = request.args.get("wallet", "").strip()
    limit = safe_int(request.args.get("limit", "25"), 25)
    min_amount = to_float(request.args.get("min_amount", str(APP_CONFIG["whale_trade_min_notional"])))

    if not wallet:
        return jsonify({
            "wallet": "",
            "trades": [],
            "activity": [],
            "positions": [],
            "walletSummary": {},
        })

    trades = fetch_wallet_trades(wallet=wallet, limit=limit, min_amount=min_amount)
    activity = fetch_wallet_activity(wallet=wallet, limit=limit)
    positions = fetch_wallet_positions(wallet=wallet, limit=15)
    wallet_summary = summarize_whale_wallet(trades)

    return jsonify({
        "wallet": wallet,
        "walletShort": short_wallet(wallet),
        "minAmount": min_amount,
        "trades": trades,
        "activity": activity,
        "positions": positions,
        "walletSummary": wallet_summary,
    })


@app.route("/state", methods=["GET"])
def state_get():
    return jsonify(load_state())


@app.route("/state", methods=["POST"])
def state_post():
    """Uloží alerts/watchlistSnapshot — jednoduchá perzistencia, prepisom celého payloadu."""
    payload = request.get_json(force=True, silent=True) or {}
    state = load_state()
    if "alerts" in payload and isinstance(payload["alerts"], list):
        state["alerts"] = payload["alerts"][:200]
    if "watchlistSnapshot" in payload and isinstance(payload["watchlistSnapshot"], list):
        state["watchlistSnapshot"] = payload["watchlistSnapshot"][:50]
    save_state(state)
    return jsonify(state)


@app.route("/pnl-log", methods=["GET"])
def pnl_log_get():
    limit = safe_int(request.args.get("limit", "100"), 100)
    return jsonify({"entries": read_pnl_log(limit=limit)})


@app.route("/risk-status")
def risk_status():
    """v7 portfolio risk overview: bankroll, rezerva, open pozicíe, expozícia, drawdown, loss streak.
    Počíta z append-only PnL logu (open/close events)."""
    entries = read_pnl_log(limit=1000)
    open_positions = {}   # slug -> {side, usdc, price, narrative, ts}
    closed = []
    for e in entries:
        slug = e.get("slug") or e.get("question")
        kind = e.get("kind")
        if kind == "open":
            open_positions[slug] = {
                "slug": slug,
                "question": e.get("question"),
                "side": e.get("side"),
                "usdc": to_float(e.get("usdc")),
                "price": to_float(e.get("price")),
                "narrative": e.get("narrative") or "",
                "ts": e.get("ts"),
            }
        elif kind == "close":
            if slug in open_positions:
                op = open_positions.pop(slug)
                pnl = to_float(e.get("pnl"))
                closed.append({**op, "pnl": pnl, "closeTs": e.get("ts")})
            else:
                closed.append({
                    "slug": slug,
                    "question": e.get("question"),
                    "pnl": to_float(e.get("pnl")),
                    "closeTs": e.get("ts"),
                })

    total_exposure = sum(p["usdc"] for p in open_positions.values())
    realized_pnl = sum(c.get("pnl", 0) for c in closed)
    equity = APP_CONFIG["bankroll_total"] + realized_pnl

    # Day drawdown: P&L záznamy z dneška
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_pnl = sum(
        c.get("pnl", 0) for c in closed
        if (c.get("closeTs") or "").startswith(today)
    )
    today_pnl_pct = today_pnl / APP_CONFIG["bankroll_total"] if APP_CONFIG["bankroll_total"] else 0

    # Loss streak: poč po sebe idúcich záporných closed
    loss_streak = 0
    for c in reversed(closed):
        if c.get("pnl", 0) < 0:
            loss_streak += 1
        else:
            break

    # Per-narrative expozícia
    narrative_exposure = {}
    for p in open_positions.values():
        narr = p.get("narrative") or "misc"
        narrative_exposure[narr] = narrative_exposure.get(narr, 0) + p["usdc"]

    # v7 limity check
    cash_available = max(0, equity - total_exposure)
    reserve_ok = cash_available >= APP_CONFIG["cash_reserve"]
    exposure_ok = total_exposure <= APP_CONFIG["max_total_exposure"]
    positions_ok = len(open_positions) < APP_CONFIG["max_active_positions"]
    drawdown_ok = today_pnl_pct > -APP_CONFIG["daily_drawdown_limit_pct"]
    streak_ok = loss_streak < APP_CONFIG["loss_streak_pause"]
    narrative_breaches = [
        n for n, e in narrative_exposure.items()
        if e > APP_CONFIG["max_narrative_exposure"]
    ]

    can_trade = reserve_ok and exposure_ok and positions_ok and drawdown_ok and streak_ok

    return jsonify({
        "version": APP_CONFIG["system_version"],
        "bankroll": APP_CONFIG["bankroll_total"],
        "equity": round(equity, 2),
        "cashAvailable": round(cash_available, 2),
        "cashReserveTarget": APP_CONFIG["cash_reserve"],
        "totalExposure": round(total_exposure, 2),
        "maxTotalExposure": APP_CONFIG["max_total_exposure"],
        "realizedPnl": round(realized_pnl, 2),
        "todayPnl": round(today_pnl, 2),
        "todayPnlPct": round(today_pnl_pct * 100, 2),
        "openPositions": list(open_positions.values()),
        "openPositionsCount": len(open_positions),
        "maxActivePositions": APP_CONFIG["max_active_positions"],
        "narrativeExposure": narrative_exposure,
        "maxNarrativeExposure": APP_CONFIG["max_narrative_exposure"],
        "narrativeBreaches": narrative_breaches,
        "lossStreak": loss_streak,
        "lossStreakLimit": APP_CONFIG["loss_streak_pause"],
        "limits": {
            "reserveOk": reserve_ok,
            "exposureOk": exposure_ok,
            "positionsOk": positions_ok,
            "drawdownOk": drawdown_ok,
            "streakOk": streak_ok,
        },
        "canTrade": can_trade,
    })


@app.route("/pnl-log", methods=["POST"])
def pnl_log_post():
    """Append-only záznam pri kliknutí Otvoriť trade alebo manuálne logovanie pnł entry/exit.
    payload: {kind: 'open'|'close'|'note', slug, question, side, price, size, usdc, pnl, note}
    """
    payload = request.get_json(force=True, silent=True) or {}
    if not isinstance(payload, dict) or not payload.get("slug") and not payload.get("question"):
        return jsonify({"ok": False, "error": "missing slug/question"}), 400
    append_pnl_log(payload)
    return jsonify({"ok": True})


def is_slug_ongoing(slug):
    """True ak market ešte beží (active=true, closed!=true, archived!=true, endDate v budúcnosti).
    Per-slug 5 min cache pre Gamma lookup."""
    if not slug:
        return False
    cached = cache_get("market_status", slug)
    if cached is not None:
        return cached.get("ongoing", False)
    try:
        r = requests.get(f"{GAMMA_BASE}/markets", params={"slug": slug}, timeout=8)
        r.raise_for_status()
        data = r.json()
        if not data:
            cache_set("market_status", slug, {"ongoing": False}, ttl=300)
            return False
        m = data[0] if isinstance(data, list) else data
        active = m.get("active") is True
        closed = m.get("closed") is True
        archived = m.get("archived") is True
        end_date = parse_date(m.get("endDate"))
        end_in_future = True
        if end_date is not None:
            end_in_future = end_date > datetime.now(timezone.utc)
        ongoing = active and not closed and not archived and end_in_future
        cache_set("market_status", slug, {"ongoing": ongoing}, ttl=300)
        return ongoing
    except Exception:
        return False


@app.route("/whale-flow")
def whale_flow():
    """Top whale obchody naprieč vsetkými Polymarket trhmi (independent na dashboard scoringu).

    Defaultne:
      - filter cena 0.05–0.95 (settle obchody preprec)
      - filter only_ongoing=true (vyhadže trhy ktoré sú closed/archived alebo s endDate v minulosti)
    """
    limit = safe_int(request.args.get("limit", "15"), 15)
    min_amount = to_float(request.args.get("min_amount", str(APP_CONFIG["whale_trade_min_notional"])))
    include_settles = request.args.get("include_settles", "false").lower() == "true"
    only_ongoing = request.args.get("only_ongoing", "true").lower() == "true"
    # Data API dáva 408 pri limit > ~100. Pri only_ongoing musime fetchnut maximum,
    # lebo top trades sú typicky zo zatvorenych zapasov — musime presievat hlúbšie.
    fetch_count = 100

    cache_key = (limit, min_amount, include_settles, only_ongoing)
    cached = cache_get("whale_flow", cache_key)
    if cached is not None:
        return jsonify(cached)

    url = f"{DATA_API_BASE}/trades"
    params = {
        "limit": fetch_count,
        "filterType": "CASH",
        "filterAmount": min_amount,
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        rows = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
        normalized = [normalize_trade_item(x) for x in rows]
        normalized = [x for x in normalized if to_float(x.get("notional"), 0) >= min_amount]
        if not include_settles:
            normalized = [x for x in normalized if 0.05 <= to_float(x.get("price"), 0) <= 0.95]
        normalized.sort(key=lambda x: to_float(x.get("notional"), 0), reverse=True)

        if only_ongoing:
            ongoing = []
            seen_slugs = {}
            for t in normalized:
                slug = t.get("slug")
                if not slug:
                    continue
                if slug not in seen_slugs:
                    seen_slugs[slug] = is_slug_ongoing(slug)
                if seen_slugs[slug]:
                    ongoing.append(t)
                if len(ongoing) >= limit:
                    break
            normalized = ongoing

        result = {
            "count": len(normalized[:limit]),
            "whaleMinNotional": min_amount,
            "includeSettles": include_settles,
            "onlyOngoing": only_ongoing,
            "trades": normalized[:limit],
        }
        cache_set("whale_flow", cache_key, result, ttl=30)
        return jsonify(result)
    except Exception as e:
        return jsonify({"count": 0, "trades": [], "whaleMinNotional": min_amount, "includeSettles": include_settles, "onlyOngoing": only_ongoing, "error": str(e)})


@app.route("/markets")
def markets():
    limit = safe_int(request.args.get("limit", "80"), 80)
    active = request.args.get("active", "true")
    closed = request.args.get("closed", "false")
    min_liquidity = to_float(
        request.args.get("min_liquidity", str(APP_CONFIG["default_min_liquidity"]))
    )
    hide_pass = request.args.get("hide_pass", "true").lower() == "true"
    category_filter = request.args.get("category", "").strip()
    diversify = request.args.get("diversify", "true").lower() == "true"
    watchlist_only = request.args.get("watchlist_only", "false").lower() == "true"
    buy_only = request.args.get("buy_only", "false").lower() == "true"
    strict_mode = request.args.get("strict_mode", "false").lower() == "true"

    params = {
        "limit": 250,
        "active": active,
        "closed": closed,
    }

    cache_key_gamma = ("gamma_markets", strict_mode)
    data = cache_get("gamma", cache_key_gamma)
    if data is None:
        url = f"{GAMMA_BASE}/markets"
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        cache_set("gamma", cache_key_gamma, data, ttl=60)

    all_rows = []  # bez hide_pass / category filtra — používame na watchlist
    rows = []
    for m in data:
        if m.get("active") is not True:
            continue
        if m.get("closed") is True:
            continue

        row = build_market_row(m, strict_mode=strict_mode)

        if to_float(row.get("liquidity")) < min_liquidity:
            continue

        all_rows.append(row)

        decision = row.get("autoDraft", {}).get("finalDecision")
        gate_score = to_float(row.get("gateScore"))
        is_watch = bool(row.get("isWatchlist"))
        flag_label = row.get("flagLabel")

        # Strict mode: zobraz len BUY signály
        if buy_only and decision not in ("BUY YES", "BUY NO"):
            continue

        # Skryť PASS, ale len ak je nízkej kvality (gate <4 a nie watchlist a flag nie je POTENCIÁL/WATCH)
        if hide_pass and decision == "PASS":
            keep = (gate_score >= 4) or is_watch or flag_label in ("POTENCIÁL", "WATCH")
            if not keep:
                continue
        if category_filter and row.get("category") != category_filter:
            continue
        if watchlist_only and not row.get("isWatchlist"):
            continue

        rows.append(row)

    def price_edge_distance(x):
        # ako ďaleko od 0.50 — väčšia vzdialenosť = silnejší convict signal
        op = x.get("outcomePrices") or [None]
        try:
            yes = float(op[0]) if op else None
        except (TypeError, ValueError):
            yes = None
        if not isinstance(yes, (int, float)):
            return 0.0
        return -abs(yes - 0.5)  # neg — čím ďalej, tým vyššie v sortingu

    rows.sort(
        key=lambda x: (
            decision_priority(x.get("autoDraft", {}).get("finalDecision")),  # BUY hore
            flag_priority(x.get("flagLabel")),
            -to_float(x.get("gateScore")),
            0 if x.get("category") != "Sports" else 1,
            price_edge_distance(x),
            oracle_priority(x.get("oracleRisk")),
            -to_float(x.get("candidateScore")),
            -to_float(x.get("liquidity")),
            -to_float(x.get("volume24hr")),
        )
    )

    diversified_rows = apply_diversity(
        rows,
        diversify=diversify,
        max_per_category={
            "Sports": 3,
            "Narrative": 2,
            "Politics": 10,
            "Crypto": 8,
            "Geopolitics": 8,
            "Other": 8,
        },
        max_per_cluster=3,
    )

    diversified_rows = diversified_rows[:limit]
    alt_non_sports = top_non_sports(all_rows, limit=3)
    watchlist = build_watchlist(all_rows, limit=12)

    return jsonify({
        "count": len(diversified_rows),
        "markets": diversified_rows,
        "topNonSports": alt_non_sports,
        "watchlist": watchlist,
        "filters": {
            "min_liquidity": min_liquidity,
            "hide_pass": hide_pass,
            "category": category_filter,
            "diversify": diversify,
            "watchlist_only": watchlist_only,
            "buy_only": buy_only,
            "strict_mode": strict_mode,
        }
    })


@app.route("/markets/top")
def markets_top():
    return markets()


def candidate_score_v7(row):
    """Sniper v7 non-sports candidate scoring.

    score = resolution_score + oracle_clarity_score + catalyst_score − friction_score

    — resolution_score: text trade type bonus (Resolution/Trap/Info-Timing > Momentum > Other)
    — oracle_clarity_score: Low oracle = +3, Medium = +1, High = −2
    — catalyst_score: High = +3, Medium = +1, Low/None = 0; bonus +2 ak deadline 2–45d
    — friction_score: friction_pp / 2 (penalizuje vyšší friction)
    — hard penalt: -100 ak hardOK=False alebo finalDecision=PASS
    """
    auto = row.get("autoDraft") or {}
    final = auto.get("finalDecision", "PASS")
    cv7 = row.get("checklistV7") or {}
    summary = cv7.get("summary", {})

    # Hard disqualify
    if final == "PASS":
        return -1000
    if not summary.get("hardAllOk", False):
        return -100

    score = 0.0

    # Resolution score — trade type bonus
    tt = (row.get("tradeType") or "").strip()
    score += {
        "Resolution": 4.0,
        "Trap": 3.5,
        "Info-Timing": 3.0,
        "Time Decay": 2.5,
        "Momentum": 2.0,
        "Value": 1.5,
        "Mean reversion": 1.0,
        "Centovka": 1.5,
        "Other": 0.5,
    }.get(tt, 0.0)

    # Oracle clarity (zisk z hard.edge a soft.oracleTrap)
    oracle_grade = (cv7.get("soft", {}).get("oracleTrap", {}) or {}).get("grade", "slabo")
    score += {"silne": 3.0, "stredne": 1.0, "slabo": -2.0}.get(oracle_grade, 0)

    # Catalyst
    catalyst_grade = (cv7.get("soft", {}).get("catalyst", {}) or {}).get("grade", "slabo")
    score += {"silne": 3.0, "stredne": 1.0, "slabo": 0.0}.get(catalyst_grade, 0)

    # Catalyst window bonus (2–45 dňí = ideál)
    days = row.get("daysToEnd")
    if isinstance(days, (int, float)):
        if 2 <= days <= 45:
            score += 2.0
        elif 45 < days <= 60:
            score += 1.0

    # Friction penalty (z hard.edge.frictionPp)
    edge_obj = cv7.get("hard", {}).get("edge", {})
    friction_pp = edge_obj.get("frictionPp")
    if isinstance(friction_pp, (int, float)):
        score -= friction_pp / 2.0

    # Liquidity bonus (silnejšia exekutíva)
    liq = float(row.get("liquidityNum") or row.get("liquidity") or 0)
    if liq >= 500000:
        score += 1.0
    elif liq >= 100000:
        score += 0.5

    # Spread penalty (nad 5pp = penalizuj)
    ep = row.get("executionPlan") or {}
    spread = ep.get("spreadPct")
    if isinstance(spread, (int, float)) and spread > 5.0:
        score -= 2.0

    return round(score, 2)


@app.route("/candidates/non-sports")
def candidates_non_sports():
    """Sniper v7 non-sports kandidáti, prefiltrovaní podľa užívateľa-defined kriterií.

    Filters:
      — ne-sport kategória (Sports = vyhodené)
      — expiry 2–60 dňí
      — spread ≤ 6pp
      — likvidita ≥ 50k USDC
      — BUY YES/NO decision + hardAllOk

    Vráti top 5 zoradených podľa candidate_score_v7.
    """
    try:
        # Relaxed liquidity to catch resolution markets
        min_liq = float(request.args.get("min_liquidity", "50000"))
        limit = int(request.args.get("limit", "5"))
    except Exception:
        min_liq = 50000.0
        limit = 5

    # Fetch markets via Gamma API (same pattern ako /markets endpoint)
    cache_key_gamma = ("gamma_markets", False)
    data = cache_get("gamma", cache_key_gamma)
    if data is None:
        try:
            r = requests.get(
                f"{GAMMA_BASE}/markets",
                params={"limit": 400, "active": "true", "closed": "false"},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            cache_set("gamma", cache_key_gamma, data, ttl=60)
        except Exception as exc:
            return jsonify({"error": f"Gamma fetch failed: {exc}", "top": []}), 502

    rows = []
    for m in (data or []):
        if m.get("active") is not True or m.get("closed") is True:
            continue
        try:
            row = build_market_row(m, strict_mode=False)
        except Exception:
            continue
        if not row:
            continue
        if to_float(row.get("liquidity")) < min_liq:
            continue
        rows.append(row)

    qualified = []
    for row in rows:
        # 1) Vyhoď šport
        if row.get("category") == "Sports":
            continue

        # 2) Expiry 2–60 dňí
        days = row.get("daysToEnd")
        if not isinstance(days, (int, float)) or days < 2 or days > 60:
            continue

        # 3) Spread ≤ 6pp
        ep = row.get("executionPlan") or {}
        spread = ep.get("spreadPct")
        if isinstance(spread, (int, float)) and spread > 6.0:
            continue

        # 4) BUY decision + hardOK
        auto = row.get("autoDraft") or {}
        if not auto.get("finalDecision", "").startswith("BUY"):
            continue
        cv7 = row.get("checklistV7") or {}
        if not (cv7.get("summary") or {}).get("hardAllOk", False):
            continue

        score = candidate_score_v7(row)
        if score <= 0:
            continue

        qualified.append((score, row))

    qualified.sort(key=lambda x: -x[0])
    top = qualified[:limit]

    out = []
    for score, row in top:
        ep = row.get("executionPlan") or {}
        cv7 = row.get("checklistV7") or {}
        edge = cv7.get("hard", {}).get("edge", {})
        out.append({
            "candidateScore": score,
            "slug": row.get("slug"),
            "question": row.get("question"),
            "category": row.get("category"),
            "tradeType": row.get("tradeType"),
            "finalDecision": (row.get("autoDraft") or {}).get("finalDecision"),
            "yesPrice": row.get("yesPrice"),
            "noPrice": row.get("noPrice"),
            "liquidity": row.get("liquidity"),
            "volume24hr": row.get("volume24hr"),
            "daysToEnd": row.get("daysToEnd"),
            "endDate": row.get("endDate"),
            "oracleRisk": row.get("oracleRisk"),
            "resolutionSource": row.get("resolutionSource"),
            "executionPlan": {
                "entrySide": ep.get("entrySide"),
                "buyLimitPrice": ep.get("buyLimitPrice"),
                "sellLimitPrice": ep.get("sellLimitPrice"),
                "bestBid": ep.get("bestBid"),
                "bestAsk": ep.get("bestAsk"),
                "spreadPct": ep.get("spreadPct"),
                "stakeUSDC": ep.get("stakeUSDC"),
                "takeProfit1": ep.get("takeProfit1"),
                "takeProfit2": ep.get("takeProfit2"),
            },
            "hardEdge": {
                "thresholdPp": edge.get("thresholdPp"),
                "frictionPp": edge.get("frictionPp"),
                "afterCostEdgePp": edge.get("afterCostEdgePp"),
                "note": edge.get("note"),
            },
            "polymarketUrl": f"https://polymarket.com/event/{row.get('slug')}" if row.get("slug") else None,
        })

    return jsonify({
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "filters": {
            "min_liquidity": min_liq,
            "expiry_days": [2, 60],
            "max_spread_pp": 6.0,
            "non_sports_only": True,
            "hard_ok_required": True,
        },
        "totalQualified": len(qualified),
        "top": out,
    })


@app.route("/analyze-market", methods=["POST"])
def analyze_market():
    payload = request.get_json(force=True, silent=True) or {}
    market = payload.get("market") or {}
    strict_mode = bool(payload.get("strict_mode", False))
    return jsonify(build_market_row(market, strict_mode=strict_mode))


@app.route("/dashboard")
def dashboard():
    title = APP_CONFIG["dashboard_title"]
    default_min_liquidity = int(APP_CONFIG["default_min_liquidity"])
    whale_min = int(APP_CONFIG["whale_trade_min_notional"])
    whale_sum = int(APP_CONFIG["whale_wallet_recent_sum"])

    html = f"""
<!doctype html>
<html lang="sk">
<head>
  <meta charset="utf-8">
  <title>{title} v6.2</title>
  <style>
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 16px; background: #f5f5f5; color: #222; }}
    h1, h2, h3 {{ margin-bottom: 0.3rem; }}
    .section {{ background: #ffffff; padding: 14px; border-radius: 8px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
    .header-strip {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(340px, 500px); gap: 16px; align-items: start; margin-bottom: 14px; }}
    .header-left h1 {{ margin: 0 0 4px 0; }}
    .header-left .small {{ margin: 0; }}
    .header-right {{ display: flex; justify-content: flex-end; }}
    .status-card {{ width: 100%; font-size: 12px; color: #555; background: #ffffff; border: 1px solid #e8e8e8; border-radius: 8px; padding: 10px 12px; line-height: 1.45; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
    .top-strip {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.3fr); gap: 12px; margin-bottom: 14px; align-items: start; }}
    .top-left-stack {{ display: grid; gap: 12px; }}
    .compact-section {{ padding: 12px; }}
    .compact-box {{ padding: 8px 10px; font-size: 12px; line-height: 1.4; }}
    .controls {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 10px; align-items: end; }}
    .control {{ display: flex; flex-direction: column; gap: 4px; min-width: 150px; }}
    label {{ font-size: 12px; color: #555; font-weight: 600; }}
    input, select {{ padding: 7px 9px; border: 1px solid #ddd; border-radius: 6px; font: inherit; background: #fff; }}
    .checkbox-wrap {{ display: flex; align-items: center; gap: 7px; padding-top: 20px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; table-layout: fixed; }}
    th, td {{ padding: 5px 6px; border-bottom: 1px solid #eee; text-align: left; vertical-align: top; }}
    th {{ background: #fafafa; font-weight: 700; position: sticky; top: 0; z-index: 1; }}
    tr.clickable {{ cursor: pointer; }}
    tr.clickable:hover {{ background: #fafcff; }}
    .small {{ font-size: 12px; color: #555; }}
    .error {{ color: #c5221f; margin-bottom: 8px; font-size: 13px; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; white-space: nowrap; }}
    .watch {{ background: #e6f4ea; color: #137333; }}
    .review {{ background: #fff4e5; color: #b06000; }}
    .pass {{ background: #fce8e6; color: #c5221f; }}
    .decision-buy-yes {{ background: #e8f5e9; color: #137333; }}
    .decision-buy-no {{ background: #e8f0fe; color: #1558d6; }}
    .decision-pass {{ background: #f3f4f6; color: #555; }}
    .cat {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; background: #eef2ff; color: #3949ab; font-weight: 600; white-space: nowrap; }}
    .risk-low {{ color: #137333; font-weight: 700; }}
    .risk-medium {{ color: #b06000; font-weight: 700; }}
    .risk-high {{ color: #c5221f; font-weight: 700; }}
    .zone-entry {{ color: #137333; font-weight: 700; }}
    .zone-near {{ color: #b06000; font-weight: 700; }}
    .zone-far {{ color: #666; font-weight: 700; }}
    .zone-chase {{ color: #c5221f; font-weight: 700; }}
    .zone-none {{ color: #888; font-weight: 700; }}
    .panel-muted {{ color: #444; font-size: 13px; line-height: 1.5; background: #fafafa; border: 1px solid #eee; border-radius: 6px; padding: 10px; }}
    .table-wrap {{ overflow: auto; max-height: 62vh; border-radius: 8px; }}
    .count {{ margin-bottom: 10px; font-size: 13px; color: #444; }}
    button {{ padding: 8px 12px; border: 1px solid #ddd; border-radius: 6px; background: #fff; cursor: pointer; }}
    .btn-primary {{ background: #1558d6; border-color: #1558d6; color: white; }}
    .check {{ display: flex; align-items: flex-start; gap: 8px; padding: 8px 0; border-bottom: 1px solid #f0f0f0; font-size: 13px; }}
    .check:last-child {{ border-bottom: none; }}
    .ok {{ color: #137333; font-weight: 700; min-width: 28px; }}
    .no {{ color: #c5221f; font-weight: 700; min-width: 28px; }}
    .mid {{ color: #b06000; font-weight: 700; min-width: 28px; }}
    .v7-summary {{ background: #f7f9fc; padding: 8px; border-radius: 4px; margin-top: 6px; }}
    .risk-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }}
    .risk-card {{ background: #fff; border: 1px solid #e0e3e7; padding: 10px 12px; border-radius: 6px; }}
    .risk-card .label {{ font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 0.4px; }}
    .risk-card .value {{ font-size: 18px; font-weight: 700; margin-top: 2px; }}
    .risk-card.alert {{ background: #fff3f0; border-color: #c5221f; }}
    .risk-card.ok {{ background: #f0f7f0; border-color: #137333; }}
    .risk-card .sub {{ font-size: 11px; color: #666; margin-top: 2px; }}
    #riskManagerSection.no-trade {{ background: #fff3f0; border: 2px solid #c5221f; }}
    .metric-pill {{ display: inline-block; padding: 3px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; margin-right: 6px; margin-bottom: 6px; background: #f3f4f6; color: #333; }}
    .delta-up {{ color: #137333; font-weight: 700; }}
    .delta-down {{ color: #c5221f; font-weight: 700; }}
    .delta-flat {{ color: #666; font-weight: 700; }}
    .action-row {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }}
    .title-row {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 8px; }}
    .title-main {{ min-width: 0; flex: 1; }}
    .title-main h3 {{ margin: 0 0 8px 0; line-height: 1.25; }}
    .trade-link {{ white-space: nowrap; font-size: 13px; text-decoration: none; color: #1558d6; font-weight: 600; margin-top: 2px; }}
    .trade-link:hover {{ text-decoration: underline; }}
    .saved-note {{ margin-top: 8px; font-size: 12px; color: #137333; }}
    .draft-grid {{ display: grid; grid-template-columns: 140px 1fr; gap: 8px; font-size: 13px; margin-bottom: 10px; }}
    .draft-grid div:first-child {{ color: #666; font-weight: 600; }}
    .block-label {{ margin-top: 10px; display: block; font-size: 12px; color: #555; font-weight: 600; }}
    .question-cell {{ min-width: 280px; max-width: 520px; }}
    .question-truncate {{ line-height: 1.35; word-break: break-word; white-space: normal; }}
    .watchlist-compact {{ display: grid; gap: 4px; }}
    .watchlist-row {{ display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: center; padding: 6px 0; border-bottom: 1px solid #f0f0f0; font-size: 12px; }}
    .watchlist-row:last-child {{ border-bottom: none; }}
    .watchlist-meta {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 3px; }}
    .alert-line {{ padding: 4px 0; border-bottom: 1px solid #f0f0f0; font-size: 12px; line-height: 1.35; }}
    .alert-line:last-child {{ border-bottom: none; }}
    .leader-line {{ display: grid; grid-template-columns: 20px 1fr auto; gap: 8px; align-items: center; padding: 5px 0; border-bottom: 1px solid #f0f0f0; font-size: 12px; }}
    .leader-line:last-child {{ border-bottom: none; }}
    .leader-click {{ cursor: pointer; }}
    .leader-click:hover {{ background: #fafcff; }}
    .detail-shell {{ display: grid; gap: 14px; }}
    .detail-top {{ display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(280px, 0.8fr); gap: 14px; align-items: start; }}
    .detail-grid {{ display: grid; grid-template-columns: minmax(260px, 0.9fr) minmax(360px, 1.35fr) minmax(300px, 1fr); gap: 14px; align-items: start; }}
    .detail-card {{ background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); padding: 14px; min-height: 100%; }}
    .trade-tape {{ display: grid; gap: 6px; }}
    .trade-line {{ display: grid; grid-template-columns: 80px minmax(0, 1fr) 70px 110px 100px 100px; gap: 10px; align-items: center; padding: 7px 0; border-bottom: 1px solid #f0f0f0; font-size: 12.5px; }}
    .trade-line .trade-market {{ line-height: 1.35; word-break: break-word; }}
    .trade-line .trade-market a {{ color: #1558d6; text-decoration: underline; }}
    .whale-stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 10px; padding: 8px 10px; background: #f7faff; border: 1px solid #e3ecff; border-radius: 6px; font-size: 12px; }}
    .whale-stats .stat-label {{ color: #666; font-size: 11px; }}
    .whale-stats .stat-value {{ font-weight: 700; font-size: 14px; }}
    .leader-line {{ grid-template-columns: 20px 1fr 90px auto !important; }}
    .leader-line .leader-meta {{ font-size: 11px; color: #777; }}
    .trade-line:last-child {{ border-bottom: none; }}
    .trade-side-buy {{ color: #137333; font-weight: 700; }}
    .trade-side-sell {{ color: #c5221f; font-weight: 700; }}
    .trade-outcome {{ font-weight: 700; color: #1558d6; }}
    .wallet-btn {{ border: 1px solid #ddd; background: #fff; border-radius: 6px; padding: 6px 8px; font-size: 12px; }}
    .wallet-btn:hover {{ background: #f7f9fc; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }}
    .whale-badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; background: #eef8f1; color: #137333; }}
    .large-badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; background: #eef2ff; color: #3949ab; }}
    .subnote {{ font-size: 11px; color: #666; margin-top: 6px; }}
    @media (max-width: 1350px) {{
      .top-strip {{ grid-template-columns: 1fr; }}
      .detail-grid {{ grid-template-columns: 1fr 1fr; }}
    }}
    @media (max-width: 1200px) {{
      .header-strip {{ grid-template-columns: 1fr; }}
      .header-right {{ justify-content: stretch; }}
      .top-strip {{ grid-template-columns: 1fr; }}
      .detail-top {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 900px) {{
      .detail-grid {{ grid-template-columns: 1fr; }}
      .trade-line {{ grid-template-columns: 1fr; gap: 4px; }}
    }}
  </style>
</head>
<body>
  <div class="header-strip">
    <div class="header-left">
      <h1>{title}</h1>
      <p class="small">v7.0: HARD/SOFT checklist, edge prahy 8–15pp, sizing 5–60 USDC, daily DD 15%, max 3–4 pozície.</p>
    </div>
    <div class="header-right">
      <div class="status-card" id="statusLine">Dashboard sa inicializuje...</div>
    </div>
  </div>

  <div class="section" id="riskManagerSection">
    <h2>v7 Risk Manager</h2>
    <div id="riskManager" class="risk-grid">Načítava sa risk overview...</div>
  </div>

  <div class="section" id="nonSportsSection">
    <h2>Top non-sports kandidáti <span class="small" style="font-weight:400;">— Sniper v7.0 edge engine</span></h2>
    <div class="small" style="margin-bottom:8px;color:#666;">Filter: ne-šport · expiry 2–60d · spread ≤ 6pp · likvidita ≥ 50k · BUY+hardOK. Zoradené podľa resolution + oracle_clarity + catalyst − friction.</div>
    <div id="nonSportsBox">Načítava sa...</div>
  </div>

  <div class="section">
    <h2>Top kandidáti</h2>

    <div class="controls">
      <div class="control">
        <label for="category">Kategória</label>
        <select id="category">
          <option value="">Všetko</option>
          <option value="Sports">Šport</option>
          <option value="Politics">Politika</option>
          <option value="Crypto">Krypto</option>
          <option value="Geopolitics">Geopolitika</option>
          <option value="Narrative">Naratív</option>
          <option value="Other">Ostatné</option>
        </select>
      </div>

      <div class="control">
        <label for="minLiquidity">Min likvidita</label>
        <select id="minLiquidity">
          <option value="50000">50 000</option>
          <option value="100000" {"selected" if default_min_liquidity == 100000 else ""}>100 000</option>
          <option value="150000">150 000</option>
          <option value="250000">250 000</option>
        </select>
      </div>

      <div class="control">
        <label for="whaleMin">Whale min cash</label>
        <select id="whaleMin">
          <option value="100000">100 000</option>
          <option value="200000" selected>200 000</option>
          <option value="300000">300 000</option>
          <option value="500000">500 000</option>
        </select>
      </div>

      <div class="checkbox-wrap">
        <input type="checkbox" id="hidePass" checked />
        <label for="hidePass">Skryť PASS</label>
      </div>

      <div class="checkbox-wrap">
        <input type="checkbox" id="diversify" checked />
        <label for="diversify">Diverzifikovať feed</label>
      </div>

      <div class="checkbox-wrap">
        <input type="checkbox" id="watchlistOnly" />
        <label for="watchlistOnly">Len watchlist</label>
      </div>

      <div class="checkbox-wrap">
        <input type="checkbox" id="buyOnly" />
        <label for="buyOnly">Len BUY signály</label>
      </div>

      <div class="checkbox-wrap">
        <input type="checkbox" id="strictMode" />
        <label for="strictMode">Strict v6 mode</label>
      </div>

      <div class="control">
        <button id="refreshBtn">Obnoviť</button>
      </div>
    </div>

    <div class="count" id="countBox"></div>
    <div id="markets-error" class="error" style="display:none;"></div>

    <div class="table-wrap">
      <table id="markets-table">
        <thead>
          <tr>
            <th>Kandidát</th>
            <th>Rozhod.</th>
            <th>Entry zóna</th>
            <th>Gate</th>
            <th>Skóre</th>
            <th>Frikcia</th>
            <th>Exit</th>
            <th>Typ</th>
            <th>Kat.</th>
            <th>Oracle</th>
            <th>Otázka</th>
            <th>Yes</th>
            <th>No</th>
            <th>24h</th>
            <th>Likv.</th>
            <th>Dni</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <div id="detailPanel">
    <div class="section">
      <h3>Detail marketu</h3>
      <p class="panel-muted">Klikni na riadok v tabuľke a zobrazí sa detail trhu, checklist, systémový draft, entry/exit plán, recent whale trades a história whale aktivít.</p>
    </div>
  </div>

  <div class="top-strip">
    <div class="top-left-stack">
      <div class="section compact-section">
        <h2>Na sledovanie</h2>
        <div id="watchlistBox" class="panel-muted compact-box">Načítavam watchlist...</div>
      </div>
      <div class="section compact-section">
        <h2>Alerty</h2>
        <div id="alertsBox" class="panel-muted compact-box">Zatiaľ bez alertov.</div>
      </div>
    </div>
    <div class="section compact-section">
      <h2>Whale / Flow signal <span class="small" style="font-weight:400;">— vybraný market</span></h2>
      <div id="whaleSignalBox" class="panel-muted compact-box">Vyber market v tabuľke pre zobrazenie whale obchodov.</div>
    </div>
  </div>

  <div class="section">
    <h2>Globálny whale flow <span class="small" style="font-weight:400;">— top obchody naprieč Polymarket, len prebiehajúce trhy</span></h2>
    <div class="controls" style="margin-bottom:8px;">
      <div class="control">
        <label for="whaleMinAmount">Min USDC</label>
        <select id="whaleMinAmount">
          <option value="50000">50 000</option>
          <option value="100000" selected>100 000</option>
          <option value="200000">200 000</option>
          <option value="500000">500 000</option>
          <option value="1000000">1 000 000</option>
        </select>
      </div>
      <div class="control" style="display:flex;align-items:center;gap:6px;">
        <input type="checkbox" id="includeSettles" />
        <label for="includeSettles">Vrátane settle obchodov (cena &lt;0.05 alebo &gt;0.95)</label>
      </div>
      <div class="control" style="display:flex;align-items:center;gap:6px;">
        <input type="checkbox" id="includeClosed" />
        <label for="includeClosed">Vrátane uzavretých trhov</label>
      </div>
    </div>
    <div id="globalWhaleBox" class="panel-muted compact-box">Načítavam globálny whale flow...</div>
  </div>


  <script>
    let cachedMarkets = [];
    let selectedMarket = null;
    let cachedWatchlist = [];
    let cachedLeaders = [];
    let previousSnapshot = new Map();
    let currentDeltaMap = new Map();
    let rareAlerts = [];
    let autoRefreshTimer = null;
    let lastRefreshAt = null;
    let currentMarketTrades = [];
    let selectedWalletHistory = null;
    let selectedWallet = null;

    const DEFAULT_WHALE_MIN = {whale_min};
    const DEFAULT_WHALE_SUM = {whale_sum};
    const REFRESH_HOURS = [7, 9, 11, 13, 15, 17, 19, 21];

    function fmtInt(value) {{
      const n = Number(value);
      if (!Number.isFinite(n)) return '';
      return Math.round(n).toLocaleString('sk-SK');
    }}

    function fmtPrice(value) {{
      const n = Number(value);
      if (!Number.isFinite(n)) return '';
      return n.toFixed(3);
    }}

    function fmtDays(value) {{
      const n = Number(value);
      if (!Number.isFinite(n)) return '';
      return Math.round(n).toString();
    }}

    function fmtPnL(value) {{
      const n = Number(value);
      if (!Number.isFinite(n)) return '';
      const sign = n > 0 ? '+' : '';
      return sign + Math.round(n).toLocaleString('sk-SK');
    }}

    function fmtDateTime(dateObj) {{
      if (!(dateObj instanceof Date)) return '';
      return dateObj.toLocaleString('sk-SK', {{
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
      }});
    }}

    function fmtSize(v) {{
      const n = Number(v);
      if (!Number.isFinite(n)) return '';
      return n.toLocaleString('sk-SK', {{ maximumFractionDigits: 0 }});
    }}

    function fmtCash(v) {{
      const n = Number(v);
      if (!Number.isFinite(n)) return '';
      return n.toLocaleString('sk-SK', {{ maximumFractionDigits: 2 }});
    }}

    function fmtTime(tsIso) {{
      if (!tsIso) return '';
      const d = new Date(tsIso);
      if (Number.isNaN(d.getTime())) return '';
      return d.toLocaleString('sk-SK', {{
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit'
      }});
    }}

    function escapeHtml(value) {{
      return String(value || '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }}

    function getWhaleMin() {{
      const el = document.getElementById('whaleMin');
      return el ? Number(el.value || DEFAULT_WHALE_MIN) : DEFAULT_WHALE_MIN;
    }}

    function getScheduleInfo(now = new Date()) {{
      const currentHour = now.getHours();
      const today = new Date(now);
      const next = new Date(now);

      for (const hour of REFRESH_HOURS) {{
        if (currentHour < hour || (currentHour === hour && now.getMinutes() === 0 && now.getSeconds() === 0)) {{
          next.setHours(hour, 0, 0, 0);
          return {{ inWindow: currentHour >= 7 && currentHour <= 21, nextRefresh: next }};
        }}
      }}

      const tomorrow = new Date(today);
      tomorrow.setDate(tomorrow.getDate() + 1);
      tomorrow.setHours(7, 0, 0, 0);

      return {{ inWindow: false, nextRefresh: tomorrow }};
    }}

    function msUntilNextRefresh(now = new Date()) {{
      const info = getScheduleInfo(now);
      return Math.max(1000, info.nextRefresh.getTime() - now.getTime());
    }}

    function updateStatusLine() {{
      const el = document.getElementById('statusLine');
      if (!el) return;

      const now = new Date();
      const info = getScheduleInfo(now);
      const lastText = lastRefreshAt ? fmtDateTime(lastRefreshAt) : 'ešte neprebehla';
      const nextText = fmtDateTime(info.nextRefresh);
      const strictValue = document.getElementById('strictMode')?.checked ? 'ON' : 'OFF';
      const whaleMin = getWhaleMin().toLocaleString('sk-SK');
      const windowText = info.inWindow
        ? 'Sme v aktívnom okne 07:00–21:00.'
        : 'Sme mimo aktívneho okna 07:00–21:00.';

      el.innerHTML =
        '<strong>Dashboard aktuálny k:</strong> ' + lastText + '<br>' +
        '<strong>Aktuálny dátum a čas:</strong> ' + fmtDateTime(now) + '<br>' +
        '<strong>Plán refreshu:</strong> 07:00, 09:00, 11:00, 13:00, 15:00, 17:00, 19:00, 21:00<br>' +
        '<strong>Strict v6 mode:</strong> ' + strictValue + '<br>' +
        '<strong>Whale filter:</strong> iba cash pohyby nad ' + whaleMin + '<br>' +
        windowText + ' <strong>Ďalší plánovaný refresh:</strong> ' + nextText;
    }}

    function scheduleNextRefresh() {{
      if (autoRefreshTimer) clearTimeout(autoRefreshTimer);
      const waitMs = msUntilNextRefresh();
      autoRefreshTimer = setTimeout(async () => {{
        await loadAll();
        scheduleNextRefresh();
      }}, waitMs);
    }}

    function flagBadge(label) {{
      if (label === 'WATCH') return '<span class="badge watch">WATCH</span>';
      if (label === 'POTENCIÁL') return '<span class="badge review">POTENCIÁL</span>';
      return '<span class="badge pass">PASS</span>';
    }}

    function decisionBadge(value) {{
      if (value === 'BUY YES') return '<span class="badge decision-buy-yes">BUY YES</span>';
      if (value === 'BUY NO') return '<span class="badge decision-buy-no">BUY NO</span>';
      return '<span class="badge decision-pass">PASS</span>';
    }}

    function catBadge(cat) {{
      return '<span class="cat">' + (cat || 'Ostatné') + '</span>';
    }}

    function oracleBadge(level) {{
      if (level === 'Nízke') return '<span class="risk-low">Nízke</span>';
      if (level === 'Stredné') return '<span class="risk-medium">Stredné</span>';
      return '<span class="risk-high">Vysoké</span>';
    }}

    function zoneBadge(zone) {{
      const code = zone?.code || 'none';
      const label = zone?.label || 'Mimo plánu';
      if (code === 'entry') return '<span class="zone-entry">' + label + '</span>';
      if (code === 'near') return '<span class="zone-near">' + label + '</span>';
      if (code === 'far') return '<span class="zone-far">' + label + '</span>';
      if (code === 'chase') return '<span class="zone-chase">' + label + '</span>';
      return '<span class="zone-none">' + label + '</span>';
    }}

    function pill(text) {{
      return '<span class="metric-pill">' + text + '</span>';
    }}

    function sideBadge(side) {{
      const s = String(side || '').toUpperCase();
      if (s === 'BUY') return '<span class="trade-side-buy">BUY</span>';
      if (s === 'SELL') return '<span class="trade-side-sell">SELL</span>';
      return '<span>' + escapeHtml(s) + '</span>';
    }}

    function whaleBadge(summary) {{
      if (!summary) return '';
      if (summary.isWhale) return '<span class="whale-badge">Whale</span>';
      return '<span class="large-badge">Large trader</span>';
    }}

    function renderChecklist(checklist) {{
      const order = [
        ['resolutability', 'Resolutability'],
        ['baseRate', 'Base Rate'],
        ['friction', 'Frikcia'],
        ['exit', 'Exit'],
        ['catalyst', 'Catalyst'],
        ['oracle', 'Oracle Trap']
      ];

      return order.map(function(pair) {{
        const key = pair[0];
        const label = pair[1];
        const item = checklist[key];
        return ''
          + '<div class="check">'
          +   '<div class="' + (item.ok ? 'ok' : 'no') + '">' + (item.ok ? 'ÁNO' : 'NIE') + '</div>'
          +   '<div><strong>' + label + '</strong><br>' + (item.note || '') + '</div>'
          + '</div>';
      }}).join('');
    }}

    function gradeColor(grade) {{
      if (grade === 'silne') return 'ok';
      if (grade === 'stredne') return 'mid';
      return 'no';
    }}

    function renderChecklistV7(v7) {{
      if (!v7) return '';
      const hardOrder = [
        ['resolutability', 'Resolutability (HARD)'],
        ['edge', 'Edge po frikcii (HARD)'],
        ['cashReserve', 'Cash rezerva (HARD)'],
        ['correlation', 'Korelácia (HARD)']
      ];
      const softOrder = [
        ['friction', 'Frikcia (SOFT)'],
        ['exit', 'Exit (SOFT)'],
        ['catalyst', 'Catalyst (SOFT)'],
        ['oracleTrap', 'Oracle Trap (SOFT)']
      ];
      let html = '';
      hardOrder.forEach(function(pair) {{
        const item = (v7.hard || {{}})[pair[0]];
        if (!item) return;
        html += '<div class="check">'
             +   '<div class="' + (item.ok ? 'ok' : 'no') + '">' + (item.ok ? 'ÁNO' : 'NIE') + '</div>'
             +   '<div><strong>' + pair[1] + '</strong><br>' + (item.note || '') + '</div>'
             + '</div>';
      }});
      softOrder.forEach(function(pair) {{
        const item = (v7.soft || {{}})[pair[0]];
        if (!item) return;
        const cls = gradeColor(item.grade);
        const lbl = (item.grade || '').toUpperCase();
        html += '<div class="check">'
             +   '<div class="' + cls + '">' + lbl + '</div>'
             +   '<div><strong>' + pair[1] + '</strong><br>' + (item.note || '') + '</div>'
             + '</div>';
      }});
      const summary = v7.summary || {{}};
      const cls = summary.hardAllOk ? 'ok' : 'no';
      html += '<div class="check v7-summary">'
           +   '<div class="' + cls + '">v7</div>'
           +   '<div><strong>Odporúčanie:</strong> ' + (summary.recommendation || '') + '</div>'
           + '</div>';
      return html;
    }}

    function deltaClass(value) {{
      if (value > 0) return 'delta-up';
      if (value < 0) return 'delta-down';
      return 'delta-flat';
    }}

    function fmtDelta(value) {{
      const n = Number(value);
      if (!Number.isFinite(n)) return '0.000';
      const sign = n > 0 ? '+' : '';
      return sign + n.toFixed(3);
    }}

    function computeDeltaMap(markets) {{
      const nextMap = new Map();
      const deltaMap = new Map();
      const alerts = [];

      markets.forEach(function(m) {{
        const key = m.slug || m.question;
        const prev = previousSnapshot.get(key);

        const snap = {{
          yesPrice: m.yesPrice,
          noPrice: m.noPrice,
          flag: m.flag,
          flagLabel: m.flagLabel,
          decision: (m.autoDraft && m.autoDraft.finalDecision) || 'PASS',
          gateScore: m.gateScore,
          liquidity: m.liquidity,
          entryZone: (m.entryZone && m.entryZone.code) || 'none',
          oracleRisk: m.oracleRiskLabel
        }};
        nextMap.set(key, snap);

        const delta = {{
          yesDelta: null,
          noDelta: null,
          gateDelta: null,
          flagChanged: false,
          decisionChanged: false,
          liquidityDeltaPct: null,
          summary: []
        }};

        if (prev) {{
          if (Number.isFinite(Number(snap.yesPrice)) && Number.isFinite(Number(prev.yesPrice))) {{
            delta.yesDelta = Number((snap.yesPrice - prev.yesPrice).toFixed(3));
            if (Math.abs(delta.yesDelta) >= 0.02) delta.summary.push('YES ' + fmtDelta(delta.yesDelta));
          }}

          if (Number.isFinite(Number(snap.noPrice)) && Number.isFinite(Number(prev.noPrice))) {{
            delta.noDelta = Number((snap.noPrice - prev.noPrice).toFixed(3));
          }}

          if (Number.isFinite(Number(snap.gateScore)) && Number.isFinite(Number(prev.gateScore))) {{
            delta.gateDelta = Number(snap.gateScore) - Number(prev.gateScore);
            if (delta.gateDelta !== 0) delta.summary.push('Gate ' + (delta.gateDelta > 0 ? '+' : '') + delta.gateDelta);
          }}

          if (snap.flag !== prev.flag) {{
            delta.flagChanged = true;
            delta.summary.push('Flag: ' + prev.flagLabel + ' -> ' + snap.flagLabel);
          }}

          if (snap.decision !== prev.decision) {{
            delta.decisionChanged = true;
            delta.summary.push('Decision: ' + prev.decision + ' -> ' + snap.decision);
          }}

          if (Number.isFinite(Number(snap.liquidity)) && Number.isFinite(Number(prev.liquidity)) && Number(prev.liquidity) > 0) {{
            const pct = ((Number(snap.liquidity) - Number(prev.liquidity)) / Number(prev.liquidity)) * 100;
            delta.liquidityDeltaPct = Number(pct.toFixed(1));
          }}

          if (snap.flag === 'WATCH' && prev.flag !== 'WATCH') alerts.push('Nový WATCH: ' + (m.question || ''));
          if (prev.decision === 'PASS' && (snap.decision === 'BUY YES' || snap.decision === 'BUY NO')) alerts.push('PASS -> ' + snap.decision + ': ' + (m.question || ''));
          if (prev.entryZone !== 'entry' && snap.entryZone === 'entry') alerts.push('Market vošiel do entry zóny: ' + (m.question || ''));
          if (snap.oracleRisk === 'Vysoké' && prev.oracleRisk !== 'Vysoké') alerts.push('Oracle risk vyskočil na vysoké: ' + (m.question || ''));
          if (Number.isFinite(delta.liquidityDeltaPct) && delta.liquidityDeltaPct <= -30) alerts.push('Likvidita prudko padla: ' + (m.question || ''));
          if (m.daysToEnd !== null && Number(m.daysToEnd) <= 3 && (snap.decision === 'BUY YES' || snap.decision === 'BUY NO')) alerts.push('Blíži sa time-stop / expiry risk: ' + (m.question || ''));
        }}

        deltaMap.set(key, delta);
      }});

      previousSnapshot = nextMap;
      currentDeltaMap = deltaMap;
      rareAlerts = alerts.slice(0, 8);
    }}

    function renderWatchlist() {{
      const box = document.getElementById('watchlistBox');
      if (!box) return;

      if (!cachedWatchlist || cachedWatchlist.length === 0) {{
        box.innerHTML = 'Žiadne watchlist kandidáty podľa aktuálnych filtrov.';
        return;
      }}

      const shortList = cachedWatchlist.slice(0, 4);

      box.innerHTML = '<div class="watchlist-compact">' + shortList.map(function(m) {{
        const safeSlug = String(m.slug || '').replace(/'/g, "\\\\'");
        return ''
          + '<div class="watchlist-row">'
          +   '<div>'
          +     '<div><strong>' + (m.question || '') + '</strong></div>'
          +     '<div class="watchlist-meta">'
          +       decisionBadge((m.autoDraft && m.autoDraft.finalDecision) || 'PASS')
          +       zoneBadge(m.entryZone)
          +       pill('Gate ' + (m.gateScore ?? '') + '/6')
          +     '</div>'
          +   '</div>'
          +   '<div><button onclick="selectWatchlistItem(\\'' + safeSlug + '\\')">Otvoriť</button></div>'
          + '</div>';
      }}).join('') + '</div>';
    }}

    function renderAlerts() {{
      const box = document.getElementById('alertsBox');
      if (!box) return;

      if (!rareAlerts || rareAlerts.length === 0) {{
        box.innerHTML = 'Zatiaľ bez rare alertov.';
        return;
      }}

      box.innerHTML = rareAlerts.slice(0, 4).map(function(a) {{
        return '<div class="alert-line">' + a + '</div>';
      }}).join('');
    }}

    function renderLeaderboard() {{
      const box = document.getElementById('leaderboardBox');
      if (!box) return;

      if (!cachedLeaders || cachedLeaders.length === 0) {{
        box.innerHTML = 'Leaderboard sa nepodarilo načítať.';
        return;
      }}

      box.innerHTML = cachedLeaders.slice(0, 10).map(function(row, idx) {{
        const wallet = row.wallet || '';
        const profit = Number(row.profit || 0);
        const volume = Number(row.volume || 0);
        const polyUrl = wallet ? ('https://polymarket.com/profile/' + wallet) : '#';
        const volText = volume > 0 ? ('Vol: ' + volume.toLocaleString('sk-SK', {{maximumFractionDigits: 0}})) : '';
        return ''
          + '<div class="leader-line">'
          +   '<div><strong>' + (idx + 1) + '.</strong></div>'
          +   '<div class="leader-click" onclick="loadWalletHistory(\\'' + escapeHtml(wallet) + '\\')">'
          +     '<strong>' + escapeHtml(row.name || 'unknown') + '</strong>'
          +     '<br><span class="small mono">' + escapeHtml(row.walletShort || '') + '</span>'
          +     (volText ? ' <span class="leader-meta">· ' + volText + '</span>' : '')
          +   '</div>'
          +   '<div><span class="' + ((profit >= 0) ? 'delta-up' : 'delta-down') + '">' + fmtPnL(profit) + '</span></div>'
          +   '<div><a href="' + polyUrl + '" target="_blank" rel="noopener" class="small" title="Profil na Polymarket">profil</a></div>'
          + '</div>';
      }}).join('') + '<div class="small" style="margin-top:6px;">Klik na meno otvorí whale history. „profil“ → Polymarket. Whale filter: ' + getWhaleMin().toLocaleString('sk-SK') + '+.</div>';
    }}

    function selectWatchlistItem(slug) {{
      if (!slug) return;
      const found = cachedMarkets.find(function(m) {{ return m.slug === slug; }})
        || cachedWatchlist.find(function(m) {{ return m.slug === slug; }});
      if (found) {{
        loadMarketTrades(found.slug).then(function() {{
          showDetail(found);
        }});
      }}
    }}

    async function loadMarketTrades(slug) {{
      if (!slug) {{
        currentMarketTrades = [];
        return;
      }}
      try {{
        const res = await fetch('/market-trades?slug=' + encodeURIComponent(slug) + '&limit=20&min_amount=100');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        currentMarketTrades = data.trades || [];
      }} catch (err) {{
        currentMarketTrades = [];
      }}
    }}

    async function loadWalletHistory(wallet) {{
      if (!wallet) {{
        selectedWalletHistory = null;
        selectedWallet = null;
        return;
      }}
      selectedWallet = wallet;
      try {{
        const res = await fetch('/wallet-history?wallet=' + encodeURIComponent(wallet) + '&limit=25&min_amount=100');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        selectedWalletHistory = data;
      }} catch (err) {{
        selectedWalletHistory = {{ wallet: wallet, trades: [], activity: [], positions: [] }};
      }}
      if (selectedMarket) {{
        showDetail(selectedMarket);
      }}
    }}

    function renderWhaleSignal(m) {{
      const trades = currentMarketTrades || [];
      if (trades.length === 0) {{
        return '<div class="small">Zatiaľ žiadne whale obchody nad ' + getWhaleMin().toLocaleString('sk-SK') + ' pre tento trh.</div>';
      }}
      // Agregát
      let buyUSD = 0, sellUSD = 0, yesUSD = 0, noUSD = 0;
      trades.forEach(function(t) {{
        const usd = Number(t.notional || 0);
        const side = String(t.side || '').toUpperCase();
        const oc = String(t.outcome || '').toLowerCase();
        if (side === 'BUY') buyUSD += usd; else if (side === 'SELL') sellUSD += usd;
        if (oc.indexOf('yes') >= 0) yesUSD += usd;
        else if (oc.indexOf('no') >= 0) noUSD += usd;
      }});
      const totalUSD = buyUSD + sellUSD;
      const fmt = function(v) {{ return Number(v || 0).toLocaleString('sk-SK', {{maximumFractionDigits: 0}}); }};
      const dominant = (buyUSD >= sellUSD)
        ? ('BUY ' + Math.round(buyUSD / Math.max(totalUSD, 1) * 100) + '%')
        : ('SELL ' + Math.round(sellUSD / Math.max(totalUSD, 1) * 100) + '%');
      const yesNoNote = (yesUSD || noUSD)
        ? ('Yes ' + fmt(yesUSD) + ' / No ' + fmt(noUSD))
        : '—';
      const stats = ''
        + '<div class="whale-stats">'
        +   '<div><div class="stat-label">Počet whale obchodov</div><div class="stat-value">' + trades.length + '</div></div>'
        +   '<div><div class="stat-label">Spolu USDC</div><div class="stat-value">' + fmt(totalUSD) + '</div></div>'
        +   '<div><div class="stat-label">Dominantná strana</div><div class="stat-value">' + dominant + '</div></div>'
        +   '<div><div class="stat-label">Yes vs No (USDC)</div><div class="stat-value" style="font-size:12px;">' + yesNoNote + '</div></div>'
        + '</div>';
      const header = ''
        + '<div class="trade-line" style="font-weight:700; color:#555; border-bottom:2px solid #ddd;">'
        +   '<div>Side</div>'
        +   '<div>Trh / Otvoriť</div>'
        +   '<div>Cena</div>'
        +   '<div>Objem (akcie)</div>'
        +   '<div>USDC spolu</div>'
        +   '<div>Čas</div>'
        + '</div>';
      const sorted = trades.slice().sort(function(a, b) {{
        return Number(b.notional || 0) - Number(a.notional || 0);
      }});
      const rows = sorted.slice(0, 12).map(function(t) {{
        const side = String(t.side || '').toUpperCase();
        const sideHtml = sideBadge(side);
        const outcome = t.outcome || '';
        const ts = t.timestamp ? new Date(Number(t.timestamp) * 1000) : null;
        const tsText = ts ? fmtDateTime(ts) : (t.timestampIso || '');
        const usd = Number(t.notional || 0);
        const price = Number(t.price || 0);
        const sizeShares = Number(t.size || 0);
        const title = String(t.title || (m && m.question) || '').replace(/"/g, '&quot;');
        const slug = t.slug || (m && m.slug) || '';
        const tradeUrl = slug ? ('https://polymarket.com/event/' + slug) : 'https://polymarket.com';
        return ''
          + '<div class="trade-line">'
          +   '<div>' + sideHtml + ' <span class="trade-outcome">' + outcome + '</span></div>'
          +   '<div class="trade-market" title="' + title + '"><a href="' + tradeUrl + '" target="_blank" rel="noopener">' + (title || slug || '—') + '</a></div>'
          +   '<div>' + (Number.isFinite(price) ? price.toFixed(3) : '') + '</div>'
          +   '<div>' + (Number.isFinite(sizeShares) ? sizeShares.toLocaleString('sk-SK', {{maximumFractionDigits: 0}}) : '') + '</div>'
          +   '<div><strong>' + (Number.isFinite(usd) ? usd.toLocaleString('sk-SK', {{maximumFractionDigits: 0}}) : '0') + '</strong></div>'
          +   '<div class="small">' + tsText + '</div>'
          + '</div>';
      }}).join('');
      return stats + '<div class="trade-tape">' + header + rows + '</div>';
    }}

    function renderDeltaTracking(m) {{
      const key = m.slug || m.question;
      const delta = currentDeltaMap.get(key);
      if (!delta || delta.summary.length === 0) return '<div class="small">Bez zmien oproti predošlému refreshu.</div>';
      return '<div class="small">' + delta.summary.join(' &middot; ') + '</div>';
    }}

    function fmtPrice(v) {{
      const n = Number(v);
      if (!Number.isFinite(n)) return '';
      return n.toFixed(3);
    }}

    async function logTrade(kind) {{
      if (!selectedMarket) return;
      const note = document.getElementById('savedNote');
      const ad = selectedMarket.autoDraft || {{}};
      const ep = selectedMarket.executionPlan || {{}};
      const payload = {{
        kind: kind,
        slug: selectedMarket.slug,
        question: selectedMarket.question,
        category: selectedMarket.categoryLabel,
        side: ad.finalDecision,
        limitPrice: ep.limitPrice,
        stakeUSDC: ep.stakeUSDC,
        gateScore: selectedMarket.gateScore,
        candidateScore: selectedMarket.candidateScore,
      }};
      if (kind === 'close') {{
        const exitPriceStr = prompt('Zadaj exit price (napr. 0.62):', '');
        const pnlStr = prompt('Zadaj realizovaný PnL v USDC (napr. 12.5 alebo -8):', '');
        payload.exitPrice = exitPriceStr ? Number(exitPriceStr) : null;
        payload.pnl = pnlStr ? Number(pnlStr) : null;
      }}
      try {{
        const res = await fetch('/pnl-log', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify(payload),
        }});
        const data = await res.json();
        if (note) {{
          note.textContent = data.ok ? ('PnL log uź ' + kind + ' zapísaný.') : ('Chyba: ' + (data.error || 'unknown'));
          setTimeout(function(){{ note.textContent=''; }}, 3000);
        }}
      }} catch (err) {{
        if (note) {{ note.textContent = 'Chyba pri zápise PnL: ' + err.message; }}
      }}
    }}

    function logTradeOpen() {{ return logTrade('open'); }}
    function logTradeClose() {{ return logTrade('close'); }}

    function copyTradeLog() {{
      const note = document.getElementById('savedNote');
      if (!selectedMarket) return;
      const text = (selectedMarket.question || '') + '\\n' + (selectedMarket.autoDraft?.finalDecision || '') + '\\nThesis: ' + (selectedMarket.autoDraft?.thesis || '');
      navigator.clipboard.writeText(text).then(function() {{
        if (note) {{ note.textContent = 'Trade-log skopírovaný.'; setTimeout(function(){{ note.textContent=''; }}, 2500); }}
      }});
    }}

    function downloadTradeLog() {{
      if (!selectedMarket) return;
      const blob = new Blob([JSON.stringify(selectedMarket, null, 2)], {{type: 'application/json'}});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = (selectedMarket.slug || 'market') + '.json';
      a.click();
      URL.revokeObjectURL(url);
    }}

    function showDetail(m) {{
      selectedMarket = m;
      const panel = document.getElementById('detailPanel');
      if (!panel) return;
      const tradeUrl = m.slug ? ('https://polymarket.com/event/' + m.slug) : 'https://polymarket.com';
      panel.innerHTML = ''
        + '<div class="detail-shell">'
        +   '<div class="section">'
        +     '<div class="title-row">'
        +       '<div class="title-main">'
        +         '<h3>' + (m.question || '') + '</h3>'
        +         flagBadge(m.flagLabel)
        +         ' ' + decisionBadge((m.autoDraft && m.autoDraft.finalDecision) || 'PASS')
        +         ' ' + catBadge(m.categoryLabel || '')
        +         ' <span class="small">Typ: ' + (m.tradeTypeLabel || '') + '</span>'
        +         ' <span class="small">Gate: ' + (Number.isFinite(Number(m.gateScore)) ? m.gateScore : '') + '/6</span>'
        +       '</div>'
        +       '<a class="trade-link" href="' + tradeUrl + '" target="_blank" rel="noopener noreferrer">Otvoriť trade</a>'
        +     '</div>'
        +     '<div class="detail-top">'
        +       '<div class="panel-muted">'
        +         '<div class="draft-grid">'
        +           '<div>Entry zóna</div><div>' + zoneBadge(m.entryZone) + '</div>'
        +           '<div>Cluster</div><div>' + (m.cluster || '') + '</div>'
        +           '<div>Fail point</div><div>' + (m.failPoint || '') + '</div>'
        +           '<div>Sizing cap</div><div>' + (m.sizingCap || '') + '</div>'
        +           '<div>Why now</div><div>' + (m.whyNow || '') + '</div>'
        +         '</div>'
        +       '</div>'
        +       '<div><h3>Delta tracking</h3>' + renderDeltaTracking(m) + '</div>'
        +     '</div>'
        +   '</div>'
        +   '<div class="detail-grid">'
        +     '<div class="detail-card"><h3>v7 HARD/SOFT checklist</h3>' + renderChecklistV7(m.checklistV7) + '<details style="margin-top:8px"><summary>Legacy 6/6</summary>' + renderChecklist(m.checklist || {{}}) + '</details></div>'
        +     '<div class="detail-card">'
        +       '<h3>Systémový draft</h3>'
        +       '<div class="draft-grid">'
        +         '<div>Bias</div><div>' + ((m.autoDraft && m.autoDraft.bias) || '') + '</div>'
        +         '<div>Rozhodnutie</div><div><strong>' + ((m.autoDraft && m.autoDraft.finalDecision) || '') + '</strong></div>'
        +         '<div>Confidence</div><div>' + ((m.autoDraft && m.autoDraft.confidence) || '') + '/10</div>'
        +         '<div>Sizing hint</div><div>' + ((m.autoDraft && m.autoDraft.sizingHint) || '') + '</div>'
        +       '</div>'
        +       '<label class="block-label">Téza</label><div class="panel-muted">' + ((m.autoDraft && m.autoDraft.thesis) || '') + '</div>'
        +       '<label class="block-label">Mispricing</label><div class="panel-muted">' + ((m.autoDraft && m.autoDraft.mispricing) || '') + '</div>'
        +       '<label class="block-label">Edge</label><div class="panel-muted">' + ((m.autoDraft && m.autoDraft.edge) || '') + '</div>'
        +       '<label class="block-label">Catalyst</label><div class="panel-muted">' + ((m.autoDraft && m.autoDraft.catalyst) || '') + '</div>'
        +       '<label class="block-label">Resolution</label><div class="panel-muted">' + ((m.autoDraft && m.autoDraft.resolution) || '') + '</div>'
        +       '<label class="block-label">Invalidácia</label><div class="panel-muted">' + ((m.autoDraft && m.autoDraft.invalidation) || '') + '</div>'
        +       '<div class="action-row"><button class="btn-primary" onclick="copyTradeLog()">Kopírovať trade-log</button> <button onclick="downloadTradeLog()">Stiahnuť</button> <button onclick="logTradeOpen()">Log open</button> <button onclick="logTradeClose()">Log close</button></div>'
        +       '<div class="saved-note" id="savedNote"></div>'
        +     '</div>'
        +     '<div class="detail-card">'
        +       '<h3>Entry / Exit plán</h3>'
        +       '<div class="draft-grid">'
        +         '<div>Entry side</div><div>' + ((m.executionPlan && m.executionPlan.entrySide) || '') + '</div>'
        +         '<div>Best Bid / Ask</div><div>' + fmtPrice(m.executionPlan && m.executionPlan.bestBid) + ' / ' + fmtPrice(m.executionPlan && m.executionPlan.bestAsk) + ((m.executionPlan && m.executionPlan.spreadPct != null) ? ' <span class="small">(spread ' + m.executionPlan.spreadPct + 'pp)</span>' : '') + '</div>'
        +         '<div><b>BUY limit (maker)</b></div><div><b>' + fmtPrice(m.executionPlan && m.executionPlan.buyLimitPrice) + '</b></div>'
        +         '<div><b>SELL limit (TP1)</b></div><div><b>' + fmtPrice(m.executionPlan && m.executionPlan.sellLimitPrice) + '</b></div>'
        +         '<div>Stake</div><div>' + ((m.executionPlan && m.executionPlan.stakeUSDC) || 0) + ' USDC (' + ((m.executionPlan && m.executionPlan.stakePct) || '0%') + ')</div>'
        +         '<div>Tranche</div><div>' + ((m.executionPlan && m.executionPlan.tranche1USDC) || 0) + ' / ' + ((m.executionPlan && m.executionPlan.tranche2USDC) || 0) + ' / ' + ((m.executionPlan && m.executionPlan.tranche3USDC) || 0) + '</div>'
        +         '<div>TP1 / TP2</div><div>' + ((m.executionPlan && m.executionPlan.takeProfit1) || '') + ' / ' + ((m.executionPlan && m.executionPlan.takeProfit2) || '') + '</div>'
        +       '</div>'
        +       '<div class="panel-muted">' + ((m.executionPlan && m.executionPlan.tp1Action) || '') + '</div>'
        +       '<div class="panel-muted">' + ((m.executionPlan && m.executionPlan.tp2Action) || '') + '</div>'
        +       '<label class="block-label">Runner rule</label><div class="panel-muted">' + ((m.executionPlan && m.executionPlan.runnerRule) || '') + '</div>'
        +       '<label class="block-label">Time-stop</label><div class="panel-muted">' + ((m.executionPlan && m.executionPlan.timeStop) || '') + '</div>'
        +       '<label class="block-label">Full exit trigger</label><div class="panel-muted">' + ((m.executionPlan && m.executionPlan.fullExitTrigger) || '') + '</div>'
        +     '</div>'
        +   '</div>'
        + '</div>';
      const whaleBox = document.getElementById('whaleSignalBox');
      if (whaleBox) whaleBox.innerHTML = renderWhaleSignal(m);
    }}

    function renderTable(markets) {{
      const tbody = document.querySelector('#markets-table tbody');
      if (!tbody) return;
      tbody.innerHTML = '';
      markets.forEach(function(m) {{
        const tr = document.createElement('tr');
        tr.className = 'clickable';
        tr.innerHTML = ''
          + '<td>' + flagBadge(m.flagLabel) + '</td>'
          + '<td>' + decisionBadge((m.autoDraft && m.autoDraft.finalDecision) || 'PASS') + '</td>'
          + '<td>' + zoneBadge(m.entryZone) + '</td>'
          + '<td>' + (Number.isFinite(Number(m.gateScore)) ? m.gateScore : '') + '/6</td>'
          + '<td>' + (Number.isFinite(Number(m.candidateScore)) ? m.candidateScore : '') + '</td>'
          + '<td>' + (m.frictionLabelSk || '') + '</td>'
          + '<td>' + (m.exitLabelSk || '') + '</td>'
          + '<td>' + (m.tradeTypeLabel || '') + '</td>'
          + '<td>' + catBadge(m.categoryLabel || '') + '</td>'
          + '<td>' + oracleBadge(m.oracleRiskLabel || '') + '</td>'
          + '<td class="question-cell"><div class="question-truncate">' + (m.question || '') + '</div></td>'
          + '<td>' + fmtPrice(m.yesPrice) + '</td>'
          + '<td>' + fmtPrice(m.noPrice) + '</td>'
          + '<td>' + fmtInt(m.volume24hr) + '</td>'
          + '<td>' + fmtInt(m.liquidity) + '</td>'
          + '<td>' + fmtDays(m.daysToEnd) + '</td>';
        tr.addEventListener('click', function() {{
          loadMarketTrades(m.slug).then(function() {{ showDetail(m); }});
        }});
        tbody.appendChild(tr);
      }});
      if (markets.length > 0 && !selectedMarket) {{
        loadMarketTrades(markets[0].slug).then(function() {{ showDetail(markets[0]); }});
      }} else if (markets.length > 0 && selectedMarket) {{
        const found = markets.find(function(x) {{ return x.slug === selectedMarket.slug; }});
        if (found) showDetail(found);
      }}
    }}

    async function loadLeaderboard() {{
      try {{
        const res = await fetch('/leaderboard?limit=12');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        cachedLeaders = data.leaders || [];
      }} catch (err) {{
        cachedLeaders = [];
      }}
      renderLeaderboard();
    }}

    async function loadMarkets() {{
      const errorBox = document.getElementById('markets-error');
      if (errorBox) {{ errorBox.style.display = 'none'; errorBox.textContent = ''; }}
      const category = document.getElementById('category')?.value || '';
      const minLiquidity = document.getElementById('minLiquidity')?.value || '';
      const hidePass = document.getElementById('hidePass')?.checked || false;
      const diversify = document.getElementById('diversify')?.checked || false;
      const watchlistOnly = document.getElementById('watchlistOnly')?.checked || false;
      const buyOnly = document.getElementById('buyOnly')?.checked || false;
      const strictMode = document.getElementById('strictMode')?.checked || false;
      updateStatusLine();
      const params = new URLSearchParams({{
        limit: '80',
        min_liquidity: minLiquidity,
        hide_pass: hidePass ? 'true' : 'false',
        diversify: diversify ? 'true' : 'false',
        watchlist_only: watchlistOnly ? 'true' : 'false',
        buy_only: buyOnly ? 'true' : 'false',
        strict_mode: strictMode ? 'true' : 'false'
      }});
      if (category) params.set('category', category);
      try {{
        const res = await fetch('/markets?' + params.toString());
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        cachedMarkets = data.markets || [];
        cachedWatchlist = data.watchlist || [];
        computeDeltaMap(cachedMarkets);
        renderWatchlist();
        renderAlerts();
        renderTable(cachedMarkets);
        const countBox = document.getElementById('countBox');
        if (countBox) countBox.textContent = 'Zobrazené markety: ' + (data.count || 0);
        lastRefreshAt = new Date();
        updateStatusLine();
      }} catch (err) {{
        if (errorBox) {{ errorBox.style.display = 'block'; errorBox.textContent = 'Nepodarilo sa načítať markety: ' + err.message; }}
      }}
    }}

    function renderGlobalWhaleFlow(payload) {{
      const trades = (payload && payload.trades) || [];
      if (trades.length === 0) {{
        return '<div class="small">Zatiaľ žiadne whale obchody nad ' + Number((payload && payload.whaleMinNotional) || 0).toLocaleString('sk-SK') + ' USDC.</div>';
      }}
      let buyUSD = 0, sellUSD = 0, yesUSD = 0, noUSD = 0;
      const marketTotals = new Map();
      trades.forEach(function(t) {{
        const usd = Number(t.notional || 0);
        const side = String(t.side || '').toUpperCase();
        const oc = String(t.outcome || '').toLowerCase();
        if (side === 'BUY') buyUSD += usd; else if (side === 'SELL') sellUSD += usd;
        if (oc.indexOf('yes') >= 0) yesUSD += usd;
        else if (oc.indexOf('no') >= 0) noUSD += usd;
        const key = t.title || t.slug || '—';
        marketTotals.set(key, (marketTotals.get(key) || 0) + usd);
      }});
      const totalUSD = buyUSD + sellUSD;
      const fmt = function(v) {{ return Number(v || 0).toLocaleString('sk-SK', {{maximumFractionDigits: 0}}); }};
      const dominant = (buyUSD >= sellUSD)
        ? ('BUY ' + Math.round(buyUSD / Math.max(totalUSD, 1) * 100) + '%')
        : ('SELL ' + Math.round(sellUSD / Math.max(totalUSD, 1) * 100) + '%');
      let topMarket = '—', topMarketUSD = 0;
      marketTotals.forEach(function(v, k) {{ if (v > topMarketUSD) {{ topMarketUSD = v; topMarket = k; }} }});
      const topMarketShort = topMarket.length > 60 ? topMarket.slice(0, 57) + '...' : topMarket;
      const stats = ''
        + '<div class="whale-stats">'
        +   '<div><div class="stat-label">Počet whale obchodov</div><div class="stat-value">' + trades.length + '</div></div>'
        +   '<div><div class="stat-label">Spolu USDC</div><div class="stat-value">' + fmt(totalUSD) + '</div></div>'
        +   '<div><div class="stat-label">Dominantná strana</div><div class="stat-value">' + dominant + '</div></div>'
        +   '<div><div class="stat-label">Top market (USDC)</div><div class="stat-value" style="font-size:12px;" title="' + topMarket.replace(/"/g, '&quot;') + '">' + topMarketShort + ' · ' + fmt(topMarketUSD) + '</div></div>'
        + '</div>';
      const header = ''
        + '<div class="trade-line" style="font-weight:700; color:#555; border-bottom:2px solid #ddd;">'
        +   '<div>Side</div>'
        +   '<div>Trh / Otvoriť</div>'
        +   '<div>Cena</div>'
        +   '<div>Objem (akcie)</div>'
        +   '<div>USDC spolu</div>'
        +   '<div>Čas</div>'
        + '</div>';
      const rows = trades.map(function(t) {{
        const side = String(t.side || '').toUpperCase();
        const sideHtml = sideBadge(side);
        const outcome = t.outcome || '';
        const ts = t.timestamp ? new Date(Number(t.timestamp) * 1000) : null;
        const tsText = ts ? fmtDateTime(ts) : (t.timestampIso || '');
        const usd = Number(t.notional || 0);
        const price = Number(t.price || 0);
        const sizeShares = Number(t.size || 0);
        const title = String(t.title || '').replace(/"/g, '&quot;');
        const slug = t.slug || '';
        const tradeUrl = slug ? ('https://polymarket.com/event/' + slug) : 'https://polymarket.com';
        return ''
          + '<div class="trade-line">'
          +   '<div>' + sideHtml + ' <span class="trade-outcome">' + outcome + '</span></div>'
          +   '<div class="trade-market" title="' + title + '"><a href="' + tradeUrl + '" target="_blank" rel="noopener">' + (title || slug || '—') + '</a></div>'
          +   '<div>' + (Number.isFinite(price) ? price.toFixed(3) : '') + '</div>'
          +   '<div>' + (Number.isFinite(sizeShares) ? sizeShares.toLocaleString('sk-SK', {{maximumFractionDigits: 0}}) : '') + '</div>'
          +   '<div><strong>' + (Number.isFinite(usd) ? usd.toLocaleString('sk-SK', {{maximumFractionDigits: 0}}) : '0') + '</strong></div>'
          +   '<div class="small">' + tsText + '</div>'
          + '</div>';
      }}).join('');
      return stats + '<div class="trade-tape">' + header + rows + '</div>';
    }}

    async function loadGlobalWhaleFlow() {{
      const box = document.getElementById('globalWhaleBox');
      if (!box) return;
      const minSel = document.getElementById('whaleMinAmount');
      const settlesEl = document.getElementById('includeSettles');
      const closedEl = document.getElementById('includeClosed');
      const minAmount = (minSel && minSel.value) ? minSel.value : '100000';
      const includeSettles = settlesEl && settlesEl.checked ? 'true' : 'false';
      const onlyOngoing = closedEl && closedEl.checked ? 'false' : 'true';
      try {{
        const res = await fetch('/whale-flow?limit=15&min_amount=' + encodeURIComponent(minAmount) + '&include_settles=' + includeSettles + '&only_ongoing=' + onlyOngoing);
        const data = await res.json();
        box.innerHTML = renderGlobalWhaleFlow(data);
      }} catch (err) {{
        box.innerHTML = '<div class="small">Nepodarilo sa načítať globálny whale flow: ' + err.message + '</div>';
      }}
    }}

    function pctFmt(n) {{ return (n>=0?'+':'') + Number(n).toFixed(1) + '%'; }}

    async function loadRiskStatus() {{
      const box = document.getElementById('riskManager');
      if (!box) return;
      try {{
        const res = await fetch('/risk-status');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const d = await res.json();
        const sec = document.getElementById('riskManagerSection');
        if (sec) sec.classList.toggle('no-trade', !d.canTrade);
        const cards = [];
        // Bankroll/Equity
        cards.push('<div class="risk-card"><div class="label">Bankroll</div><div class="value">' + d.bankroll + ' USDC</div><div class="sub">Equity: ' + d.equity + ' (' + pctFmt(((d.equity-d.bankroll)/d.bankroll*100)) + ')</div></div>');
        // Cash
        const cashCls = d.limits.reserveOk ? 'ok' : 'alert';
        cards.push('<div class="risk-card ' + cashCls + '"><div class="label">Cash dostupný</div><div class="value">' + d.cashAvailable + ' USDC</div><div class="sub">Min rezerva: ' + d.cashReserveTarget + '</div></div>');
        // Exposure
        const expCls = d.limits.exposureOk ? 'ok' : 'alert';
        cards.push('<div class="risk-card ' + expCls + '"><div class="label">Total expozícia</div><div class="value">' + d.totalExposure + ' / ' + d.maxTotalExposure + '</div><div class="sub">Max 40% bankrollu</div></div>');
        // Positions
        const posCls = d.limits.positionsOk ? 'ok' : 'alert';
        cards.push('<div class="risk-card ' + posCls + '"><div class="label">Otvorené pozície</div><div class="value">' + d.openPositionsCount + ' / ' + d.maxActivePositions + '</div></div>');
        // Day P&L
        const ddCls = d.limits.drawdownOk ? '' : 'alert';
        cards.push('<div class="risk-card ' + ddCls + '"><div class="label">Dnes P&L</div><div class="value">' + d.todayPnl + ' USDC</div><div class="sub">' + pctFmt(d.todayPnlPct) + ' (limit -15%)</div></div>');
        // Realized
        cards.push('<div class="risk-card"><div class="label">Realized P&L</div><div class="value">' + d.realizedPnl + ' USDC</div></div>');
        // Loss streak
        const lsCls = d.limits.streakOk ? '' : 'alert';
        cards.push('<div class="risk-card ' + lsCls + '"><div class="label">Loss streak</div><div class="value">' + d.lossStreak + ' / ' + d.lossStreakLimit + '</div><div class="sub">Po 3x stop 48h</div></div>');
        // Can trade
        const ctCls = d.canTrade ? 'ok' : 'alert';
        cards.push('<div class="risk-card ' + ctCls + '"><div class="label">Status</div><div class="value">' + (d.canTrade ? 'CAN TRADE' : 'NO TRADE') + '</div><div class="sub">v7 limit checker</div></div>');
        // Open positions list
        if (d.openPositions && d.openPositions.length) {{
          let posHtml = '<div class="risk-card" style="grid-column: 1/-1"><div class="label">Otvorené pozície (' + d.openPositionsCount + ')</div>';
          d.openPositions.forEach(function(p) {{
            posHtml += '<div class="sub" style="font-size:13px;color:#222;margin-top:4px"><strong>' + (p.side||'?') + '</strong> ' + p.usdc + ' USDC @ ' + p.price + ' — ' + (p.question||p.slug) + (p.narrative ? ' (' + p.narrative + ')' : '') + '</div>';
          }});
          posHtml += '</div>';
          cards.push(posHtml);
        }}
        box.innerHTML = cards.join('');
      }} catch (err) {{
        box.innerHTML = '<div class="small">Risk status nedostupný: ' + err.message + '</div>';
      }}
    }}

    async function loadNonSports() {{
      const box = document.getElementById('nonSportsBox');
      if (!box) return;
      try {{
        const res = await fetch('/candidates/non-sports?limit=5');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const d = await res.json();
        if (!d.top || d.top.length === 0) {{
          box.innerHTML = '<div class="small">Žiadny non-sports kandidát nespĺňa v7.0 kvalifikácie (BUY + hardOK + spread ≤ 6pp + expiry 2–60d).</div>';
          return;
        }}
        let html = '<table style="width:100%;border-collapse:collapse;font-size:13px;">';
        html += '<thead><tr style="background:#f0f0f0;text-align:left;"><th style="padding:6px;">#</th><th style="padding:6px;">Score</th><th style="padding:6px;">Market</th><th style="padding:6px;">Typ</th><th style="padding:6px;">Smer</th><th style="padding:6px;">Cena</th><th style="padding:6px;">Edge pp</th><th style="padding:6px;">BUY @</th><th style="padding:6px;">SELL TP1</th><th style="padding:6px;">Stake</th><th style="padding:6px;">Expiry</th><th style="padding:6px;">Kateg.</th><th style="padding:6px;">Link</th></tr></thead><tbody>';
        d.top.forEach(function(t, i) {{
          const ep = t.executionPlan || {{}};
          const he = t.hardEdge || {{}};
          const price = t.yesPrice != null ? Number(t.yesPrice).toFixed(3) : '-';
          const buyAt = ep.buyLimitPrice != null ? Number(ep.buyLimitPrice).toFixed(3) : '-';
          const tp1 = ep.takeProfit1 != null ? Number(ep.takeProfit1).toFixed(3) : '-';
          const stake = ep.stakeUSDC != null ? ep.stakeUSDC : '-';
          const edge = he.afterCostEdgePp != null ? (Number(he.afterCostEdgePp).toFixed(1) + 'pp') : '-';
          const days = t.daysToEnd != null ? (Number(t.daysToEnd).toFixed(0) + 'd') : '-';
          const url = t.polymarketUrl || ('https://polymarket.com/event/' + t.slug);
          const q = (t.question || t.slug || '').replace(/</g,'&lt;');
          html += '<tr style="border-top:1px solid #e0e0e0;">';
          html += '<td style="padding:6px;">' + (i+1) + '</td>';
          html += '<td style="padding:6px;font-weight:600;">' + t.candidateScore + '</td>';
          html += '<td style="padding:6px;max-width:340px;">' + q + '</td>';
          html += '<td style="padding:6px;">' + (t.tradeType || '-') + '</td>';
          html += '<td style="padding:6px;">' + (t.finalDecision || '-') + '</td>';
          html += '<td style="padding:6px;">' + price + '</td>';
          html += '<td style="padding:6px;">' + edge + '</td>';
          html += '<td style="padding:6px;">' + buyAt + '</td>';
          html += '<td style="padding:6px;">' + tp1 + '</td>';
          html += '<td style="padding:6px;">' + stake + ' USDC</td>';
          html += '<td style="padding:6px;">' + days + '</td>';
          html += '<td style="padding:6px;">' + (t.category || '-') + '</td>';
          html += '<td style="padding:6px;"><a href="' + url + '" target="_blank" rel="noopener">otvoriť</a></td>';
          html += '</tr>';
        }});
        html += '</tbody></table>';
        html += '<div class="small" style="margin-top:6px;color:#666;">Kvalifikovaní: ' + d.totalQualified + ' · generated: ' + (d.generatedAt || '').slice(0,19) + 'Z</div>';
        box.innerHTML = html;
      }} catch (err) {{
        box.innerHTML = '<div class="small">Nepodarilo sa načítať non-sports kandidátov: ' + err.message + '</div>';
      }}
    }}

    async function loadAll() {{
      await Promise.all([loadMarkets(), loadGlobalWhaleFlow(), loadRiskStatus(), loadNonSports()]);
    }}

    document.getElementById('refreshBtn')?.addEventListener('click', loadAll);
    document.getElementById('strictMode')?.addEventListener('change', function() {{ updateStatusLine(); }});
    document.getElementById('buyOnly')?.addEventListener('change', loadMarkets);
    document.getElementById('whaleMinAmount')?.addEventListener('change', loadGlobalWhaleFlow);
    document.getElementById('includeSettles')?.addEventListener('change', loadGlobalWhaleFlow);
    document.getElementById('includeClosed')?.addEventListener('change', loadGlobalWhaleFlow);

    loadAll();
    scheduleNextRefresh();
  </script>
</body>
</html>
"""
    return html


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
