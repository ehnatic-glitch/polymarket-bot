from flask import Flask, jsonify, request
import requests

app = Flask(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"


def to_bool(value, default=None):
    if value is None:
        return default
    return str(value).lower() in ("true", "1", "yes")


@app.route("/")
def home():
    return jsonify({
        "message": "Polymarket read-only app is running",
        "endpoints": [
            "/health",
            "/markets",
            "/markets/top",
            "/events"
        ]
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/markets")
def markets():
    # Čítame query parametre z URL, napr. /markets?limit=20&order=volume24hr
    limit = request.args.get("limit", "10")
    order = request.args.get("order", "volume24hr")
    active = request.args.get("active", "true")
    closed = request.args.get("closed", "false")

    params = {
        "limit": limit,
        "order": order,
        "active": active,
        "closed": closed,
    }

    url = f"{GAMMA_BASE}/markets"
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    simplified = []
    for m in data:
        simplified.append({
            "question": m.get("question"),
            "slug": m.get("slug"),
            "active": m.get("active"),
            "closed": m.get("closed"),
            "volume": m.get("volume"),
            "volume24hr": m.get("volume24hr"),
            "liquidity": m.get("liquidity"),
            "endDate": m.get("endDate"),
            "outcomes": m.get("outcomes"),
            "outcomePrices": m.get("outcomePrices"),
        })

    return jsonify({
        "count": len(simplified),
        "params": params,
        "markets": simplified,
    })

@app.route("/markets/top")
def markets_top():
    params = {
        "limit": request.args.get("limit", 20),
        "offset": request.args.get("offset", 0),
        "order": request.args.get("order", "volume_24hr"),
        "ascending": request.args.get("ascending", "false"),
        "active": request.args.get("active", "true"),
        "closed": request.args.get("closed", "false"),
    }

    url = f"{GAMMA_BASE}/markets"
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    simplified = []
    for m in data:
        simplified.append({
            "question": m.get("question"),
            "slug": m.get("slug"),
            "volume": m.get("volume"),
            "volume24hr": m.get("volume24hr"),
            "liquidity": m.get("liquidity"),
            "endDate": m.get("endDate"),
            "outcomePrices": m.get("outcomePrices")
        })

    return jsonify(simplified)

@app.route("/events")
def events():
    # Základné parametre s rozumnými defaultmi
    limit = request.args.get("limit", "10")
    order = request.args.get("order", "volume24hr")
    active = request.args.get("active", "true")
    closed = request.args.get("closed", "false")

    params = {
        "limit": limit,
        "order": order,
        "active": active,
        "closed": closed,
    }

    url = f"{GAMMA_BASE}/events"
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    simplified = []
    for e in data:
        markets = e.get("markets") or []
        simplified.append({
            "title": e.get("title"),
            "slug": e.get("slug"),
            "active": e.get("active"),
            "closed": e.get("closed"),
            "volume": e.get("volume"),
            "volume24hr": e.get("volume24hr"),
            "liquidity": e.get("liquidity"),
            "startDate": e.get("startDate"),
            "endDate": e.get("endDate"),
            "marketsCount": len(markets),
        })

    return jsonify({
        "count": len(simplified),
        "params": params,
        "events": simplified,
    })


@app.route("/events")
def events():
    params = {
        "limit": request.args.get("limit", 10),
        "offset": request.args.get("offset", 0),
        "order": request.args.get("order", "volume_24hr"),
        "ascending": request.args.get("ascending", "false"),
        "active": request.args.get("active", "true"),
        "closed": request.args.get("closed", "false"),
    }

    slug = request.args.get("slug")
    if slug:
        params["slug"] = slug

    tag_id = request.args.get("tag_id")
    if tag_id:
        params["tag_id"] = tag_id

    related_tags = to_bool(request.args.get("related_tags"), None)
    if related_tags is not None:
        params["related_tags"] = str(related_tags).lower()

    url = f"{GAMMA_BASE}/events"
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    simplified = []
    for e in data:
        markets = e.get("markets", []) or []
        simplified.append({
            "id": e.get("id"),
            "title": e.get("title"),
            "slug": e.get("slug"),
            "active": e.get("active"),
            "closed": e.get("closed"),
            "volume": e.get("volume"),
            "volume24hr": e.get("volume24hr"),
            "liquidity": e.get("liquidity"),
            "startDate": e.get("startDate"),
            "endDate": e.get("endDate"),
            "marketsCount": len(markets),
            "markets": [
                {
                    "id": m.get("id"),
                    "question": m.get("question"),
                    "slug": m.get("slug"),
                    "outcomePrices": m.get("outcomePrices"),
                    "volume": m.get("volume"),
                    "liquidity": m.get("liquidity"),
                }
                for m in markets[:5]
            ]
        })

    return jsonify({
        "count": len(simplified),
        "params": params,
        "events": simplified
    })