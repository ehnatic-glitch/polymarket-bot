ffrom flask import Flask, jsonify
import requests

app = Flask(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

@app.route("/")
def home():
    return jsonify({
        "message": "Polymarket read-only app is running",
        "endpoints": ["/health", "/markets"]
    })

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/markets")
def markets():
    url = f"{GAMMA_BASE}/markets"
    params = {
        "limit": 10,
        "active": "true",
        "closed": "false"
    }

    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    simplified = []
    for m in data[:10]:
        simplified.append({
            "question": m.get("question"),
            "active": m.get("active"),
            "closed": m.get("closed"),
            "volume": m.get("volume"),
            "liquidity": m.get("liquidity"),
            "endDate": m.get("endDate"),
            "outcomes": m.get("outcomes"),
            "outcomePrices": m.get("outcomePrices")
        })

    return jsonify(simplified)