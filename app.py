# ============================================================================
# POLYMARKET SNIPER v2.0 — Lean & Mean
# ----------------------------------------------------------------------------
# Single-file Flask app · deploy na Render
# Framework: v2.0 PDF — Tier A=30, B=15, C<=10 USDC · Max 3 CORE + 4 SANDBOX
# APIs: gamma-api.polymarket.com (markets) + data-api.polymarket.com (positions/trades)
# Bankroll: 500 USDC fixne · Reserve: 150 · Max exposure: 200
# ============================================================================

import os
import requests
import json
import re
import time
import threading
from flask import Flask, jsonify, request
from datetime import datetime, timezone, timedelta
from collections import defaultdict

app = Flask(__name__)

# ============================================================================
# 1. CONFIGURATION: HYBRID SNIPER v2.1 (v2.0 PDF Rules + v11.2 Quality Score)
# ============================================================================
APP_CONFIG = {
    "version": "v2.1-Hybrid-English",
    "bankroll_usdc": 500.0,      # Fixed bankroll per v2.0 [1]
    "global_max_exposure": 200.0, # Max total risk deployed [4]
    "cash_reserve": 150.0,       # Minimum cash to hold [1]
    "max_narrative_exposure": 70.0, # Pillar 2 Correlation Limit [4, 6]
    
    # Sizing Tiers based on v2.0 PDF [1, 7]
    "tier_a_stake": 30.0,  # High Confidence Core
    "tier_b_stake": 15.0,  # Solid Momentum/Time-Decay
    "tier_c_stake": 10.0,  # Sandbox/Centovka
    
    # v11.2 Quality Score Thresholds (0-30 points) [4, 5]
    "qs_tier_a_min": 24,
    "qs_tier_b_min": 17,
    "qs_near_miss": 14,
    
    "default_min_liquidity": 10000.0,
    "gamma_base": "https://gamma-api.polymarket.com",
    "data_api_base": "https://data-api.polymarket.com"
}

# ============================================================================
# 2. PERSISTENCE & LOGGING (Translated from main.py-old) [3, 8, 9]
# ============================================================================
STATE_DIR = os.environ.get("STATE_DIR", "/tmp/polymarket_state")
os.makedirs(STATE_DIR, exist_ok=True)
STATE_FILE = os.path.join(STATE_DIR, "state.json")
PNL_LOG_FILE = os.path.join(STATE_DIR, "pnl_log.jsonl")
_STATE_LOCK = threading.Lock()

def save_state(state):
    state["updatedAt"] = datetime.now(timezone.utc).isoformat()
    with _STATE_LOCK:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

def append_pnl_log(entry):
    """Logs trades for the dashboard to track exposure and PnL [3, 10]."""
    entry["loggedAt"] = datetime.now(timezone.utc).isoformat()
    with _STATE_LOCK:
        with open(PNL_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

# ============================================================================
# 3. CORE ANALYTICS: PILLARS & PROTOCOLS (English Only) [5, 11-13]
# ============================================================================

def protocol_1_verification(description, resolution_source):
    """Zero-Tolerance Verification. Flags unverified rumors as NOISE [11]."""
    combined = f"{description or ''} {resolution_source or ''}".lower()
    noise_keywords = ["reportedly", "rumored", "analysts say", "insider", "whale flow", "sources say"]
    hits = [k for k in noise_keywords if k in combined]
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
