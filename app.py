import os
import requests
import json
import re
import time
import threading
from flask import Flask, jsonify, request
from collections import defaultdict
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

### ============================================================================
### POLYMARKET SNIPER v2.1 — Hybrid Professional Edition
### ----------------------------------------------------------------------------
### Integrated Framework: v2.0 PDF Pillars + v11.2 Quality Score Engine
### Deployment: Optimized for Render / Single Dyno
### Bankroll: 500 USDC fixed · Max exposure: 200 · Reserve: 150
### Tiers: A=30, B=15, C<=10 USDC · Max 3 CORE + 4 SANDBOX positions
### ============================================================================

### ============================================================================
### CONFIGURATION (Merged from v2.0 and v11.2)
### ============================================================================
APP_CONFIG = {
    "title": "Polymarket Hybrid Sniper v2.1",
    "version": "v2.1-Hybrid-Full",
    "bankroll_usdc": 500.0,
    "max_total_exposure": 200.0,
    "cash_reserve": 150.0,
    "max_core_positions": 3,
    "max_sandbox_positions": 4,
    "max_narrative_exposure": 70.0,
    
    # Sizing Tiers (Pillar 2 - v2.0 PDF)
    "tier_a_stake": 30.0,
    "tier_b_stake": 15.0,
    "tier_c_stake": 10.0,
    
    # Quality Score Thresholds (v11.2)
    "qs_tier_a_min": 24,
    "qs_tier_b_min": 17,
    "qs_near_miss": 14,
    
    "default_min_liquidity": 10000.0,
    "whale_trade_min_notional": 200000.0,
}

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"

# --- In-memory Cache for API requests ---
_CACHE = {}
_CACHE_LOCK = threading.Lock()

def cache_get(namespace, key):
    full_key = (namespace, key)
    now = time.time()
    with _CACHE_LOCK:
        entry = _CACHE.get(full_key)
        if entry and entry["expires"] > now:
            return entry["value"]
        return None

def cache_set(namespace, key, value, ttl=60):
    full_key = (namespace, key)
    with _CACHE_LOCK:
        _CACHE[full_key] = {"value": value, "expires": time.time() + ttl}

### ============================================================================
### PERSISTENCE LAYER (From main.py-old)
### ============================================================================
STATE_DIR = os.environ.get("STATE_DIR", "/tmp/polymarket_state")
os.makedirs(STATE_DIR, exist_ok=True)
STATE_FILE = os.path.join(STATE_DIR, "state.json")
PNL_LOG_FILE = os.path.join(STATE_DIR, "pnl_log.jsonl")
_STATE_LOCK = threading.Lock()

def load_state():
    with _STATE_LOCK:
        if not os.path.exists(STATE_FILE):
            return {"alerts": [], "watchlistSnapshot": [], "updatedAt": None, "catalysts": []}
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {"alerts": [], "watchlistSnapshot": [], "updatedAt": None, "catalysts": []}

def save_state(state):
    state["updatedAt"] = datetime.now(timezone.utc).isoformat()
    with _STATE_LOCK:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

