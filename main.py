from flask import Flask, jsonify, request
import requests
import json
import re
from collections import defaultdict
from datetime import datetime, timezone

app = Flask(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"


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
        ]
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
            no_price = 1 - best_ask

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


def score_market(m):
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
            7 <= days_to_end <= 120
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

    fail = fail_point(checklist, oracle_risk, notes)
    sizing_cap = sizing_cap_from_v6(flag, trade_type, auto_draft["finalDecision"])
    cluster = detect_cluster(raw_question, category)

    return {
        "candidateScore": score,
        "flag": flag,
        "flagLabel": flag,
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
        "failPoint": fail,
        "sizingCap": sizing_cap,
        "cluster": cluster,
    }


def build_market_row(m):
    scored = score_market(m)

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
        "failPoint": scored["failPoint"],
        "sizingCap": scored["sizingCap"],
        "cluster": scored["cluster"],
    }


def flag_priority(flag):
    if flag == "WATCH":
        return 0
    if flag == "REVIEW":
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


def apply_diversity(rows, mode="strict", diversify=True, max_per_category=None, max_per_cluster=None):
    if not diversify:
        return rows

    if max_per_category is None:
        max_per_category = 3 if mode == "strict" else 4
    if max_per_cluster is None:
        max_per_cluster = 2

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


@app.route("/markets")
def markets():
    limit = int(request.args.get("limit", "80"))
    active = request.args.get("active", "true")
    closed = request.args.get("closed", "false")
    min_liquidity = to_float(request.args.get("min_liquidity", "0"))
    min_volume = to_float(request.args.get("min_volume", "0"))
    hide_pass = request.args.get("hide_pass", "true").lower() == "true"
    category_filter = request.args.get("category", "").strip()
    trade_type_filter = request.args.get("trade_type", "").strip()
    max_oracle_risk = request.args.get("max_oracle_risk", "").strip()
    gate_only = request.args.get("gate_only", "false").lower() == "true"
    catalyst_conf_filter = request.args.get("catalyst_confidence", "").strip()
    mode = request.args.get("mode", "strict").strip().lower()
    diversify = request.args.get("diversify", "true").lower() == "true"
    max_per_category = request.args.get("max_per_category")
    max_per_cluster = request.args.get("max_per_cluster")

    max_per_category = int(max_per_category) if max_per_category not in [None, ""] else None
    max_per_cluster = int(max_per_cluster) if max_per_cluster not in [None, ""] else None

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

        row = build_market_row(m)

        if to_float(row.get("liquidity")) < min_liquidity:
            continue
        if to_float(row.get("volume24hr")) < min_volume:
            continue
        if hide_pass and row.get("autoDraft", {}).get("finalDecision") == "PASS":
            continue
        if category_filter and row.get("category") != category_filter:
            continue
        if trade_type_filter and row.get("tradeType") != trade_type_filter:
            continue
        if catalyst_conf_filter and row.get("catalystConfidence") != catalyst_conf_filter:
            continue
        if max_oracle_risk:
            allowed = {"Low": 0, "Medium": 1, "High": 2}
            if allowed.get(row.get("oracleRisk"), 2) > allowed.get(max_oracle_risk, 2):
                continue
        if gate_only and row.get("gateScore", 0) < 6:
            continue

        if mode == "strict":
            if row.get("gateScore", 0) < 6:
                continue
            if row.get("oracleRisk") != "Low":
                continue
        elif mode == "scout":
            if row.get("gateScore", 0) < 4:
                continue

        rows.append(row)

    rows.sort(
        key=lambda x: (
            0 if x.get("category") != "Sports" and mode == "scout" else 1,
            decision_priority(x.get("autoDraft", {}).get("finalDecision")),
            flag_priority(x.get("flag")),
            -to_float(x.get("gateScore")),
            oracle_priority(x.get("oracleRisk")),
            -to_float(x.get("candidateScore")),
            -to_float(x.get("liquidity")),
            -to_float(x.get("volume24hr")),
        )
    )

    diversified_rows = apply_diversity(
        rows,
        mode=mode,
        diversify=diversify,
        max_per_category=max_per_category,
        max_per_cluster=max_per_cluster,
    )

    diversified_rows = diversified_rows[:limit]
    alt_non_sports = top_non_sports(rows, limit=3)

    return jsonify({
        "count": len(diversified_rows),
        "markets": diversified_rows,
        "topNonSports": alt_non_sports,
        "filters": {
            "min_liquidity": min_liquidity,
            "min_volume": min_volume,
            "hide_pass": hide_pass,
            "category": category_filter,
            "trade_type": trade_type_filter,
            "max_oracle_risk": max_oracle_risk,
            "gate_only": gate_only,
            "catalyst_confidence": catalyst_conf_filter,
            "mode": mode,
            "diversify": diversify,
            "max_per_category": max_per_category,
            "max_per_cluster": max_per_cluster,
        }
    })


