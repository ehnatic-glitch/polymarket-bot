from flask import Flask, jsonify, request
import requests
import json

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
            "/events",
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


@app.route("/markets")
def markets():
    limit = int(request.args.get("limit", "10"))
    active = request.args.get("active", "true")
    closed = request.args.get("closed", "false")

    params = {
        "limit": 200,
        "active": active,
        "closed": closed,
    }

    url = f"{GAMMA_BASE}/markets"
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    filtered = []
    for m in data:
        volume24hr = to_float(m.get("volume24hr"))
        liquidity = to_float(m.get("liquidity"))
        outcome_prices = parse_json_list(m.get("outcomePrices"))
        outcomes = parse_json_list(m.get("outcomes"))

        if m.get("active") is not True:
            continue
        if m.get("closed") is True:
            continue
        if volume24hr <= 0:
            continue
        if liquidity <= 0:
            continue

        filtered.append({
            "question": m.get("question"),
            "slug": m.get("slug"),
            "active": m.get("active"),
            "closed": m.get("closed"),
            "volume": m.get("volume"),
            "volume24hr": m.get("volume24hr"),
            "liquidity": m.get("liquidity"),
            "endDate": m.get("endDate"),
            "outcomes": outcomes,
            "outcomePrices": outcome_prices,
            "bestBid": m.get("bestBid"),
            "bestAsk": m.get("bestAsk"),
            "lastTradePrice": m.get("lastTradePrice"),
        })

    filtered.sort(key=lambda x: to_float(x.get("volume24hr")), reverse=True)
    filtered = filtered[:limit]

    return jsonify({
        "count": len(filtered),
        "params": params,
        "markets": filtered,
    })


@app.route("/markets/top")
def markets_top():
    limit = int(request.args.get("limit", "20"))

    params = {
        "limit": 200,
        "active": "true",
        "closed": "false",
    }

    url = f"{GAMMA_BASE}/markets"
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    filtered = []
    for m in data:
        volume24hr = to_float(m.get("volume24hr"))
        liquidity = to_float(m.get("liquidity"))
        outcome_prices = parse_json_list(m.get("outcomePrices"))

        if m.get("active") is not True:
            continue
        if m.get("closed") is True:
            continue
        if volume24hr <= 0:
            continue
        if liquidity <= 0:
            continue

        filtered.append({
            "question": m.get("question"),
            "slug": m.get("slug"),
            "volume": m.get("volume"),
            "volume24hr": m.get("volume24hr"),
            "liquidity": m.get("liquidity"),
            "endDate": m.get("endDate"),
            "outcomePrices": outcome_prices,
            "bestBid": m.get("bestBid"),
            "bestAsk": m.get("bestAsk"),
            "lastTradePrice": m.get("lastTradePrice"),
        })

    filtered.sort(key=lambda x: to_float(x.get("volume24hr")), reverse=True)
    return jsonify(filtered[:limit])