def append_pnl_log(entry):
    entry["loggedAt"] = datetime.now(timezone.utc).isoformat()
    with _STATE_LOCK:
        with open(PNL_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

### ============================================================================
### UTILITIES
### ============================================================================
def to_float(value, default=0.0):
    try: return float(value) if value is not None and value != "" else default
    except: return default

def safe_int(value, default=0):
    try: return int(float(value)) if value is not None and value != "" else default
    except: return default

def parse_json_list(value):
    if isinstance(value, list): return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except: return []
    return []

def short_wallet(addr):
    if not addr or len(addr) < 12: return addr or ""
    return f"{addr[:6]}...{addr[-4:]}"

def clamp_price(x):
    if x is None: return None
    return max(0.01, min(0.99, round(x, 3)))

### ============================================================================
### POLYMARKET API CLIENT
### ============================================================================
def fetch_active_markets(limit=250):
    try:
        r = requests.get(f"{GAMMA_BASE}/markets", params={"limit": limit, "active": "true", "closed": "false"}, timeout=20)
        r.raise_for_status()
        return r.json()
    except: return []

def fetch_positions(wallet):
    if not wallet: return []
    try:
        r = requests.get(f"{DATA_API_BASE}/positions", params={"user": wallet, "sizeThreshold": 1}, timeout=20)
        return r.json() if isinstance(r.json(), list) else r.json().get("data", [])
    except: return []

### ============================================================================
### CATEGORIZATION & EDGE DETECTION (Full lists from app.py)
### ============================================================================
def categorize_market(question):
    q = (question or "").lower()
    if any(k in q for k in ["ukraine", "nato", "israel", "gaza", "iran", "war"]): return "Geopolitics"
    if any(k in q for k in ["presidential", "trump", "election", "senate"]): return "Politics"
    if any(k in q for k in ["bitcoin", "btc", "eth", "crypto"]): return "Crypto"
    if any(k in q for k in ["nba", "fifa", "world cup", "nfl"]): return "Sports"
    return "Other"

def detect_edge_type(question):
    q = (question or "").lower()
    if any(k in q for k in ["letters", "words", "specific", "rules"]): return "text", "Textual rules gap"
    if any(k in q for k in ["official source", "sole discretion", "resolves"]): return "oracle", "Oracle ambiguity"
    return None, "Directional market"

### ============================================================================
### COGNITIVE-BIAS PROTOKOLS (Pillar 1/4 - v2.0 PDF)
### ============================================================================
def flag_unverified_sources(description, resolution_source):
    """PROTOCOL 1 — Zero-Tolerance Verification [9]."""
    combined = f"{description or ''} {resolution_source or ''}".lower()
    noise = ["smart money", "whale flow", "reportedly", "rumored", "analysts say"]
    hits = [s for s in noise if s in combined]
    if hits:
        return {"flagged": True, "note": f"PROTOCOL 1: Unverified signals ({', '.join(hits)}). TREAT AS NOISE."}
    return {"flagged": False, "note": "PROTOCOL 1: OK."}

def information_edge_test(question):
    """PROTOCOL 3 — Information Edge Test [3]."""
    q = (question or "").lower()
    generic = ["inflation", "cpi", "gdp", "btc price", "bitcoin price"]
    if any(s in q for s in generic):
        return {"forcePass": True, "reason": "PROTOCOL 3: Generic macro/crypto. AUTO PASS."}
    return {"forcePass": False, "reason": "Edge test continues."}

### ============================================================================
### QUALITY SCORE ENGINE (v11.2 Logic - From main.py-old)
### ============================================================================
def compute_quality_score_v11(market):
    """Quality Score 0–30 (v11.2 Logic) [10]."""
    score = 0
    q = market.get("question", "").lower()
    
    # Factor 1: Edge Type
    edge_type, _ = detect_edge_type(q)
    if edge_type in ["text", "oracle"]: score += 5
    
    # Factor 2: Catalyst (v11.2 Logic)
    days = (datetime.fromisoformat(market.get("endDate").replace("Z", "+00:00")) - datetime.now(timezone.utc)).days if market.get("endDate") else 90
    if any(k in q for k in ["vote", "election", "fomc"]): score += 5
    elif days <= 7: score += 3
    
    # Factor 3: Liquidity
    liq = to_float(market.get("liquidity", 0))
    if liq > 50000: score += 5
    elif liq > 10000: score += 2
    
    return score

### ============================================================================
### THE 4 PILLARS (v2.0 PDF)
### ============================================================================
def kill_switch_check(market, edge_type, open_clusters):
    """Pillar 1: 3 binary questions [6]."""
    q1 = edge_type is not None
    cluster = detect_cluster(market.get("question", ""), categorize_market(market.get("question")))
    q2 = open_clusters.get(cluster, 0) < APP_CONFIG["max_narrative_exposure"]
    q3 = to_float(market.get("liquidity")) >= APP_CONFIG["default_min_liquidity"]
    return q1 and q2 and q3

def detect_cluster(question, category):
    q = (question or "").lower()
    if "ukraine" in q: return "Ukraine"
    if any(k in q for k in ["israel", "gaza", "iran"]): return "Middle East"
    return f"{category}: misc"

def build_exit_plan(yes_price):
    """Pillar 3: Mechanical exit [8]."""
    if yes_price < 0.25:
        return "FREE-ROLL (Immediately recover 100% principal)"
    elif yes_price > 0.40:
        return "TIME-STOP (Mandatory exit before resolution)"
    return "STANDARD (TP1 @ +50%)"

### ============================================================================
### ENDPOINTS (Merged & Complete)
### ============================================================================
@app.route("/")
def home():
    return jsonify({"app": APP_CONFIG["title"], "version": APP_CONFIG["version"]})

@app.route("/analyze-market", methods=["POST"])
def analyze_market_endpoint():
    payload = request.get_json(force=True, silent=True) or {}
    market = payload.get("market", {})
    open_clusters = payload.get("open_clusters", {})
    
    # Protocols
    p3 = information_edge_test(market.get("question", ""))
    if p3["forcePass"]: return jsonify({"decision": "PASS", "reason": p3["reason"]})
    
    # Pillar 1
    edge_type, edge_reason = detect_edge_type(market.get("question"))
    ks_pass = kill_switch_check(market, edge_type, open_clusters)
    if not ks_pass: return jsonify({"decision": "PASS", "reason": "Kill-Switch Fail"})
    
    # Pillar 2 - Quality Score
    qs = compute_quality_score_v11(market)
    if qs >= APP_CONFIG["qs_tier_a_min"]: tier, stake = "A (CORE)", APP_CONFIG["tier_a_stake"]
    elif qs >= APP_CONFIG["qs_tier_b_min"]: tier, stake = "B (CORE)", APP_CONFIG["tier_b_stake"]
    else: tier, stake = "C (SANDBOX)", APP_CONFIG["tier_c_stake"]
    
    return jsonify({
        "decision": "PROCEED",
        "tier": tier,
        "stake": stake,
        "quality_score": f"{qs}/30",
        "exit_plan": build_exit_plan(to_float(market.get("yes_price", 0.5))),
        "devils_advocate": "Loss Scenario: Oracle ambiguity or liquidity drain"
    })

@app.route("/dashboard")
def dashboard():
    return "<h1>Polymarket Hybrid Dashboard v2.1</h1><p>Status: Running</p>"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False) k in combined]
    return {"status": "NOISE" if hits else "SIGNAL", "reason": hits}

