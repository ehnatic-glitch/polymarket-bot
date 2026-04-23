from flask import Flask, jsonify, request
import requests
import json
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


def score_market(m):
    score = 0
    notes = []

    liquidity = to_float(m.get("liquidity"))
    volume24hr = to_float(m.get("volume24hr"))
    yes_price, no_price = get_yes_no_prices(m)
    question = (m.get("question") or "").lower()
    end_date = parse_date(m.get("endDate"))

    if liquidity > 250000:
        score += 3
        notes.append("high_liquidity")
    elif liquidity > 100000:
        score += 2
        notes.append("good_liquidity")
    elif liquidity > 50000:
        score += 1
        notes.append("ok_liquidity")
    else:
        score -= 3
        notes.append("thin_liquidity")

    if volume24hr > 500000:
        score += 3
        notes.append("high_volume")
    elif volume24hr > 100000:
        score += 2
        notes.append("good_volume")
    elif volume24hr > 25000:
        score += 1
        notes.append("ok_volume")
    else:
        score -= 2
        notes.append("low_volume")

    if isinstance(yes_price, (int, float)):
        if 0.15 <= yes_price <= 0.85:
            score += 2
            notes.append("balanced_price")
        elif yes_price < 0.10 or yes_price > 0.90:
            score -= 3
            notes.append("extreme_price")
        elif yes_price < 0.15 or yes_price > 0.85:
            score -= 1
            notes.append("stretched_price")
    else:
        score -= 2
        notes.append("missing_price")

    if end_date:
        now = datetime.now(timezone.utc)
        days_to_end = (end_date - now).total_seconds() / 86400

        if 3 <= days_to_end <= 180:
            score += 1
            notes.append("good_time_window")
        elif days_to_end < 1:
            score -= 3
            notes.append("too_close_to_expiry")
        elif days_to_end < 3:
            score -= 2
            notes.append("close_to_expiry")
        elif days_to_end > 365:
            score -= 2
            notes.append("too_far_expiry")
    else:
        days_to_end = None
        score -= 1
        notes.append("missing_end_date")

    noise_keywords = [
        "up or down",
        "5 minutes",
        "hourly",
        "today",
        "this week",
        "daily",
        "minute",
        "opens up or down",
    ]
    if any(k in question for k in noise_keywords):
        score -= 3
        notes.append("noise_market")

    sports_keywords = [
        "world cup",
        "nba finals",
        "mlb",
        "nfl",
        "champions league",
        "stanley cup",
    ]
    if any(k in question for k in sports_keywords):
        score -= 2
        notes.append("sports_hype_risk")

    ambiguity_keywords = [
        "called by",
        "out by",
        "any country leave",
        "military clash",
    ]
    if any(k in question for k in ambiguity_keywords):
        score -= 2
        notes.append("possible_ambiguity")

    if score >= 5:
        flag = "WATCH"
    elif score >= 2:
        flag = "MAYBE"
    else:
        flag = "PASS"

    return {
        "candidateScore": score,
        "flag": flag,
        "notes": notes,
        "yesPrice": yes_price,
        "noPrice": no_price,
        "daysToEnd": days_to_end,
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
        "notes": scored["notes"],
        "yesPrice": scored["yesPrice"],
        "noPrice": scored["noPrice"],
        "daysToEnd": scored["daysToEnd"],
    }


def flag_priority(flag):
    if flag == "WATCH":
        return 0
    if flag == "MAYBE":
        return 1
    return 2


@app.route("/markets")
def markets():
    limit = int(request.args.get("limit", "20"))
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

    rows = []
    for m in data:
        if m.get("active") is not True:
            continue
        if m.get("closed") is True:
            continue

        row = build_market_row(m)
        rows.append(row)

    rows.sort(
        key=lambda x: (
            flag_priority(x.get("flag")),
            -to_float(x.get("candidateScore")),
            -to_float(x.get("liquidity")),
            -to_float(x.get("volume24hr")),
        )
    )

    rows = rows[:limit]

    return jsonify({
        "count": len(rows),
        "params": params,
        "markets": rows,
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

    rows = []
    for m in data:
        if m.get("active") is not True:
            continue
        if m.get("closed") is True:
            continue

        row = build_market_row(m)
        rows.append(row)

    rows.sort(
        key=lambda x: (
            flag_priority(x.get("flag")),
            -to_float(x.get("candidateScore")),
            -to_float(x.get("liquidity")),
            -to_float(x.get("volume24hr")),
        )
    )

    return jsonify(rows[:limit])


@app.route("/dashboard")
def dashboard():
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Polymarket Candidate Dashboard</title>
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
    }
    .watch {
      background: #e6f4ea;
      color: #137333;
    }
    .maybe {
      background: #fff4e5;
      color: #b06000;
    }
    .pass {
      background: #fce8e6;
      color: #c5221f;
    }
    a {
      color: #1558d6;
      text-decoration: none;
    }
    a:hover {
      text-decoration: underline;
    }
    .notes {
      font-size: 12px;
      color: #666;
      max-width: 220px;
      white-space: normal;
    }
  </style>
</head>
<body>
  <h1>Polymarket Candidate Dashboard</h1>
  <p class="small">Filtrované podľa v5 discovery logiky: score, liquidity, volume, price shape, expiry, noise risk.</p>

  <div class="section">
    <h2>Top candidates</h2>
    <div id="markets-error" class="error" style="display:none;"></div>
    <table id="markets-table">
      <thead>
        <tr>
          <th>Flag</th>
          <th>Score</th>
          <th>Question</th>
          <th>Yes</th>
          <th>No</th>
          <th>24h volume</th>
          <th>Liquidity</th>
          <th>End</th>
          <th>Notes</th>
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

    function flagBadge(flag) {
      if (flag === 'WATCH') return '<span class="badge watch">WATCH</span>';
      if (flag === 'MAYBE') return '<span class="badge maybe">MAYBE</span>';
      return '<span class="badge pass">PASS</span>';
    }

    async function loadMarkets() {
      const errorEl = document.getElementById('markets-error');
      const tbody = document.querySelector('#markets-table tbody');

      try {
        const res = await fetch('/markets?limit=25');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();

        tbody.innerHTML = '';

        (data.markets || []).forEach(m => {
          const tr = document.createElement('tr');
          const link = m.slug
            ? 'https://polymarket.com/market/' + m.slug
            : null;

          tr.innerHTML = `
            <td>${flagBadge(m.flag)}</td>
            <td>${m.candidateScore ?? ''}</td>
            <td>${m.question || ''}</td>
            <td>${fmtPrice(m.yesPrice)}</td>
            <td>${fmtPrice(m.noPrice)}</td>
            <td>${fmtInt(m.volume24hr)}</td>
            <td>${fmtInt(m.liquidity)}</td>
            <td>${m.endDate ? new Date(m.endDate).toLocaleString('sk-SK') : ''}</td>
            <td class="notes">${(m.notes || []).join(', ')}</td>
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