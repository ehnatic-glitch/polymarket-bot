from flask import Flask, jsonify, request
import requests
import json
import re
import os
from collections import defaultdict
from datetime import datetime, timezone

app = Flask(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
APP_CONFIG = {
    "dashboard_title": "Polymarket kandidátny dashboard",
    "default_min_liquidity": 100000.0,
    "bankroll_total": 500.0,
    "cash_reserve": 150.0,
    "max_total_exposure": 200.0,
    "max_narrative_exposure": 75.0,
    "max_active_positions": 3,
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


def get_yes_no_prices(market):
    prices = parse_json_list(market.get("outcomePrices"))

    yes_price = None
    no_price = None

    if len(prices) >= 2:
        yes_price = to_float(prices[0], None)
        no_price = to_float(prices[1], None)
    elif len(prices) == 1:
        yes_price = to_float(prices[0], None)
        no_price = (1 - yes_price) if isinstance(yes_price, (int, float)) else None
    else:
        best_bid = to_float(market.get("bestBid"), None)
        best_ask = to_float(market.get("bestAsk"), None)

        if isinstance(best_bid, (int, float)):
            yes_price = best_bid
        if isinstance(best_ask, (int, float)):
            no_price = 1 - best_ask if isinstance(best_ask, (int, float)) else None

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
        "world cup", "nba finals", "nfl", "mlb", "stanley cup",
        "champions league", "premier league", "ufc", "fifa",
        "win the 2026", "win the finals", "win the world cup"
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
    q = (question or "").lower()

    if isinstance(yes_price, (int, float)) and 0.01 <= yes_price <= 0.05:
        return "Centovka"

    resolution_keywords = [
        "called by", "out by", "official sources", "good faith",
        "materially", "substantially", "at any time"
    ]
    if any(k in q for k in resolution_keywords):
        return "Resolution"

    if days_to_end is not None and days_to_end <= 7:
        return "Time Decay"

    momentum_keywords = [
        "ipo", "ceasefire", "announcement", "report", "vote", "deadline", "earnings"
    ]
    if any(k in q for k in momentum_keywords):
        return "Momentum"

    return "Other"


def oracle_risk_level(question):
    q = (question or "").lower()
    high_risk_keywords = [
        "good faith", "sole discretion", "official sources only",
        "materially", "substantially", "at any time"
    ]
    medium_risk_keywords = [
        "called by", "out by", "military clash", "any country leave",
        "official sources"
    ]

    if any(k in q for k in high_risk_keywords):
        return "High"
    if any(k in q for k in medium_risk_keywords):
        return "Medium"
    return "Low"


def detect_catalyst(question, days_to_end):
    q = (question or "").lower()

    if any(k in q for k in ["vote", "voting", "election", "runoff"]):
        return ("Vote/Election", "High")
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


def decision_bias(flag, yes_price, oracle_risk, trade_type, gate_score, fr_score, ex_score):
    if flag == "PASS":
        return "No trade", "PASS"

    if not isinstance(yes_price, (int, float)):
        return "No trade", "PASS"

    if oracle_risk == "High":
        return "No trade", "PASS"

    if yes_price < 0.15:
        if gate_score >= 5 and fr_score >= 3 and ex_score >= 2 and trade_type == "Centovka":
            return "Lean YES", "BUY YES"
        return "Lean NO", "BUY NO"

    if yes_price > 0.85:
        return "Lean NO", "BUY NO"

    if 0.15 <= yes_price <= 0.45:
        if flag == "WATCH" and gate_score >= 5:
            return "Lean YES", "BUY YES"

    if 0.55 <= yes_price <= 0.85:
        if flag == "WATCH" and gate_score >= 5:
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


def sizing_cap_from_v6(flag, trade_type, final_decision):
    if final_decision == "PASS":
        return "0 USDC"
    if trade_type == "Centovka":
        return "5–12 USDC"
    if flag == "WATCH":
        return "25–40 USDC"
    return "10–20 USDC"


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


def build_execution_plan(flag, trade_type, final_decision, yes_price, no_price, liquidity, volume24hr, days_to_end):
    if final_decision == "PASS":
        return {
            "entrySide": "NONE",
            "limitPrice": None,
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

    return {
        "entrySide": entry_side,
        "limitPrice": limit_price,
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
            sizing_hint = "Orientačný sizing podľa v6: 5–12 USDC."
        elif flag == "WATCH":
            sizing_hint = "Orientačný sizing podľa v6: 25–40 USDC len ak zostane setup čistý."
        else:
            sizing_hint = "Orientačný sizing podľa v6: 10–20 USDC až po ďalšom potvrdení edge-u."

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
    oracle_risk = oracle_risk_level(raw_question)

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
    gate_catalyst = catalyst_confidence in ["High", "Medium"] and days_to_end is not None and days_to_end <= 180
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
        if 7 <= days_to_end <= 180:
            score += 2
            notes.append("good_time_window")
        elif 3 <= days_to_end < 7:
            score += 1
            notes.append("near_time_window")
        elif days_to_end < 1:
            score -= 4
            notes.append("too_close_to_expiry")
        elif days_to_end < 3:
            score -= 3
            notes.append("close_to_expiry")
        elif days_to_end > 365:
            score -= 3
            notes.append("too_far_expiry")
        elif days_to_end > 180:
            score -= 1
            notes.append("far_expiry")
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
        "missing_price" in fr_notes
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

    execution_plan = build_execution_plan(
        flag=flag,
        trade_type=trade_type,
        final_decision=auto_draft["finalDecision"],
        yes_price=yes_price,
        no_price=no_price,
        liquidity=liquidity,
        volume24hr=volume24hr,
        days_to_end=days_to_end,
    )

    fail = fail_point(checklist, oracle_risk, notes)
    sizing_cap = sizing_cap_from_v6(flag, trade_type, auto_draft["finalDecision"])
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
        entry_zone["code"] in ["entry", "near"]
    )

    return {
        "candidateScore": score,
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
        "autoDraft": scored["autoDraft"],
        "executionPlan": scored["executionPlan"],
        "failPoint": scored["failPoint"],
        "sizingCap": scored["sizingCap"],
        "cluster": scored["cluster"],
        "entryZone": scored["entryZone"],
        "whyNow": scored["whyNow"],
        "isWatchlist": scored["isWatchlist"],
        "strictModeApplied": scored["strictModeApplied"],
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


def apply_diversity(rows, diversify=True, max_per_category=3, max_per_cluster=2):
    if not diversify:
        return rows

    category_counts = defaultdict(int)
    cluster_counts = defaultdict(int)
    selected = []

    for row in rows:
        cat = row.get("category", "Other")
        cluster = row.get("cluster", "misc")

        if category_counts[cat] >= max_per_category:
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


def build_watchlist(rows, limit=12):
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
    return watch[:limit]


def build_portfolio_summary(rows):
    selected = []
    narrative_buckets = defaultdict(float)

    for row in rows:
        decision = row.get("autoDraft", {}).get("finalDecision")
        if decision not in ["BUY YES", "BUY NO"]:
            continue
        selected.append(row)

    selected.sort(
        key=lambda x: (
            decision_priority(x.get("autoDraft", {}).get("finalDecision")),
            -to_float(x.get("gateScore")),
            -to_float(x.get("candidateScore")),
        )
    )

    selected = selected[:APP_CONFIG["max_active_positions"]]

    total_exposure = 0.0
    positions = []

    for row in selected:
        stake = to_float(row.get("executionPlan", {}).get("stakeUSDC"), 0.0)
        cluster = row.get("cluster") or "misc"
        narrative_buckets[cluster] += stake
        total_exposure += stake
        positions.append({
            "question": row.get("question"),
            "cluster": cluster,
            "decision": row.get("autoDraft", {}).get("finalDecision"),
            "stakeUSDC": stake,
            "limitPrice": row.get("executionPlan", {}).get("limitPrice"),
            "entryZone": row.get("entryZone", {}).get("label"),
        })

    reserve = APP_CONFIG["cash_reserve"]
    bankroll_total = APP_CONFIG["bankroll_total"]
    max_total = APP_CONFIG["max_total_exposure"]
    free_risk = max(0.0, max_total - total_exposure)

    warnings = []
    if total_exposure > max_total:
        warnings.append("Prekročený max celkový risk 200 USDC.")
    if len(positions) > APP_CONFIG["max_active_positions"]:
        warnings.append("Prekročený max počet aktívnych pozícií.")
    for cluster, amount in narrative_buckets.items():
        if amount > APP_CONFIG["max_narrative_exposure"]:
            warnings.append(f"Prekročený max narrative cap 75 USDC pre {cluster}.")

    return {
        "bankrollTotal": bankroll_total,
        "cashReserve": reserve,
        "maxTotalExposure": max_total,
        "activeExposure": round(total_exposure, 2),
        "freeRiskBudget": round(free_risk, 2),
        "activePositions": len(positions),
        "positions": positions,
        "narrativeExposure": [
            {"cluster": k, "exposure": round(v, 2)}
            for k, v in sorted(narrative_buckets.items(), key=lambda x: -x[1])
        ],
        "warnings": warnings,
    }


@app.route("/markets")
def markets():
    limit = int(request.args.get("limit", "80"))
    active = request.args.get("active", "true")
    closed = request.args.get("closed", "false")
    min_liquidity = to_float(
        request.args.get("min_liquidity", str(APP_CONFIG["default_min_liquidity"]))
    )
    hide_pass = request.args.get("hide_pass", "true").lower() == "true"
    category_filter = request.args.get("category", "").strip()
    diversify = request.args.get("diversify", "true").lower() == "true"
    watchlist_only = request.args.get("watchlist_only", "false").lower() == "true"
    strict_mode = request.args.get("strict_mode", "false").lower() == "true"

    params = {
        "limit": 250,
        "active": active,
        "closed": closed,
    }

    url = f"{GAMMA_BASE}/markets"
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    rows = []
    for m in data:
        if m.get("active") is not True:
            continue
        if m.get("closed") is True:
            continue

        row = build_market_row(m, strict_mode=strict_mode)

        if to_float(row.get("liquidity")) < min_liquidity:
            continue
        if hide_pass and row.get("autoDraft", {}).get("finalDecision") == "PASS":
            continue
        if category_filter and row.get("category") != category_filter:
            continue
        if watchlist_only and not row.get("isWatchlist"):
            continue

        rows.append(row)

    rows.sort(
        key=lambda x: (
            decision_priority(x.get("autoDraft", {}).get("finalDecision")),
            flag_priority(x.get("flagLabel")),
            0 if x.get("category") != "Sports" else 1,
            -to_float(x.get("gateScore")),
            oracle_priority(x.get("oracleRisk")),
            -to_float(x.get("candidateScore")),
            -to_float(x.get("liquidity")),
            -to_float(x.get("volume24hr")),
        )
    )

    diversified_rows = apply_diversity(
        rows,
        diversify=diversify,
        max_per_category=3,
        max_per_cluster=2,
    )

    diversified_rows = diversified_rows[:limit]
    alt_non_sports = top_non_sports(rows, limit=3)
    watchlist = build_watchlist(rows, limit=12)
    portfolio = build_portfolio_summary(watchlist)

    return jsonify({
        "count": len(diversified_rows),
        "markets": diversified_rows,
        "topNonSports": alt_non_sports,
        "watchlist": watchlist,
        "portfolio": portfolio,
        "filters": {
            "min_liquidity": min_liquidity,
            "hide_pass": hide_pass,
            "category": category_filter,
            "diversify": diversify,
            "watchlist_only": watchlist_only,
            "strict_mode": strict_mode,
        }
    })


@app.route("/markets/top")
def markets_top():
    return markets()


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

    return f"""
<!doctype html>
<html lang="sk">
<head>
  <meta charset="utf-8">
  <title>{title} v6.0.1</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 16px;
      background: #f5f5f5;
      color: #222;
    }}
    h1, h2, h3 {{
      margin-bottom: 0.3rem;
    }}
    .section {{
      background: #ffffff;
      padding: 14px;
      border-radius: 8px;
      margin-bottom: 16px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 2.2fr) minmax(420px, 1fr);
      gap: 14px;
      align-items: start;
    }}
    .controls {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 10px;
      align-items: end;
    }}
    .control {{
      display: flex;
      flex-direction: column;
      gap: 4px;
      min-width: 150px;
    }}
    label {{
      font-size: 12px;
      color: #555;
      font-weight: 600;
    }}
    input, select {{
      padding: 7px 9px;
      border: 1px solid #ddd;
      border-radius: 6px;
      font: inherit;
      background: #fff;
    }}
    .checkbox-wrap {{
      display: flex;
      align-items: center;
      gap: 7px;
      padding-top: 20px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 12.5px;
      table-layout: fixed;
    }}
    th, td {{
      padding: 5px 6px;
      border-bottom: 1px solid #eee;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: #fafafa;
      font-weight: 700;
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    tr.clickable {{
      cursor: pointer;
    }}
    tr.clickable:hover {{
      background: #fafcff;
    }}
    .small {{
      font-size: 12px;
      color: #555;
    }}
    .error {{
      color: #c5221f;
      margin-bottom: 8px;
      font-size: 13px;
    }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .watch {{
      background: #e6f4ea;
      color: #137333;
    }}
    .review {{
      background: #fff4e5;
      color: #b06000;
    }}
    .pass {{
      background: #fce8e6;
      color: #c5221f;
    }}
    .decision-buy-yes {{
      background: #e8f5e9;
      color: #137333;
    }}
    .decision-buy-no {{
      background: #e8f0fe;
      color: #1558d6;
    }}
    .decision-pass {{
      background: #f3f4f6;
      color: #555;
    }}
    .cat {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      background: #eef2ff;
      color: #3949ab;
      font-weight: 600;
      white-space: nowrap;
    }}
    .risk-low {{
      color: #137333;
      font-weight: 700;
    }}
    .risk-medium {{
      color: #b06000;
      font-weight: 700;
    }}
    .risk-high {{
      color: #c5221f;
      font-weight: 700;
    }}
    .zone-entry {{
      color: #137333;
      font-weight: 700;
    }}
    .zone-near {{
      color: #b06000;
      font-weight: 700;
    }}
    .zone-far {{
      color: #666;
      font-weight: 700;
    }}
    .zone-chase {{
      color: #c5221f;
      font-weight: 700;
    }}
    .zone-none {{
      color: #888;
      font-weight: 700;
    }}
    .panel {{
      position: sticky;
      top: 16px;
    }}
    .panel-box {{
      background: #fff;
      border-radius: 8px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.08);
      padding: 14px;
    }}
    .panel-muted {{
      color: #444;
      font-size: 13px;
      line-height: 1.5;
      background: #fafafa;
      border: 1px solid #eee;
      border-radius: 6px;
      padding: 10px;
    }}
    .table-wrap {{
      overflow: auto;
      max-height: 62vh;
      border-radius: 8px;
    }}
    .count {{
      margin-bottom: 10px;
      font-size: 13px;
      color: #444;
    }}
    .status-line {{
      margin-bottom: 10px;
      font-size: 12px;
      color: #666;
      background: #fafafa;
      border: 1px solid #eee;
      border-radius: 6px;
      padding: 8px 10px;
      line-height: 1.5;
    }}
    button {{
      padding: 8px 12px;
      border: 1px solid #ddd;
      border-radius: 6px;
      background: #fff;
      cursor: pointer;
    }}
    .btn-primary {{
      background: #1558d6;
      border-color: #1558d6;
      color: white;
    }}
    .check {{
      display: flex;
      align-items: flex-start;
      gap: 8px;
      padding: 8px 0;
      border-bottom: 1px solid #f0f0f0;
      font-size: 13px;
    }}
    .check:last-child {{
      border-bottom: none;
    }}
    .ok {{
      color: #137333;
      font-weight: 700;
      min-width: 28px;
    }}
    .no {{
      color: #c5221f;
      font-weight: 700;
      min-width: 28px;
    }}
    .metric-pill {{
      display: inline-block;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      margin-right: 6px;
      margin-bottom: 6px;
      background: #f3f4f6;
      color: #333;
    }}
    .delta-up {{
      color: #137333;
      font-weight: 700;
    }}
    .delta-down {{
      color: #c5221f;
      font-weight: 700;
    }}
    .delta-flat {{
      color: #666;
      font-weight: 700;
    }}
    .action-row {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 12px;
    }}
    .title-row {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 8px;
    }}
    .title-main {{
      min-width: 0;
      flex: 1;
    }}
    .title-main h3 {{
      margin: 0 0 8px 0;
      line-height: 1.25;
    }}
    .trade-link {{
      white-space: nowrap;
      font-size: 13px;
      text-decoration: none;
      color: #1558d6;
      font-weight: 600;
      margin-top: 2px;
    }}
    .trade-link:hover {{
      text-decoration: underline;
    }}
    .journal-box, .non-sports-box, .watchlist-box, .portfolio-box, .alerts-box {{
      margin-top: 16px;
      padding-top: 12px;
      border-top: 1px solid #eee;
    }}
    .saved-note {{
      margin-top: 8px;
      font-size: 12px;
      color: #137333;
    }}
    .draft-grid {{
      display: grid;
      grid-template-columns: 120px 1fr;
      gap: 8px;
      font-size: 13px;
      margin-bottom: 10px;
    }}
    .draft-grid div:first-child {{
      color: #666;
      font-weight: 600;
    }}
    .block-label {{
      margin-top: 10px;
      display: block;
      font-size: 12px;
      color: #555;
      font-weight: 600;
    }}
    .question-cell {{
      max-width: 320px;
    }}
    .question-truncate {{
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      line-height: 1.35;
      max-height: 2.7em;
      word-break: break-word;
    }}
    .mini-list {{
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }}
    .mini-card {{
      border: 1px solid #eee;
      border-radius: 6px;
      padding: 8px;
      background: #fafafa;
      font-size: 12px;
      line-height: 1.4;
    }}
    .mini-card strong {{
      display: block;
      margin-bottom: 4px;
    }}
    .top-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }}
    .warning {{
      color: #c5221f;
      font-weight: 700;
      font-size: 12px;
      margin-top: 6px;
    }}

    .w-flag {{ width: 92px; }}
    .w-decision {{ width: 92px; }}
    .w-zone {{ width: 92px; }}
    .w-gate {{ width: 50px; }}
    .w-score {{ width: 46px; }}
    .w-friction {{ width: 82px; }}
    .w-exit {{ width: 76px; }}
    .w-type {{ width: 72px; }}
    .w-cat {{ width: 58px; }}
    .w-oracle {{ width: 56px; }}
    .w-price {{ width: 50px; }}
    .w-vol {{ width: 74px; }}
    .w-liq {{ width: 82px; }}
    .w-days {{ width: 42px; }}

    @media (max-width: 1200px) {{
      .layout {{
        grid-template-columns: 1fr;
      }}
      .panel {{
        position: static;
      }}
      .top-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <p class="small">v6.0.1: delta tracking, watchlist, entry zóna, portfolio guardrails, rare alerts, strict v6 mode. [file:454][cite:544]</p>

  <div class="top-grid">
    <div class="section">
      <h2>Watchlist</h2>
      <div id="watchlistBox" class="panel-muted">Načítavam watchlist...</div>
    </div>
    <div class="section">
      <h2>Portfolio panel</h2>
      <div id="portfolioBox" class="panel-muted">Načítavam portfolio panel...</div>
    </div>
    <div class="section">
      <h2>Rare alerts</h2>
      <div id="alertsBox" class="panel-muted">Zatiaľ bez alertov.</div>
    </div>
  </div>

  <div class="layout">
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
          <input type="checkbox" id="strictMode" />
          <label for="strictMode">Strict v6 mode</label>
        </div>

        <div class="control">
          <button onclick="loadMarkets(true)">Obnoviť</button>
        </div>
      </div>

      <div class="status-line" id="statusLine">Dashboard sa inicializuje...</div>
      <div class="count" id="countBox"></div>
      <div id="markets-error" class="error" style="display:none;"></div>

      <div class="table-wrap">
        <table id="markets-table">
          <thead>
            <tr>
              <th class="w-flag">Kandidát</th>
              <th class="w-decision">Rozhod.</th>
              <th class="w-zone">Entry zóna</th>
              <th class="w-gate">Gate</th>
              <th class="w-score">Skóre</th>
              <th class="w-friction">Frikcia</th>
              <th class="w-exit">Exit</th>
              <th class="w-type">Typ</th>
              <th class="w-cat">Kat.</th>
              <th class="w-oracle">Oracle</th>
              <th>Otázka</th>
              <th class="w-price">Yes</th>
              <th class="w-price">No</th>
              <th class="w-vol">24h</th>
              <th class="w-liq">Likv.</th>
              <th class="w-days">Dni</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </div>

    <div class="panel">
      <div class="panel-box" id="detailPanel">
        <h3>Detail marketu</h3>
        <p class="panel-muted">Klikni na riadok v tabuľke a zobrazí sa checklist, delta tracking, systémový draft a entry/exit plán. [file:454]</p>
      </div>
    </div>
  </div>

  <script>
    let cachedMarkets = [];
    let selectedMarket = null;
    let cachedNonSports = [];
    let cachedWatchlist = [];
    let cachedPortfolio = null;
    let previousSnapshot = new Map();
    let currentDeltaMap = new Map();
    let rareAlerts = [];
    let autoRefreshTimer = null;
    let lastRefreshAt = null;

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

    function getScheduleInfo(now = new Date()) {{
      const currentHour = now.getHours();
      const today = new Date(now);
      const next = new Date(now);

      for (const hour of REFRESH_HOURS) {{
        if (currentHour < hour || (currentHour === hour && now.getMinutes() === 0 && now.getSeconds() === 0)) {{
          next.setHours(hour, 0, 0, 0);
          return {{
            inWindow: currentHour >= 7 && currentHour <= 21,
            nextRefresh: next
          }};
        }}
      }}

      const tomorrow = new Date(today);
      tomorrow.setDate(tomorrow.getDate() + 1);
      tomorrow.setHours(7, 0, 0, 0);

      return {{
        inWindow: false,
        nextRefresh: tomorrow
      }};
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
      const windowText = info.inWindow
        ? 'Sme v aktívnom okne 07:00–21:00.'
        : 'Sme mimo aktívneho okna 07:00–21:00.';
      const strictOn = document.getElementById('strictMode')?.checked ? 'ON' : 'OFF';

      el.innerHTML =
        'Dashboard aktuálny k: <strong>' + lastText + '</strong><br>' +
        'Aktuálny dátum a čas: <strong>' + fmtDateTime(now) + '</strong><br>' +
        'Plán refreshu: <strong>07:00, 09:00, 11:00, 13:00, 15:00, 17:00, 19:00, 21:00</strong><br>' +
        'Strict v6 mode: <strong>' + strictOn + '</strong><br>' +
        windowText + ' Ďalší plánovaný refresh: <strong>' + nextText + '</strong>';
    }}

    function scheduleNextRefresh() {{
      if (autoRefreshTimer) clearTimeout(autoRefreshTimer);

      const waitMs = msUntilNextRefresh();
      autoRefreshTimer = setTimeout(async () => {{
        await loadMarkets();
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

    function renderChecklist(checklist) {{
      const order = [
        ['resolutability', 'Resolutability'],
        ['baseRate', 'Base Rate'],
        ['friction', 'Frikcia'],
        ['exit', 'Exit'],
        ['catalyst', 'Catalyst'],
        ['oracle', 'Oracle Trap']
      ];

      return order.map(([key, label]) => {{
        const item = checklist[key];
        return `
          <div class="check">
            <div class="${{item.ok ? 'ok' : 'no'}}">${{item.ok ? 'ÁNO' : 'NIE'}}</div>
            <div><strong>${{label}}</strong><br>${{item.note || ''}}</div>
          </div>
        `;
      }}).join('');
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

      markets.forEach(m => {{
        const key = m.slug || m.question;
        const prev = previousSnapshot.get(key);

        const snap = {{
          yesPrice: m.yesPrice,
          noPrice: m.noPrice,
          flag: m.flag,
          flagLabel: m.flagLabel,
          decision: m.autoDraft?.finalDecision || 'PASS',
          gateScore: m.gateScore,
          liquidity: m.liquidity,
          entryZone: m.entryZone?.code || 'none',
          oracleRisk: m.oracleRiskLabel,
        }};
        nextMap.set(key, snap);

        const delta = {{
          yesDelta: null,
          noDelta: null,
          gateDelta: null,
          flagChanged: false,
          decisionChanged: false,
          liquidityDeltaPct: null,
          summary: [],
        }};

        if (prev) {{
          if (Number.isFinite(Number(snap.yesPrice)) && Number.isFinite(Number(prev.yesPrice))) {{
            delta.yesDelta = Number((snap.yesPrice - prev.yesPrice).toFixed(3));
            if (Math.abs(delta.yesDelta) >= 0.02) {{
              delta.summary.push('YES ' + fmtDelta(delta.yesDelta));
            }}
          }}

          if (Number.isFinite(Number(snap.noPrice)) && Number.isFinite(Number(prev.noPrice))) {{
            delta.noDelta = Number((snap.noPrice - prev.noPrice).toFixed(3));
          }}

          if (Number.isFinite(Number(snap.gateScore)) && Number.isFinite(Number(prev.gateScore))) {{
            delta.gateDelta = Number(snap.gateScore) - Number(prev.gateScore);
            if (delta.gateDelta !== 0) {{
              delta.summary.push('Gate ' + (delta.gateDelta > 0 ? '+' : '') + delta.gateDelta);
            }}
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

          if (snap.flag === 'WATCH' && prev.flag !== 'WATCH') {{
            alerts.push('Nový WATCH: ' + (m.question || ''));
          }}
          if (prev.decision === 'PASS' && (snap.decision === 'BUY YES' || snap.decision === 'BUY NO')) {{
            alerts.push('PASS -> ' + snap.decision + ': ' + (m.question || ''));
          }}
          if (prev.entryZone !== 'entry' && snap.entryZone === 'entry') {{
            alerts.push('Market vošiel do entry zóny: ' + (m.question || ''));
          }}
          if (snap.oracleRisk === 'Vysoké' && prev.oracleRisk !== 'Vysoké') {{
            alerts.push('Oracle risk vyskočil na vysoké: ' + (m.question || ''));
          }}
          if (Number.isFinite(delta.liquidityDeltaPct) && delta.liquidityDeltaPct <= -30) {{
            alerts.push('Likvidita prudko padla: ' + (m.question || ''));
          }}
          if (m.daysToEnd !== null && Number(m.daysToEnd) <= 3 && (snap.decision === 'BUY YES' || snap.decision === 'BUY NO')) {{
            alerts.push('Blíži sa time-stop / expiry risk: ' + (m.question || ''));
          }}
        }}

        deltaMap.set(key, delta);
      }});

      previousSnapshot = nextMap;
      currentDeltaMap = deltaMap;
      rareAlerts = alerts.slice(0, 10);
    }}

    function renderWatchlist() {{
      const box = document.getElementById('watchlistBox');
      if (!box) return;

      if (!cachedWatchlist || cachedWatchlist.length === 0) {{
        box.innerHTML = 'Žiadne watchlist kandidáty podľa aktuálnych filtrov.';
        return;
      }}

      box.innerHTML = cachedWatchlist.map(m => `
        <div class="mini-card">
          <strong>${{m.question || ''}}</strong>
          ${{decisionBadge(m.autoDraft?.finalDecision || 'PASS')}}
          ${{zoneBadge(m.entryZone)}}
          ${{pill('Gate: ' + (m.gateScore ?? '') + '/6')}}
          <div style="margin-top:6px;">${{m.whyNow || ''}}</div>
        </div>
      `).join('');
    }}

    function renderPortfolio() {{
      const box = document.getElementById('portfolioBox');
      if (!box) return;

      const p = cachedPortfolio;
      if (!p) {{
        box.innerHTML = 'Portfolio panel nie je dostupný.';
        return;
      }}

      let html = `
        <div>${{pill('Bankroll: ' + fmtInt(p.bankrollTotal) + ' USDC')}}</div>
        <div>${{pill('Rezerva: ' + fmtInt(p.cashReserve) + ' USDC')}}</div>
        <div>${{pill('Active risk: ' + fmtInt(p.activeExposure) + ' USDC')}}</div>
        <div>${{pill('Free risk: ' + fmtInt(p.freeRiskBudget) + ' USDC')}}</div>
        <div>${{pill('Pozície: ' + (p.activePositions || 0) + '/3')}}</div>
      `;

      if (p.narrativeExposure && p.narrativeExposure.length) {{
        html += '<div style="margin-top:8px;">' + p.narrativeExposure.map(x =>
          pill(x.cluster + ': ' + fmtInt(x.exposure) + ' USDC')
        ).join(' ') + '</div>';
      }}

      if (p.warnings && p.warnings.length) {{
        html += p.warnings.map(w => '<div class="warning">' + w + '</div>').join('');
      }}

      box.innerHTML = html;
    }}

    function renderAlerts() {{
      const box = document.getElementById('alertsBox');
      if (!box) return;

      if (!rareAlerts || rareAlerts.length === 0) {{
        box.innerHTML = 'Zatiaľ bez rare alertov.';
        return;
      }}

      box.innerHTML = rareAlerts.map(a => `
        <div class="mini-card">${{a}}</div>
      `).join('');
    }}

    function buildTradeLogText(m) {{
      return `KATEGORIZÁCIA TRHU: ${{m.tradeTypeLabel || ''}}

PRE-TRADE CHECKLIST:
1. Resolutability: ${{(m.checklist?.resolutability?.ok ? 'ÁNO' : 'NIE')}} - ${{m.checklist?.resolutability?.note || ''}}
2. Base Rate: ${{(m.checklist?.baseRate?.ok ? 'ÁNO' : 'NIE')}} - ${{m.checklist?.baseRate?.note || ''}}
3. Frikcia: ${{(m.checklist?.friction?.ok ? 'ÁNO' : 'NIE')}} - ${{m.checklist?.friction?.note || ''}}
4. Exit: ${{(m.checklist?.exit?.ok ? 'ÁNO' : 'NIE')}} - ${{m.checklist?.exit?.note || ''}}
5. Catalyst: ${{(m.checklist?.catalyst?.ok ? 'ÁNO' : 'NIE')}} - ${{m.checklist?.catalyst?.note || ''}}
6. Oracle Trap: ${{(m.checklist?.oracle?.ok ? 'ÁNO' : 'NIE')}} - ${{m.checklist?.oracle?.note || ''}}

ANALÝZA EDGE-U:
${{m.autoDraft?.thesis || ''}}

KDE JE MISPRICING:
${{m.autoDraft?.mispricing || ''}}

TYP EDGE:
${{m.autoDraft?.edge || ''}}

RESOLUTION ANALÝZA:
${{m.autoDraft?.resolution || ''}}

WHY NOW:
${{m.whyNow || ''}}

PARAMETRE VSTUPU:
Market: ${{m.question || ''}}
Entry side: ${{m.executionPlan?.entrySide || ''}}
Limit price: ${{m.executionPlan?.limitPrice ?? ''}}
Stake: ${{m.executionPlan?.stakeUSDC || 0}} USDC
Tranches: ${{m.executionPlan?.tranche1USDC || 0}} / ${{m.executionPlan?.tranche2USDC || 0}} / ${{m.executionPlan?.tranche3USDC || 0}} USDC
Entry zone: ${{m.entryZone?.label || ''}}
YES cena: ${{fmtPrice(m.yesPrice)}}
NO cena: ${{fmtPrice(m.noPrice)}}
Likvidita: ${{fmtInt(m.liquidity)}}
24h objem: ${{fmtInt(m.volume24hr)}}
Dni do expirácie: ${{fmtDays(m.daysToEnd)}}

PLÁN VÝSTUPU:
TP1: ${{m.executionPlan?.takeProfit1 || ''}} / predať ${{m.executionPlan?.tp1Pct || 0}}%
TP2: ${{m.executionPlan?.takeProfit2 || ''}} / predať ${{m.executionPlan?.tp2Pct || 0}}%
Runner: ${{m.executionPlan?.runnerPct || 0}}%
TP1 action: ${{m.executionPlan?.tp1Action || ''}}
TP2 action: ${{m.executionPlan?.tp2Action || ''}}
Runner rule: ${{m.executionPlan?.runnerRule || ''}}
Time-stop: ${{m.executionPlan?.timeStop || ''}}

INVALIDÁCIA (FULL EXIT TRIGGER):
${{m.executionPlan?.fullExitTrigger || ''}}

KATALYZÁTOR:
${{m.autoDraft?.catalyst || ''}}

CONFIDENCE:
${{m.autoDraft?.confidence || ''}}/10

BIAS:
${{m.autoDraft?.bias || ''}}

FINÁLNE ROZHODNUTIE:
${{m.autoDraft?.finalDecision || 'PASS'}}
`;
    }}

    async function copyTradeLog() {{
      if (!selectedMarket) return;
      const text = buildTradeLogText(selectedMarket);
      await navigator.clipboard.writeText(text);
      const msg = document.getElementById('savedNote');
      if (msg) msg.textContent = 'Trade-log šablóna skopírovaná do schránky.';
    }}

    function downloadTradeLog() {{
      if (!selectedMarket) return;
      const text = buildTradeLogText(selectedMarket);
      const blob = new Blob([text], {{ type: 'text/plain;charset=utf-8' }});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      const safeSlug = (selectedMarket.slug || 'trade-log').replace(/[^a-z0-9-]/gi, '-');
      a.download = safeSlug + '-trade-log.txt';
      a.click();
      URL.revokeObjectURL(a.href);
      const msg = document.getElementById('savedNote');
      if (msg) msg.textContent = 'Trade-log šablóna stiahnutá.';
    }}

    function renderNonSports() {{
      if (!cachedNonSports || cachedNonSports.length === 0) {{
        return `
          <div class="non-sports-box">
            <h3>Top mimo športu</h3>
            <div class="panel-muted">Aktuálne nebol nájdený žiadny zaujímavý non-sports kandidát podľa tvojich filtrov.</div>
          </div>
        `;
      }}

      const cards = cachedNonSports.map(m => `
        <div class="mini-card">
          <strong>${{m.question || ''}}</strong>
          ${{decisionBadge(m.autoDraft?.finalDecision || 'PASS')}}
          ${{catBadge(m.categoryLabel || 'Ostatné')}}
          ${{pill('Gate: ' + (m.gateScore ?? '') + '/6')}}
          <div style="margin-top:6px;">Cluster: ${{m.cluster || ''}}</div>
        </div>
      `).join('');

      return `
        <div class="non-sports-box">
          <h3>Top mimo športu</h3>
          <div class="mini-list">${{cards}}</div>
        </div>
      `;
    }}

    function renderDeltaSection(m) {{
      const key = m.slug || m.question;
      const d = currentDeltaMap.get(key);

      if (!d) {{
        return '<div class="panel-muted">Delta tracking zatiaľ nie je k dispozícii.</div>';
      }}

      const yesLine = d.yesDelta === null
        ? 'YES delta: bez predošlého snapshotu.'
        : 'YES delta: <span class="' + deltaClass(d.yesDelta) + '">' + fmtDelta(d.yesDelta) + '</span>';

      const gateLine = d.gateDelta === null
        ? 'Gate delta: bez predošlého snapshotu.'
        : 'Gate delta: <span class="' + deltaClass(d.gateDelta) + '">' + (d.gateDelta > 0 ? '+' : '') + d.gateDelta + '</span>';

      const liqLine = d.liquidityDeltaPct === null
        ? 'Likvidita delta: bez predošlého snapshotu.'
        : 'Likvidita delta: <span class="' + deltaClass(d.liquidityDeltaPct) + '">' + (d.liquidityDeltaPct > 0 ? '+' : '') + d.liquidityDeltaPct.toFixed(1) + '%</span>';

      const summary = d.summary && d.summary.length
        ? d.summary.join(' | ')
        : 'Bez významnej zmeny od posledného refreshu.';

      return `
        <div class="panel-muted">
          ${yesLine}<br>
          ${gateLine}<br>
          ${liqLine}<br>
          Zhrnutie: ${summary}
        </div>
      `;
    }}

    function showDetail(m) {{
      selectedMarket = m;
      const panel = document.getElementById('detailPanel');
      const link = m.slug ? 'https://polymarket.com/market/' + m.slug : '';

      panel.innerHTML = `
        <div class="title-row">
          <div class="title-main">
            <h3>${{m.question || 'Detail marketu'}}</h3>
            <div>
              ${{flagBadge(m.flagLabel)}}
              ${{decisionBadge(m.autoDraft?.finalDecision || 'PASS')}}
              ${{catBadge(m.categoryLabel)}}
              ${{pill('Typ: ' + (m.tradeTypeLabel || 'Ostatné'))}}
              ${{pill('Gate: ' + (m.gateScore ?? '') + '/6')}}
              ${{pill('Entry zóna: ' + (m.entryZone?.label || ''))}}
            </div>
          </div>
          <div>
            ${{link ? '<a class="trade-link" href="' + link + '" target="_blank" rel="noopener noreferrer">Otvoriť trade</a>' : ''}}
          </div>
        </div>

        <div class="draft-grid"><div>Cluster</div><div>${{m.cluster || ''}}</div></div>
        <div class="draft-grid"><div>Fail point</div><div>${{m.failPoint || ''}}</div></div>
        <div class="draft-grid"><div>Sizing cap</div><div>${{m.sizingCap || ''}}</div></div>
        <div class="draft-grid"><div>Why now</div><div>${{m.whyNow || ''}}</div></div>

        <h3 style="margin-top:14px;">Delta tracking</h3>
        ${{renderDeltaSection(m)}}

        <h3 style="margin-top:14px;">Mini 6/6 checklist</h3>
        ${{renderChecklist(m.checklist || {{}})}}

        <div class="journal-box">
          <h3>Systémový draft</h3>

          <div class="draft-grid"><div>Bias</div><div>${{m.autoDraft?.bias || ''}}</div></div>
          <div class="draft-grid"><div>Rozhodnutie</div><div><strong>${{m.autoDraft?.finalDecision || ''}}</strong></div></div>
          <div class="draft-grid"><div>Confidence</div><div>${{m.autoDraft?.confidence || ''}}/10</div></div>
          <div class="draft-grid"><div>Sizing hint</div><div>${{m.autoDraft?.sizingHint || ''}}</div></div>

          <label class="block-label">Téza</label>
          <div class="panel-muted">${{m.autoDraft?.thesis || ''}}</div>

          <label class="block-label">Mispricing</label>
          <div class="panel-muted">${{m.autoDraft?.mispricing || ''}}</div>

          <label class="block-label">Edge</label>
          <div class="panel-muted">${{m.autoDraft?.edge || ''}}</div>

          <label class="block-label">Katalyzátor</label>
          <div class="panel-muted">${{m.autoDraft?.catalyst || ''}}</div>

          <label class="block-label">Resolution analýza</label>
          <div class="panel-muted">${{m.autoDraft?.resolution || ''}}</div>
        </div>

        <div class="journal-box">
          <h3>Entry / Exit plán</h3>

          <div class="draft-grid"><div>Side</div><div>${{m.executionPlan?.entrySide || ''}}</div></div>
          <div class="draft-grid"><div>Limit price</div><div>${{m.executionPlan?.limitPrice ?? ''}}</div></div>
          <div class="draft-grid"><div>Entry zóna</div><div>${{m.entryZone?.label || ''}}</div></div>
          <div class="draft-grid"><div>Stake</div><div>${{m.executionPlan?.stakeUSDC || 0}} USDC (${{m.executionPlan?.stakePct || '0%'}})</div></div>
          <div class="draft-grid"><div>Tranches</div><div>${{m.executionPlan?.tranche1USDC || 0}} / ${{m.executionPlan?.tranche2USDC || 0}} / ${{m.executionPlan?.tranche3USDC || 0}} USDC</div></div>

          <div class="draft-grid"><div>TP1</div><div>${{m.executionPlan?.takeProfit1 || ''}} / predať ${{m.executionPlan?.tp1Pct || 0}}%</div></div>
          <div class="draft-grid"><div>TP2</div><div>${{m.executionPlan?.takeProfit2 || ''}} / predať ${{m.executionPlan?.tp2Pct || 0}}%</div></div>
          <div class="draft-grid"><div>Runner</div><div>Nechať ${{m.executionPlan?.runnerPct || 0}}%</div></div>

          <label class="block-label">TP1 action</label>
          <div class="panel-muted">${{m.executionPlan?.tp1Action || ''}}</div>

          <label class="block-label">TP2 action</label>
          <div class="panel-muted">${{m.executionPlan?.tp2Action || ''}}</div>

          <label class="block-label">Runner rule</label>
          <div class="panel-muted">${{m.executionPlan?.runnerRule || ''}}</div>

          <label class="block-label">Time stop</label>
          <div class="panel-muted">${{m.executionPlan?.timeStop || ''}}</div>

          <label class="block-label">Full exit trigger</label>
          <div class="panel-muted">${{m.executionPlan?.fullExitTrigger || ''}}</div>

          <div class="action-row">
            <button class="btn-primary" onclick="copyTradeLog()">Kopírovať trade-log šablónu</button>
            <button onclick="downloadTradeLog()">Stiahnuť trade-log</button>
          </div>

          <div class="saved-note" id="savedNote"></div>
        </div>

        ${{renderNonSports()}}
      `;
    }}

    async function loadMarkets() {{
      const errorEl = document.getElementById('markets-error');
      const tbody = document.querySelector('#markets-table tbody');
      const countBox = document.getElementById('countBox');

      const category = document.getElementById('category').value;
      const minLiquidity = document.getElementById('minLiquidity').value;
      const hidePass = document.getElementById('hidePass').checked;
      const diversify = document.getElementById('diversify').checked;
      const watchlistOnly = document.getElementById('watchlistOnly').checked;
      const strictMode = document.getElementById('strictMode').checked;

      try {{
        const params = new URLSearchParams({{
          limit: '120',
          min_liquidity: minLiquidity,
          hide_pass: hidePass ? 'true' : 'false',
          category: category,
          diversify: diversify ? 'true' : 'false',
          watchlist_only: watchlistOnly ? 'true' : 'false',
          strict_mode: strictMode ? 'true' : 'false'
        }});

        const res = await fetch('/markets?' + params.toString());
        if (!res.ok) throw new Error('HTTP ' + res.status);

        const data = await res.json();
        const markets = data.markets || [];
        cachedNonSports = data.topNonSports || [];
        cachedWatchlist = data.watchlist || [];
        cachedPortfolio = data.portfolio || null;

        computeDeltaMap(markets);

        let selectedSlug = selectedMarket?.slug || null;
        cachedMarkets = markets;
        tbody.innerHTML = '';

        markets.forEach((m, idx) => {{
          const tr = document.createElement('tr');
          tr.className = 'clickable';

          tr.innerHTML = `
            <td>${{flagBadge(m.flagLabel)}}</td>
            <td>${{decisionBadge(m.autoDraft?.finalDecision || 'PASS')}}</td>
            <td>${{zoneBadge(m.entryZone)}}</td>
            <td>${{m.gateScore ?? ''}}/6</td>
            <td>${{m.candidateScore ?? ''}}</td>
            <td>${{m.frictionLabelSk || ''}}</td>
            <td>${{m.exitLabelSk || ''}}</td>
            <td>${{m.tradeTypeLabel || ''}}</td>
            <td>${{catBadge(m.categoryLabel)}}</td>
            <td>${{oracleBadge(m.oracleRiskLabel)}}</td>
            <td class="question-cell"><div class="question-truncate">${{m.question || ''}}</div></td>
            <td>${{fmtPrice(m.yesPrice)}}</td>
            <td>${{fmtPrice(m.noPrice)}}</td>
            <td>${{fmtInt(m.volume24hr)}}</td>
            <td>${{fmtInt(m.liquidity)}}</td>
            <td>${{fmtDays(m.daysToEnd)}}</td>
          `;

          tr.addEventListener('click', () => {{
            showDetail(cachedMarkets[idx]);
          }});

          tbody.appendChild(tr);
        }});

        countBox.textContent = 'Zobrazené markety: ' + markets.length;
        errorEl.style.display = 'none';
        lastRefreshAt = new Date();
        updateStatusLine();
        renderWatchlist();
        renderPortfolio();
        renderAlerts();

        if (markets.length > 0) {{
          const matched = selectedSlug ? markets.find(x => x.slug === selectedSlug) : null;
          showDetail(matched || markets[0]);
        }} else {{
          document.getElementById('detailPanel').innerHTML =
            '<h3>Detail marketu</h3><p class="panel-muted">Žiadny market nevyhovuje aktuálnym filtrom.</p>' +
            renderNonSports();
        }}
      }} catch (err) {{
        errorEl.textContent = 'Chyba pri načítaní marketov: ' + err.message;
        errorEl.style.display = 'block';
      }}
    }}

    document.getElementById('category').addEventListener('change', loadMarkets);
    document.getElementById('minLiquidity').addEventListener('change', loadMarkets);
    document.getElementById('hidePass').addEventListener('change', loadMarkets);
    document.getElementById('diversify').addEventListener('change', loadMarkets);
    document.getElementById('watchlistOnly').addEventListener('change', loadMarkets);
    document.getElementById('strictMode').addEventListener('change', () => {{
      updateStatusLine();
      loadMarkets();
    }});

    loadMarkets();
    updateStatusLine();
    scheduleNextRefresh();
    setInterval(updateStatusLine, 1000);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)