def information_edge_test(question):
    """Auto-pass for generic macro/crypto lotteries (No Edge) [12]."""
    q = question.lower()
    macro = ["cpi", "inflation", "gdp", "fed rate", "unemployment"]
    crypto = ["bitcoin price", "btc price", "eth price", "will btc reach"]
    if any(k in q for k in macro + crypto):
        return True # Immediate Pass (No info edge)
    return False

def kill_switch_check(market, edge_type, open_clusters):
    """v2.0 Pillar 1: 3 Binary Questions (Edge, Correlation, Liquidity) [13]."""
    q1_edge = edge_type is not None
    
    cluster = detect_cluster(market.get("question", ""))
    current_exposure = open_clusters.get(cluster, 0.0)
    q2_correlation = current_exposure < APP_CONFIG["max_narrative_exposure"]
    
    q3_liquidity = float(market.get("liquidity", 0)) >= APP_CONFIG["default_min_liquidity"]
    
    return q1_edge and q2_correlation and q3_liquidity

def compute_quality_score_v11(market):
    """Points-based scoring (0-30) for Tier classification [4, 5]."""
    score = 0
    q = market.get("question", "").lower()
    
    # Factor 1: Edge Type (5 pts for Text/Oracle) [5, 14]
    edge_type, _ = detect_edge_type(q)
    if edge_type in ["text", "oracle"]: score += 5
    
    # Factor 2: Liquidity (5 pts for >50k) [5]
    if float(market.get("liquidity", 0)) > 50000: score += 5
    
    # Factor 3: Catalyst Strength (5 pts for High) [15]
    _, strength = detect_catalyst(q)
    if strength == "High": score += 5
    
    return score