@app.route("/markets/top")
def markets_top():
    return markets()


@app.route("/dashboard")
def dashboard():
    return """
<!doctype html>
<html lang="sk">
<head>
  <meta charset="utf-8">
  <title>Polymarket Kandidátny Dashboard v5.4</title>
  <style>
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 16px;
      background: #f5f5f5;
      color: #222;
    }
    h1, h2, h3 {
      margin-bottom: 0.3rem;
    }
    .section {
      background: #ffffff;
      padding: 14px;
      border-radius: 8px;
      margin-bottom: 16px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 2.2fr) minmax(390px, 1fr);
      gap: 14px;
      align-items: start;
    }
    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 10px;
      align-items: end;
    }
    .control {
      display: flex;
      flex-direction: column;
      gap: 4px;
      min-width: 135px;
    }
    .help-inline {
      margin: 6px 0 10px 0;
      font-size: 12px;
      color: #555;
      background: #fafafa;
      border: 1px solid #eee;
      padding: 8px 10px;
      border-radius: 6px;
    }
    label {
      font-size: 12px;
      color: #555;
      font-weight: 600;
    }
    input, select {
      padding: 7px 9px;
      border: 1px solid #ddd;
      border-radius: 6px;
      font: inherit;
      background: #fff;
    }
    .checkbox-wrap {
      display: flex;
      align-items: center;
      gap: 7px;
      padding-top: 20px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12.5px;
      table-layout: fixed;
    }
    th, td {
      padding: 5px 6px;
      border-bottom: 1px solid #eee;
      text-align: left;
      vertical-align: top;
    }
    th {
      background: #fafafa;
      font-weight: 700;
      position: sticky;
      top: 0;
      z-index: 1;
    }
    tr.clickable {
      cursor: pointer;
    }
    tr.clickable:hover {
      background: #fafcff;
    }
    .small {
      font-size: 12px;
      color: #555;
    }
    .error {
      color: #c5221f;
      margin-bottom: 8px;
      font-size: 13px;
    }
    .badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
    }
    .watch {
      background: #e6f4ea;
      color: #137333;
    }
    .review {
      background: #fff4e5;
      color: #b06000;
    }
    .pass {
      background: #fce8e6;
      color: #c5221f;
    }
    .decision-buy-yes {
      background: #e8f5e9;
      color: #137333;
    }
    .decision-buy-no {
      background: #e8f0fe;
      color: #1558d6;
    }
    .decision-pass {
      background: #f3f4f6;
      color: #555;
    }
    .cat {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      background: #eef2ff;
      color: #3949ab;
      font-weight: 600;
      white-space: nowrap;
    }
    .risk-low {
      color: #137333;
      font-weight: 700;
    }
    .risk-medium {
      color: #b06000;
      font-weight: 700;
    }
    .risk-high {
      color: #c5221f;
      font-weight: 700;
    }
    .panel {
      position: sticky;
      top: 16px;
    }
    .panel-box {
      background: #fff;
      border-radius: 8px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.08);
      padding: 14px;
    }
    .panel-muted {
      color: #444;
      font-size: 13px;
      line-height: 1.5;
      background: #fafafa;
      border: 1px solid #eee;
      border-radius: 6px;
      padding: 10px;
    }
    .table-wrap {
      overflow: auto;
      max-height: 62vh;
      border-radius: 8px;
    }
    .count {
      margin-bottom: 10px;
      font-size: 13px;
      color: #444;
    }
    button {
      padding: 8px 12px;
      border: 1px solid #ddd;
      border-radius: 6px;
      background: #fff;
      cursor: pointer;
    }
    .btn-primary {
      background: #1558d6;
      border-color: #1558d6;
      color: white;
    }
    .check {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      padding: 8px 0;
      border-bottom: 1px solid #f0f0f0;
      font-size: 13px;
    }
    .check:last-child {
      border-bottom: none;
    }
    .ok {
      color: #137333;
      font-weight: 700;
      min-width: 28px;
    }
    .no {
      color: #c5221f;
      font-weight: 700;
      min-width: 28px;
    }
    .metric-pill {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      margin-right: 6px;
      margin-bottom: 6px;
      background: #f3f4f6;
      color: #333;
    }
    .action-row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 12px;
    }
    .title-row {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 8px;
    }
    .title-main {
      min-width: 0;
      flex: 1;
    }
    .title-main h3 {
      margin: 0 0 8px 0;
      line-height: 1.25;
    }
    .trade-link {
      white-space: nowrap;
      font-size: 13px;
      text-decoration: none;
      color: #1558d6;
      font-weight: 600;
      margin-top: 2px;
    }
    .trade-link:hover {
      text-decoration: underline;
    }
    .journal-box, .legend-box, .non-sports-box {
      margin-top: 16px;
      padding-top: 12px;
      border-top: 1px solid #eee;
    }
    .saved-note {
      margin-top: 8px;
      font-size: 12px;
      color: #137333;
    }
    .draft-grid {
      display: grid;
      grid-template-columns: 110px 1fr;
      gap: 8px;
      font-size: 13px;
      margin-bottom: 10px;
    }
    .draft-grid div:first-child {
      color: #666;
      font-weight: 600;
    }
    .block-label {
      margin-top: 10px;
      display: block;
      font-size: 12px;
      color: #555;
      font-weight: 600;
    }
    .legend-item {
      padding: 8px 0;
      border-bottom: 1px solid #f2f2f2;
      font-size: 13px;
      line-height: 1.5;
    }
    .legend-item:last-child {
      border-bottom: none;
    }
    .question-cell {
      max-width: 320px;
    }
    .question-truncate {
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      line-height: 1.35;
      max-height: 2.7em;
      word-break: break-word;
    }
    .mini-list {
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }
    .mini-card {
      border: 1px solid #eee;
      border-radius: 6px;
      padding: 8px;
      background: #fafafa;
      font-size: 12px;
      line-height: 1.4;
    }
    .mini-card strong {
      display: block;
      margin-bottom: 4px;
    }

    .w-flag { width: 70px; }
    .w-decision { width: 92px; }
    .w-gate { width: 50px; }
    .w-score { width: 46px; }
    .w-friction { width: 72px; }
    .w-exit { width: 70px; }
    .w-type { width: 72px; }
    .w-cat { width: 58px; }
    .w-oracle { width: 56px; }
    .w-catc { width: 60px; }
    .w-price { width: 50px; }
    .w-vol { width: 74px; }
    .w-liq { width: 82px; }
    .w-days { width: 42px; }

    @media (max-width: 1200px) {
      .layout {
        grid-template-columns: 1fr;
      }
      .panel {
        position: static;
      }
    }
  </style>
</head>
<body>
  <h1>Polymarket kandidátny dashboard</h1>
  <p class="small">v5.4: diversity filter, cluster dedupe, top mimo športu, Strict/Scout helper a menej športového spamu.</p>

  <div class="layout">
    <div class="section">
      <h2>Top kandidáti</h2>

      <div class="controls">
        <div class="control">
          <label for="mode">Režim</label>
          <select id="mode">
            <option value="strict" selected>Strict v6</option>
            <option value="scout">Scout</option>
          </select>
        </div>

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
          <label for="tradeType">Typ trhu</label>
          <select id="tradeType">
            <option value="">Všetko</option>
            <option value="Momentum">Momentum</option>
            <option value="Time Decay">Časový rozpad</option>
            <option value="Resolution">Resolution / spor</option>
            <option value="Centovka">Centovka</option>
            <option value="Other">Ostatné</option>
          </select>
        </div>

        <div class="control">
          <label for="catalystConfidence">Sila katalyzátora</label>
          <select id="catalystConfidence">
            <option value="">Všetko</option>
            <option value="High">Silný</option>
            <option value="Medium">Stredný</option>
            <option value="Low">Slabý</option>
          </select>
        </div>

        <div class="control">
          <label for="maxOracleRisk">Max oracle riziko</label>
          <select id="maxOracleRisk">
            <option value="">Všetko</option>
            <option value="Low" selected>Nízke</option>
            <option value="Medium">Stredné</option>
            <option value="High">Vysoké</option>
          </select>
        </div>

        <div class="control">
          <label for="minLiquidity">Min likvidita</label>
          <select id="minLiquidity">
            <option value="0">0</option>
            <option value="50000">50 000</option>
            <option value="100000" selected>100 000</option>
            <option value="150000">150 000</option>
            <option value="250000">250 000</option>
          </select>
        </div>

        <div class="control">
          <label for="minVolume">Min 24h objem</label>
          <select id="minVolume">
            <option value="0">0</option>
            <option value="25000" selected>25 000</option>
            <option value="50000">50 000</option>
            <option value="100000">100 000</option>
            <option value="250000">250 000</option>
          </select>
        </div>

        <div class="control">
          <label for="maxPerCategory">Max / kategória</label>
          <select id="maxPerCategory">
            <option value="">Auto</option>
            <option value="2">2</option>
            <option value="3">3</option>
            <option value="4">4</option>
            <option value="5">5</option>
          </select>
        </div>

        <div class="control">
          <label for="maxPerCluster">Max / cluster</label>
          <select id="maxPerCluster">
            <option value="">Auto</option>
            <option value="1">1</option>
            <option value="2" selected>2</option>
            <option value="3">3</option>
          </select>
        </div>

        <div class="checkbox-wrap">
          <input type="checkbox" id="hidePass" checked />
          <label for="hidePass">Skryť PASS</label>
        </div>

        <div class="checkbox-wrap">
          <input type="checkbox" id="gateOnly" />
          <label for="gateOnly">Len 6/6 gate</label>
        </div>

        <div class="checkbox-wrap">
          <input type="checkbox" id="diversify" checked />
          <label for="diversify">Diverzifikovať feed</label>
        </div>

        <div class="control">
          <button onclick="loadMarkets()">Obnoviť</button>
        </div>
      </div>

      <div class="help-inline" id="modeHelp">
        Strict v6 = užší shortlist na reálny trade. Scout = širší research feed na hľadanie kandidátov pred finálnym preosiatím.
      </div>

      <div class="count" id="countBox"></div>
      <div id="markets-error" class="error" style="display:none;"></div>

      <div class="table-wrap">
        <table id="markets-table">
          <thead>
            <tr>
              <th class="w-flag">Flag</th>
              <th class="w-decision">Rozhod.</th>
              <th class="w-gate">Gate</th>
              <th class="w-score">Skóre</th>
              <th class="w-friction">Frikcia</th>
              <th class="w-exit">Exit</th>
              <th class="w-type">Typ</th>
              <th class="w-cat">Kat.</th>
              <th class="w-oracle">Oracle</th>
              <th class="w-catc">Kat.</th>
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
        <p class="panel-muted">Klikni na riadok v tabuľke a zobrazí sa checklist, systémový draft, cluster a legenda v6.</p>
      </div>
    </div>
  </div>

  <script>
    let cachedMarkets = [];
    let selectedMarket = null;
    let cachedNonSports = [];

    function fmtInt(value) {
      const n = Number(value);
      if (!Number.isFinite(n)) return '';
      return Math.round(n).toLocaleString('sk-SK');
    }

    function fmtPrice(value) {
      const n = Number(value);
      if (!Number.isFinite(n)) return '';
      return n.toFixed(3);
    }

    function fmtDays(value) {
      const n = Number(value);
      if (!Number.isFinite(n)) return '';
      return Math.round(n).toString();
    }

    function flagBadge(flag) {
      if (flag === 'WATCH') return '<span class="badge watch">WATCH</span>';
      if (flag === 'REVIEW') return '<span class="badge review">REVIEW</span>';
      return '<span class="badge pass">PASS</span>';
    }

    function decisionBadge(value) {
      if (value === 'BUY YES') return '<span class="badge decision-buy-yes">BUY YES</span>';
      if (value === 'BUY NO') return '<span class="badge decision-buy-no">BUY NO</span>';
      return '<span class="badge decision-pass">PASS</span>';
    }

    function catBadge(cat) {
      return '<span class="cat">' + (cat || 'Ostatné') + '</span>';
    }

    function oracleBadge(level) {
      if (level === 'Nízke') return '<span class="risk-low">Nízke</span>';
      if (level === 'Stredné') return '<span class="risk-medium">Stredné</span>';
      return '<span class="risk-high">Vysoké</span>';
    }

    function pill(text) {
      return '<span class="metric-pill">' + text + '</span>';
    }

    function renderChecklist(checklist) {
      const order = [
        ['resolutability', 'Resolutability'],
        ['baseRate', 'Base Rate'],
        ['friction', 'Frikcia'],
        ['exit', 'Exit'],
        ['catalyst', 'Catalyst'],
        ['oracle', 'Oracle Trap']
      ];

      return order.map(([key, label]) => {
        const item = checklist[key];
        return `
          <div class="check">
            <div class="${item.ok ? 'ok' : 'no'}">${item.ok ? 'ÁNO' : 'NIE'}</div>
            <div><strong>${label}</strong><br>${item.note || ''}</div>
          </div>
        `;
      }).join('');
    }

    function buildTradeLogText(m) {
      return `KATEGORIZÁCIA TRHU: ${m.tradeTypeLabel || ''}

PRE-TRADE CHECKLIST:
1. Resolutability: ${(m.checklist?.resolutability?.ok ? 'ÁNO' : 'NIE')} - ${m.checklist?.resolutability?.note || ''}
2. Base Rate: ${(m.checklist?.baseRate?.ok ? 'ÁNO' : 'NIE')} - ${m.checklist?.baseRate?.note || ''}
3. Frikcia: ${(m.checklist?.friction?.ok ? 'ÁNO' : 'NIE')} - ${m.checklist?.friction?.note || ''}
4. Exit: ${(m.checklist?.exit?.ok ? 'ÁNO' : 'NIE')} - ${m.checklist?.exit?.note || ''}
5. Catalyst: ${(m.checklist?.catalyst?.ok ? 'ÁNO' : 'NIE')} - ${m.checklist?.catalyst?.note || ''}
6. Oracle Trap: ${(m.checklist?.oracle?.ok ? 'ÁNO' : 'NIE')} - ${m.checklist?.oracle?.note || ''}

ANALÝZA EDGE-U:
${m.autoDraft?.thesis || ''}

KDE JE MISPRICING:
${m.autoDraft?.mispricing || ''}

TYP EDGE:
${m.autoDraft?.edge || ''}

RESOLUTION ANALÝZA:
${m.autoDraft?.resolution || ''}

PARAMETRE VSTUPU:
Market: ${m.question || ''}
YES cena: ${fmtPrice(m.yesPrice)}
NO cena: ${fmtPrice(m.noPrice)}
Likvidita: ${fmtInt(m.liquidity)}
24h objem: ${fmtInt(m.volume24hr)}
Dni do expirácie: ${fmtDays(m.daysToEnd)}
Sizing hint: ${m.autoDraft?.sizingHint || ''}

PLÁN VÝSTUPU:
Fáza 1: podľa typu setupu a liquidity conditions
Fáza 2: partial de-risk pri repricingu
Runner: len ak edge ostáva platný
Time-stop limit: 24–72h po očakávanom katalyzátore bez pohybu

INVALIDÁCIA (FULL EXIT TRIGGER):
${m.autoDraft?.invalidation || ''}

KATALYZÁTOR:
${m.autoDraft?.catalyst || ''}

CONFIDENCE:
${m.autoDraft?.confidence || ''}/10

BIAS:
${m.autoDraft?.bias || ''}

FINÁLNE ROZHODNUTIE:
${m.autoDraft?.finalDecision || 'PASS'}
`;
    }

    async function copyTradeLog() {
      if (!selectedMarket) return;
      const text = buildTradeLogText(selectedMarket);
      await navigator.clipboard.writeText(text);
      const msg = document.getElementById('savedNote');
      if (msg) msg.textContent = 'Trade-log šablóna skopírovaná do schránky.';
    }

    function downloadTradeLog() {
      if (!selectedMarket) return;
      const text = buildTradeLogText(selectedMarket);
      const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      const safeSlug = (selectedMarket.slug || 'trade-log').replace(/[^a-z0-9-]/gi, '-');
      a.download = safeSlug + '-trade-log.txt';
      a.click();
      URL.revokeObjectURL(a.href);
      const msg = document.getElementById('savedNote');
      if (msg) msg.textContent = 'Trade-log šablóna stiahnutá.';
    }

    function renderLegend() {
      return `
        <div class="legend-box">
          <h3>Legenda v6</h3>

          <div class="legend-item">
            <strong>Strict v6</strong><br>
            Užší shortlist na reálny trade. Cieľ je vidieť len najčistejšie kandidáty blízko execution-ready stavu.
          </div>

          <div class="legend-item">
            <strong>Scout</strong><br>
            Širší research feed. Slúži na objavovanie kandidátov, nie na automatický vstup bez ďalšieho review.
          </div>

          <div class="legend-item">
            <strong>Gate 6/6</strong><br>
            Súčet 6 povinných podmienok pred tradeom. Ak nie je 6/6, v strict režime trade spravidla neexistuje.
          </div>

          <div class="legend-item">
            <strong>Mini checklist</strong><br>
            Detailný rozpis gate bodov: Resolutability, Base Rate, Frikcia, Exit, Catalyst, Oracle Trap.
          </div>

          <div class="legend-item">
            <strong>Frikcia</strong><br>
            Náklady a praktické prekážky tradu: spread, fees, sklz, likvidita a kvalita exitu. Ak zje edge, výsledok má byť PASS.
          </div>

          <div class="legend-item">
            <strong>Typ trhu</strong><br>
            Momentum = news repricing, Časový rozpad = edge z času, Resolution = pravidlá a oracle, Centovka = asymetria v pásme 0.01–0.05.
          </div>

          <div class="legend-item">
            <strong>Oracle riziko</strong><br>
            Riziko, že market resolve-ne inak, než vyzerá podľa reality, kvôli presnému textu pravidiel, source alebo ambiguity.
          </div>

          <div class="legend-item">
            <strong>Cluster</strong><br>
            Skupina marketov patriacich k rovnakému eventu alebo naratívu. Slúži na zníženie duplicitného feedu a lepšiu diverzifikáciu.
          </div>
        </div>
      `;
    }

    function renderNonSports() {
      if (!cachedNonSports || cachedNonSports.length === 0) {
        return `
          <div class="non-sports-box">
            <h3>Top mimo športu</h3>
            <div class="panel-muted">Aktuálne nebol nájdený žiadny zaujímavý non-sports kandidát podľa tvojich filtrov.</div>
          </div>
        `;
      }

      const cards = cachedNonSports.map(m => `
        <div class="mini-card">
          <strong>${m.question || ''}</strong>
          ${decisionBadge(m.autoDraft?.finalDecision || 'PASS')}
          ${catBadge(m.categoryLabel || 'Ostatné')}
          ${pill('Gate: ' + (m.gateScore ?? '') + '/6')}
          <div style="margin-top:6px;">Cluster: ${m.cluster || ''}</div>
        </div>
      `).join('');

      return `
        <div class="non-sports-box">
          <h3>Top mimo športu</h3>
          <div class="mini-list">${cards}</div>
        </div>
      `;
    }

    function showDetail(m) {
      selectedMarket = m;
      const panel = document.getElementById('detailPanel');
      const link = m.slug ? 'https://polymarket.com/market/' + m.slug : '';

      panel.innerHTML = `
        <div class="title-row">
          <div class="title-main">
            <h3>${m.question || 'Detail marketu'}</h3>
            <div>
              ${flagBadge(m.flag)}
              ${decisionBadge(m.autoDraft?.finalDecision || 'PASS')}
              ${catBadge(m.categoryLabel)}
              ${pill('Typ: ' + (m.tradeTypeLabel || 'Ostatné'))}
              ${pill('Gate: ' + (m.gateScore ?? '') + '/6')}
            </div>
          </div>
          <div>
            ${link ? '<a class="trade-link" href="' + link + '" target="_blank" rel="noopener noreferrer">Otvoriť trade</a>' : ''}
          </div>
        </div>

        <div class="draft-grid"><div>Cluster</div><div>${m.cluster || ''}</div></div>
        <div class="draft-grid"><div>Fail point</div><div>${m.failPoint || ''}</div></div>
        <div class="draft-grid"><div>Sizing cap</div><div>${m.sizingCap || ''}</div></div>

        <h3 style="margin-top:14px;">Mini 6/6 checklist</h3>
        ${renderChecklist(m.checklist || {})}

        <div class="journal-box">
          <h3>Systémový draft podľa v6</h3>

          <div class="draft-grid"><div>Bias</div><div>${m.autoDraft?.bias || ''}</div></div>
          <div class="draft-grid"><div>Rozhodnutie</div><div><strong>${m.autoDraft?.finalDecision || ''}</strong></div></div>
          <div class="draft-grid"><div>Confidence</div><div>${m.autoDraft?.confidence || ''}/10</div></div>
          <div class="draft-grid"><div>Sizing hint</div><div>${m.autoDraft?.sizingHint || ''}</div></div>

          <label class="block-label">Navrhovaná téza</label>
          <div class="panel-muted">${m.autoDraft?.thesis || ''}</div>

          <label class="block-label">Kde je mispricing</label>
          <div class="panel-muted">${m.autoDraft?.mispricing || ''}</div>

          <label class="block-label">Typ edge</label>
          <div class="panel-muted">${m.autoDraft?.edge || ''}</div>

          <label class="block-label">Katalyzátor</label>
          <div class="panel-muted">${m.autoDraft?.catalyst || ''}</div>

          <label class="block-label">Resolution analýza</label>
          <div class="panel-muted">${m.autoDraft?.resolution || ''}</div>

          <label class="block-label">Invalidácia</label>
          <div class="panel-muted">${m.autoDraft?.invalidation || ''}</div>

          <div class="action-row">
            <button class="btn-primary" onclick="copyTradeLog()">Kopírovať trade-log šablónu</button>
            <button onclick="downloadTradeLog()">Stiahnuť trade-log</button>
          </div>

          <div class="saved-note" id="savedNote"></div>
        </div>

        ${renderNonSports()}
        ${renderLegend()}
      `;
    }

    function updateModeHelp() {
      const mode = document.getElementById('mode').value;
      const help = document.getElementById('modeHelp');
      if (mode === 'strict') {
        help.textContent = 'Strict v6 = užší shortlist na reálny trade. Prirodzene pustí menej marketov, ale bližšie k execution-ready kvalite.';
      } else {
        help.textContent = 'Scout = širší research feed. Umožní objaviť viac kandidátov, ale neznamená to automatický BUY bez ďalšieho v6 review.';
      }
    }

    async function loadMarkets() {
      const errorEl = document.getElementById('markets-error');
      const tbody = document.querySelector('#markets-table tbody');
      const countBox = document.getElementById('countBox');

      const mode = document.getElementById('mode').value;
      const category = document.getElementById('category').value;
      const tradeType = document.getElementById('tradeType').value;
      const catalystConfidence = document.getElementById('catalystConfidence').value;
      const maxOracleRisk = document.getElementById('maxOracleRisk').value;
      const minLiquidity = document.getElementById('minLiquidity').value;
      const minVolume = document.getElementById('minVolume').value;
      const hidePass = document.getElementById('hidePass').checked;
      const gateOnly = document.getElementById('gateOnly').checked;
      const diversify = document.getElementById('diversify').checked;
      const maxPerCategory = document.getElementById('maxPerCategory').value;
      const maxPerCluster = document.getElementById('maxPerCluster').value;

      updateModeHelp();

      try {
        const params = new URLSearchParams({
          limit: '100',
          mode: mode,
          min_liquidity: minLiquidity,
          min_volume: minVolume,
          hide_pass: hidePass ? 'true' : 'false',
          category: category,
          trade_type: tradeType,
          max_oracle_risk: maxOracleRisk,
          gate_only: gateOnly ? 'true' : 'false',
          catalyst_confidence: catalystConfidence,
          diversify: diversify ? 'true' : 'false',
          max_per_category: maxPerCategory,
          max_per_cluster: maxPerCluster
        });

        const res = await fetch('/markets?' + params.toString());
        if (!res.ok) throw new Error('HTTP ' + res.status);

        const data = await res.json();
        const markets = data.markets || [];
        cachedNonSports = data.topNonSports || [];

        cachedMarkets = markets;
        tbody.innerHTML = '';

        markets.forEach((m, idx) => {
          const tr = document.createElement('tr');
          tr.className = 'clickable';

          tr.innerHTML = `
            <td>${flagBadge(m.flag)}</td>
            <td>${decisionBadge(m.autoDraft?.finalDecision || 'PASS')}</td>
            <td>${m.gateScore ?? ''}/6</td>
            <td>${m.candidateScore ?? ''}</td>
            <td>${m.frictionLabelSk || ''}</td>
            <td>${m.exitLabelSk || ''}</td>
            <td>${m.tradeTypeLabel || ''}</td>
            <td>${catBadge(m.categoryLabel)}</td>
            <td>${oracleBadge(m.oracleRiskLabel)}</td>
            <td>${m.catalystConfidenceLabel || ''}</td>
            <td class="question-cell"><div class="question-truncate">${m.question || ''}</div></td>
            <td>${fmtPrice(m.yesPrice)}</td>
            <td>${fmtPrice(m.noPrice)}</td>
            <td>${fmtInt(m.volume24hr)}</td>
            <td>${fmtInt(m.liquidity)}</td>
            <td>${fmtDays(m.daysToEnd)}</td>
          `;

          tr.addEventListener('click', () => {
            showDetail(cachedMarkets[idx]);
          });

          tbody.appendChild(tr);
        });

        countBox.textContent = 'Zobrazené markety: ' + markets.length;
        errorEl.style.display = 'none';

        if (markets.length > 0) {
          showDetail(markets[0]);
        } else {
          document.getElementById('detailPanel').innerHTML =
            '<h3>Detail marketu</h3><p class="panel-muted">Žiadny market nevyhovuje aktuálnym filtrom.</p>' +
            renderNonSports() + renderLegend();
        }
      } catch (err) {
        errorEl.textContent = 'Chyba pri načítaní marketov: ' + err.message;
        errorEl.style.display = 'block';
      }
    }

    document.getElementById('mode').addEventListener('change', loadMarkets);
    document.getElementById('category').addEventListener('change', loadMarkets);
    document.getElementById('tradeType').addEventListener('change', loadMarkets);
    document.getElementById('catalystConfidence').addEventListener('change', loadMarkets);
    document.getElementById('maxOracleRisk').addEventListener('change', loadMarkets);
    document.getElementById('minLiquidity').addEventListener('change', loadMarkets);
    document.getElementById('minVolume').addEventListener('change', loadMarkets);
    document.getElementById('hidePass').addEventListener('change', loadMarkets);
    document.getElementById('gateOnly').addEventListener('change', loadMarkets);
    document.getElementById('diversify').addEventListener('change', loadMarkets);
    document.getElementById('maxPerCategory').addEventListener('change', loadMarkets);
    document.getElementById('maxPerCluster').addEventListener('change', loadMarkets);

    loadMarkets();
  </script>
</body>
</html>
    """


@app.route("/analyze-market")
def analyze_market():
    slug = request.args.get("slug")
    if not slug:
        return jsonify({"error": "Missing slug parameter"}), 400

    url = f"{GAMMA_BASE}/markets"
    params = {
        "slug": slug,
        "limit": 1,
        "active": "true",
        "closed": "false",
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
    except Exception as e:
        return jsonify({"error": f"Gamma request failed: {e}"}), 502

    data = r.json()
    if not data:
        return jsonify({"error": "No market found for given slug", "slug": slug}), 404

    m = data[0]
    row = build_market_row(m)

    return jsonify(row)