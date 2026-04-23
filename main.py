from flask import Flask, jsonify, request
import requests

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
            "/events"
        ]
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/markets")
def markets():
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
        "limit": request.args.get("limit", "20"),
        "offset": request.args.get("offset", "0"),
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
@app.route("/dashboard")
def dashboard():
    # Vrátime jednoduchú HTML stránku priamo ako string
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
    async function loadMarkets() {
      const errorEl = document.getElementById('markets-error');
      const tbody = document.querySelector('#markets-table tbody');
      try {
        const res = await fetch('/markets?limit=10&order=volume24hr');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        tbody.innerHTML = '';
        (data.markets || []).forEach(m => {
          const tr = document.createElement('tr');
          const prices = m.outcomePrices || [];
          const yes = prices[0] ?? null;
          const no = (yes !== null) ? (1 - yes) : null;

          const link = m.slug
            ? 'https://polymarket.com/event/' + m.slug
            : null;

          tr.innerHTML = `
            <td>${m.question || ''}</td>
            <td>${yes !== null ? yes.toFixed(3) : ''}</td>
            <td>${no !== null ? no.toFixed(3) : ''}</td>
            <td>${m.volume24hr ?? ''}</td>
            <td>${m.liquidity ?? ''}</td>
            <td>${m.endDate ? new Date(m.endDate).toLocaleString() : ''}</td>
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
          const status = e.active ? 'active' : (e.closed ? 'closed' : 'other');
          let statusHtml = '';
          if (status === 'active') {
            statusHtml = '<span class="badge badge-active">ACTIVE</span>';
          } else if (status === 'closed') {
            statusHtml = '<span class="badge badge-closed">CLOSED</span>';
          } else {
            statusHtml = '<span class="badge">OTHER</span>';
          }

          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td>${e.title || ''}</td>
            <td>${statusHtml}</td>
            <td>${e.volume24hr ?? ''}</td>
            <td>${e.liquidity ?? ''}</td>
            <td>${e.marketsCount ?? ''}</td>
            <td>${e.endDate ? new Date(e.endDate).toLocaleString() : ''}</td>
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
@app.route("/analyze-market")
def analyze_market():
    slug = request.args.get("slug")
    if not slug:
        return jsonify({"error": "Missing slug parameter"}), 400

    # 1) Fetch market by slug from Gamma API
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

    prices = m.get("outcomePrices") or []
    yes_price = prices[0] if len(prices) > 0 else None
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
        "prices": {
            "yes": yes_price,
            "no": no_price,
        },
        "raw_outcomes": m.get("outcomes"),
        "raw_outcomePrices": prices,
    }

    return jsonify(result)

    return jsonify({
        "count": len(simplified),
        "params": params,
        "events": simplified,
    })