# ============================================================================
# 4. CLASSIFICATION & EXIT ENGINE (English Only) [7, 16-18]
# ============================================================================

def classify_trade(quality_score):
    """Maps score to v2.0 Tiers and USDC stake amounts [4, 19]."""
    if quality_score >= APP_CONFIG["qs_tier_a_min"]:
        return "A (CORE)", APP_CONFIG["tier_a_stake"]
    elif quality_score >= APP_CONFIG["qs_tier_b_min"]:
        return "B (CORE)", APP_CONFIG["tier_b_stake"]
    elif quality_score >= APP_CONFIG["qs_near_miss"]:
        return "C (SANDBOX)", APP_CONFIG["tier_c_stake"]
    return "PASS", 0.0

def build_exit_plan(yes_price):
    """v2.0 Pillar 3: Mechanical Exit Strategy [16]."""
    if yes_price < 0.25:
        return {"strategy": "FREE-ROLL", "note": "Limit sell to recover 100% principal immediately."}
    elif yes_price > 0.40:
        return {"strategy": "TIME-STOP", "note": "Mandatory sell before resolution date."}
    return {"strategy": "STANDARD", "note": "Target +50% profit for first exit."}

# ============================================================================
# 5. CATEGORIZATION HELPERS [20-23]
# ============================================================================

def detect_edge_type(question):
    q = question.lower()
    if any(k in q for k in ["letters", "words", "specific", "rules"]): return "text", "Textual Edge"
    if any(k in q for k in ["discretion", "resolves", "official"]): return "oracle", "Oracle/Interpretation Gap"
    return None, "Directional Lottery"

def detect_catalyst(question):
    q = question.lower()
    if any(k in q for k in ["election", "vote", "fomc", "earnings"]): return "Event", "High"
    return "Time", "Medium"

def detect_cluster(question):
    q = question.lower()
    if "ukraine" in q: return "Ukraine"
    if any(k in q for k in ["israel", "iran", "gaza"]): return "Middle East"
    if "trump" in q or "election" in q: return "US Politics"
    return "Misc"

# ============================================================================
# 6. DASHBOARD ENDPOINTS [24-26]
# ============================================================================

@app.route("/analyze", methods=["POST"])
def analyze_market():
    payload = request.json
    market = payload.get("market", {})
    open_clusters = payload.get("open_clusters", {}) # Current narrative exposure
    
    question = market.get("question", "")
    
    # Step 1: Info Edge Check [12]
    if information_edge_test(question):
        return jsonify({"decision": "PASS", "reason": "No info edge (Generic Macro/Crypto)"})
    
    # Step 2: Verification Protocol [11]
    verif = protocol_1_verification(market.get("desc"), market.get("src"))
    if verif["status"] == "NOISE":
        return jsonify({"decision": "PASS", "reason": "Protokol 1: Unverified Noise", "details": verif["reason"]})
    
    # Step 3: Kill Switch [13]
    edge_type, _ = detect_edge_type(question)
    if not kill_switch_check(market, edge_type, open_clusters):
        return jsonify({"decision": "PASS", "reason": "Kill-Switch Fail (Liquidity/Correlation/No Edge)"})
    
    # Step 4: Scoring & Classification [5, 17]
    qs = compute_quality_score_v11(market)
    tier, stake = classify_trade(qs)
    
    if tier == "PASS":
        return jsonify({"decision": "PASS", "reason": "Low Quality Score", "qs": qs})

    return jsonify({
        "decision": "PROCEED",
        "tier": tier,
        "stake_usdc": stake,
        "quality_score": f"{qs}/30",
        "exit_plan": build_exit_plan(market.get("yes_price", 0.5)),
        "devils_advocate": "Loss Scenario: Oracle ambiguity or sudden liquidity drain" # Pillar 4 [27]
    })

@app.route("/narrative-map")
def get_narrative_map():
    """Returns narrative exposure mapping for the dashboard [6]."""
    # Placeholder: In a live environment, this reads pnl_log.jsonl to calculate sums
    return jsonify({"max_per_narrative": APP_CONFIG["max_narrative_exposure"], "status": "Ready"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