@app.route("/dashboard")
def dashboard():
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Polymarket Dashboard</title>
  <style>
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 20px;
      background: #f5f5f5;
      color: #222;
    }
    h1, h2 {
      margin-bottom: 0.3rem;
    }
    .section {
      background: #ffffff;
      padding: 16px;
      border-radius: 8px;
      margin-bottom: 20px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    th, td {
      padding: 6px 8px;
      border-bottom: 1px solid #eee;
      text-align: left;
      vertical-align: top;
    }
    th {
      background: #fafafa;
      font-weight: 600;
    }
    .badge {
      display: inline-block;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 600;
    }
    .badge-active {
      background: #e6f4ea;
      color: #137333;
    }
    .badge-closed {
      background: #fce8e6;
      color: #c5221f;
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
    a {
      color: #1558d6;
      text-decoration: none;
    }
    a:hover {
      text-decoration: underline;
    }
  </style>
</head>
<body>
  <h1>Polymarket Read‑Only Dashboard</h1>
  <p class="small">Zdroj: Render backend &gamma;-API (markets + events)</p>

  <div class="section">
    <h2>Top markets (volume24hr)</h2>
    <div id="markets-error" class="error" style="display:none;"></div>
    <table id="markets-table">
      <thead>
        <tr>
          <th>Question</th>
          <th>Yes price</th>
          <th>No price</th>
          <th>24h volume</th>
          <th>Liquidity</th>
          <th>End</th>
          <th>Link</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="section">
    <h2>Events (volume24hr)</h2>
    <div id="events-error" class="error" style="display:none;"></div>
    <table id="events-table">
      <thead>
        <tr>
          <th>Title</th>
          <th>Status</th>
          <th>24h volume</th>
          <th>Liquidity</th>
          <th>Markets</th>
          <th>End</th>
          <th>Link</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>

  <script>
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

    async function loadMarkets() {
      const errorEl = document.getElementById('markets-error');
      const tbody = document.querySelector('#markets-table tbody');
      try {
        const res = await fetch('/markets?limit=10');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        tbody.innerHTML = '';

        (data.markets || []).forEach(m => {
          const tr = document.createElement('tr');
          const prices = Array.isArray(m.outcomePrices) ? m.outcomePrices : [];

          let yes = null;
          let no = null;

          if (prices.length >= 2) {
            yes = Number(prices[0]);
            no = Number(prices[1]);
          } else if (prices.length === 1) {
            yes = Number(prices[0]);
            no = Number.isFinite(yes) ? (1 - yes) : null;
          } else {
            const bid = Number(m.bestBid);
            const ask = Number(m.bestAsk);
            if (Number.isFinite(bid)) yes = bid;
            if (Number.isFinite(ask)) no = 1 - ask;
          }

          const link = m.slug
            ? 'https://polymarket.com/market/' + m.slug
            : null;

          tr.innerHTML = `
            <td>${m.question || ''}</td>
            <td>${fmtPrice(yes)}</td>
            <td>${fmtPrice(no)}</td>
            <td>${fmtInt(m.volume24hr)}</td>
            <td>${fmtInt(m.liquidity)}</td>
            <td>${m.endDate ? new Date(m.endDate).toLocaleString('sk-SK') : ''}</td>
            <td>${link ? '<a href="' + link + '" target="_blank" rel="noopener noreferrer">Open</a>' : ''}</td>
          `;
          tbody.appendChild(tr);
        });

        errorEl.style.display = 'none';
      } catch (err) {
        errorEl.textContent = 'Chyba pri načítaní markets: ' + err.message;
        errorEl.style.display = 'block';
      }
    }

    async function loadEvents() {
      const errorEl = document.getElementById('events-error');
      const tbody = document.querySelector('#events-table tbody');
      try {
        const res = await fetch('/events?limit=10&order=volume24hr');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        tbody.innerHTML = '';

        (data.events || []).forEach(e => {
          const link = e.slug
            ? 'https://polymarket.com/event/' + e.slug
            : null;
          const status = e.closed ? 'closed' : (e.active ? 'active' : 'other');
          let statusHtml = '';
          if (status === 'active') {
            statusHtml = '<span class="badge badge-active">ACTIVE</span>';
          } else if (status === 'closed') {
            statusHtml = '<span class="badge badge-closed">CLOSED</span>';
          } else {
            statusHtml = '<span class="badge">OTHER</span>';
          }

          const marketsCount = (
            e.marketsCount !== undefined && e.marketsCount !== null
              ? e.marketsCount
              : (e.markets ? e.markets.length : '')
          );

          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td>${e.title || ''}</td>
            <td>${statusHtml}</td>
            <td>${fmtInt(e.volume24hr)}</td>
            <td>${fmtInt(e.liquidity)}</td>
            <td>${marketsCount ?? ''}</td>
            <td>${e.endDate ? new Date(e.endDate).toLocaleString('sk-SK') : ''}</td>
            <td>${link ? '<a href="' + link + '" target="_blank" rel="noopener noreferrer">Open</a>' : ''}</td>
          `;
          tbody.appendChild(tr);
        });

        errorEl.style.display = 'none';
      } catch (err) {
        errorEl.textContent = 'Chyba pri načítaní events: ' + err.message;
        errorEl.style.display = 'block';
      }
    }

    loadMarkets();
    loadEvents();
  </script>
</body>
</html>
    """


@app.route("/events")
def events():
    params = {
        "limit": request.args.get("limit", "10"),
        "active": request.args.get("active", "true"),
        "closed": request.args.get("closed", "false"),
    }

    url = f"{GAMMA_BASE}/events"

    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return jsonify({"error": str(e), "where": "gamma/events"}), 502

    out = []
    for ev in data:
        out.append({
            "title": ev.get("title"),
            "slug": ev.get("slug"),
            "active": ev.get("active"),
            "closed": ev.get("closed"),
            "endDate": ev.get("endDate"),
            "liquidity": ev.get("liquidity"),
            "volume": ev.get("volume"),
            "volume24hr": ev.get("volume24hr"),
            "marketsCount": ev.get("marketsCount"),
            "markets": ev.get("markets", []),
        })

    return jsonify({
        "count": len(out),
        "events": out,
        "params": params,
    })


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
    prices = parse_json_list(m.get("outcomePrices"))
    outcomes = parse_json_list(m.get("outcomes"))

    yes_price = None
    no_price = None

    if len(prices) >= 2:
        yes_price = to_float(prices[0], None)
        no_price = to_float(prices[1], None)
    elif len(prices) == 1:
        yes_price = to_float(prices[0], None)
        no_price = (1 - yes_price) if isinstance(yes_price, (int, float)) else None

    result = {
        "slug": m.get("slug"),
        "question": m.get("question"),
        "active": m.get("active"),
        "closed": m.get("closed"),
        "endDate": m.get("endDate"),
        "volume": m.get("volume"),
        "volume24hr": m.get("volume24hr"),
        "liquidity": m.get("liquidity"),
        "bestBid": m.get("bestBid"),
        "bestAsk": m.get("bestAsk"),
        "lastTradePrice": m.get("lastTradePrice"),
        "prices": {
            "yes": yes_price,
            "no": no_price,
        },
        "raw_outcomes": outcomes,
        "raw_outcomePrices": prices,
    }

    return jsonify(result)