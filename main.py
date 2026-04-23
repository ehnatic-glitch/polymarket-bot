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


def categorize_market(question):
    q = (question or "").lower()

    sports_keywords = [
        "world cup", "nba finals", "nfl", "mlb", "stanley cup",
        "champions league", "premier league", "ufc", "fifa"
    ]
    politics_keywords = [
        "presidential", "election", "senate", "house", "democratic",
        "republican", "nomination", "trump", "vance", "rubio", "newsom"
    ]
    crypto_keywords = [
        "bitcoin", "btc", "ethereum", "eth", "solana", "xrp", "crypto",
        "kraken", "coinbase", "ipo"
    ]
    geopolitics_keywords = [
        "ukraine", "nato", "china", "india", "macron", "military",
        "war", "troops", "ceasefire", "taiwan"
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


def score_market(m):
    score = 0
    notes = []

    liquidity = to_float(m.get("liquidity"))
    volume24hr = to_float(m.get("volume24hr"))
    yes_price, no_price = get_yes_no_prices(m)
    question = (m.get("question") or "").lower()
    category = categorize_market(m.get("question"))
    end_date = parse_date(m.get("endDate"))

    if liquidity >= 500000:
        score += 4
        notes.append("very_high_liquidity")
    elif liquidity >= 250000:
        score += 3
        notes.append("high_liquidity")
    elif liquidity >= 150000:
        score += 2
        notes.append("good_liquidity")
    elif liquidity >= 50000:
        score += 1
        notes.append("ok_liquidity")
    else:
        score -= 4
        notes.append("thin_liquidity")

    if volume24hr >= 500000:
        score += 3
        notes.append("high_volume")
    elif volume24hr >= 100000:
        score += 2
        notes.append("good_volume")
    elif volume24hr >= 25000:
        score += 1
        notes.append("ok_volume")
    else:
        score -= 3
        notes.append("low_volume")

    if isinstance(yes_price, (int, float)):
        if 0.12 <= yes_price <= 0.88:
            score += 2
            notes.append("balanced_price")
        elif yes_price < 0.10 or yes_price > 0.90:
            score -= 4
            notes.append("extreme_price")
        else:
            score -= 1
            notes.append("stretched_price")
    else:
        score -= 3
        notes.append("missing_price")

    now = datetime.now(timezone.utc)
    days_to_end = None
    if end_date:
        days_to_end = (end_date - now).total_seconds() / 86400
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
        score -= 5
        notes.append("noise_market")

    ambiguity_keywords = [
        "called by", "out by", "any country leave", "military clash",
        "good faith", "official sources"
    ]
    if any(k in question for k in ambiguity_keywords):
        score -= 3
        notes.append("possible_ambiguity")

    if category == "Sports":
        score -= 2
        notes.append("sports_hype_risk")

    if category == "Narrative":
        score -= 3
        notes.append("narrative_risk")

    if category == "Politics" and end_date:
        if days_to_end is not None and days_to_end > 365:
            score -= 2
            notes.append("long_dated_politics")

    strict_watch = (
        liquidity >= 150000 and
        volume24hr >= 50000 and
        isinstance(yes_price, (int, float)) and
        0.12 <= yes_price <= 0.88 and
        "possible_ambiguity" not in notes and
        "noise_market" not in notes and
        "extreme_price" not in notes and
        "narrative_risk" not in notes
    )

    if strict_watch and score >= 6:
        flag = "WATCH"
    elif score >= 2:
        flag = "MAYBE"
    else:
        flag = "PASS"

    summary_parts = []

    if liquidity >= 250000:
        summary_parts.append("likvidný")
    elif liquidity >= 100000:
        summary_parts.append("slušná likvidita")
    else:
        summary_parts.append("slabšia likvidita")

    if volume24hr >= 100000:
        summary_parts.append("dobrý objem")
    elif volume24hr >= 25000:
        summary_parts.append("ok objem")
    else:
        summary_parts.append("slabý objem")

    if isinstance(yes_price, (int, float)):
        if 0.12 <= yes_price <= 0.88:
            summary_parts.append("vyvážená cena")
        elif yes_price < 0.10 or yes_price > 0.90:
            summary_parts.append("extrémna cena")
        else:
            summary_parts.append("natiahnutá cena")
    else:
        summary_parts.append("chýba cena")

    if "possible_ambiguity" in notes:
        summary_parts.append("ambiguity risk")
    if "sports_hype_risk" in notes:
        summary_parts.append("športový hype")
    if "narrative_risk" in notes:
        summary_parts.append("narrative risk")
    if "long_dated_politics" in notes:
        summary_parts.append("príliš vzdialené expiry")

    summary = ", ".join(summary_parts)

    return {
        "candidateScore": score,
        "flag": flag,
        "notes": notes,
        "summary": summary,
        "yesPrice": yes_price,
        "noPrice": no_price,
        "daysToEnd": days_to_end,
        "category": category,
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
        "summary": scored["summary"],
        "yesPrice": scored["yesPrice"],
        "noPrice": scored["noPrice"],
        "daysToEnd": scored["daysToEnd"],
        "category": scored["category"],
    }


def flag_priority(flag):
    if flag == "WATCH":
        return 0
    if flag == "MAYBE":
        return 1
    return 2


@app.route("/markets")
def markets():
    limit = int(request.args.get("limit", "50"))
    active = request.args.get("active", "true")
    closed = request.args.get("closed", "false")
    min_liquidity = to_float(request.args.get("min_liquidity", "0"))
    min_volume = to_float(request.args.get("min_volume", "0"))
    hide_pass = request.args.get("hide_pass", "false").lower() == "true"
    category_filter = request.args.get("category", "").strip()

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

        if to_float(row.get("liquidity")) < min_liquidity:
            continue
        if to_float(row.get("volume24hr")) < min_volume:
            continue
        if hide_pass and row.get("flag") == "PASS":
            continue
        if category_filter and row.get("category") != category_filter:
            continue

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
        "markets": rows,
        "filters": {
            "min_liquidity": min_liquidity,
            "min_volume": min_volume,
            "hide_pass": hide_pass,
            "category": category_filter,
        }
    })


@app.route("/markets/top")
def markets_top():
    return markets()


@app.route("/dashboard")
def dashboard():
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Polymarket Candidate Dashboard v2</title>
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
    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 14px;
      align-items: end;
    }
    .control {
      display: flex;
      flex-direction: column;
      gap: 4px;
      min-width: 160px;
    }
    label {
      font-size: 12px;
      color: #555;
      font-weight: 600;
    }
    input, select {
      padding: 8px 10px;
      border: 1px solid #ddd;
      border-radius: 6px;
      font: inherit;
      background: #fff;
    }
    .checkbox-wrap {
      display: flex;
      align-items: center;
      gap: 8px;
      padding-top: 22px;
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
      position: sticky;
      top: 0;
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
    .cat {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      background: #eef2ff;
      color: #3949ab;
      font-weight: 600;
    }
    a {
      color: #1558d6;
      text-decoration: none;
    }
    a:hover {
      text-decoration: underline;
    }
    .summary {
      font-size: 12px;
      color: #666;
      max-width: 260px;
      white-space: normal;
    }
    .table-wrap {
      overflow: auto;
      max-height: 78vh;
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
  </style>
</head>
<body>
  <h1>Polymarket Candidate Dashboard</h1>
  <p class="small">v2: category, hide PASS, horné filtre, sprísnený WATCH scoring podľa v5 heuristiky.</p>

  <div class="section">
    <h2>Top candidates</h2>

    <div class="controls">
      <div class="control">
        <label for="category">Category</label>
        <select id="category">
          <option value="">All</option>
          <option value="Sports">Sports</option>
          <option value="Politics">Politics</option>
          <option value="Crypto">Crypto</option>
          <option value="Geopolitics">Geopolitics</option>
          <option value="Narrative">Narrative</option>
          <option value="Other">Other</option>
        </select>
      </div>

      <div class="control">
        <label for="minLiquidity">Min liquidity</label>
        <select id="minLiquidity">
          <option value="0">0</option>
          <option value="50000">50 000</option>
          <option value="100000" selected>100 000</option>
          <option value="150000">150 000</option>
          <option value="250000">250 000</option>
        </select>
      </div>

      <div class="control">
        <label for="minVolume">Min 24h volume</label>
        <select id="minVolume">
          <option value="0">0</option>
          <option value="25000" selected>25 000</option>
          <option value="50000">50 000</option>
          <option value="100000">100 000</option>
          <option value="250000">250 000</option>
        </select>
      </div>

      <div class="checkbox-wrap">
        <input type="checkbox" id="hidePass" checked />
        <label for="hidePass">Hide PASS</label>
      </div>

      <div class="checkbox-wrap">
        <input type="checkbox" id="watchOnly" />
        <label for="watchOnly">WATCH only</label>
      </div>

      <div class="control">
        <button onclick="loadMarkets()">Refresh</button>
      </div>
    </div>

    <div class="count" id="countBox"></div>
    <div id="markets-error" class="error" style="display:none;"></div>

    <div class="table-wrap">
      <table id="markets-table">
        <thead>
          <tr>
            <th>Flag</th>
            <th>Score</th>
            <th>Category</th>
            <th>Question</th>
            <th>Yes</th>
            <th>No</th>
            <th>24h volume</th>
            <th>Liquidity</th>
            <th>End</th>
            <th>Summary</th>
            <th>Link</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
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

    function catBadge(cat) {
      return '<span class="cat">' + (cat || 'Other') + '</span>';
    }

    async function loadMarkets() {
      const errorEl = document.getElementById('markets-error');
      const tbody = document.querySelector('#markets-table tbody');
      const countBox = document.getElementById('countBox');

      const category = document.getElementById('category').value;
      const minLiquidity = document.getElementById('minLiquidity').value;
      const minVolume = document.getElementById('minVolume').value;
      const hidePass = document.getElementById('hidePass').checked;
      const watchOnly = document.getElementById('watchOnly').checked;

      try {
        const params = new URLSearchParams({
          limit: '80',
          min_liquidity: minLiquidity,
          min_volume: minVolume,
          hide_pass: hidePass ? 'true' : 'false',
          category: category
        });

        const res = await fetch('/markets?' + params.toString());
        if (!res.ok) throw new Error('HTTP ' + res.status);

        const data = await res.json();
        let markets = data.markets || [];

        if (watchOnly) {
          markets = markets.filter(m => m.flag === 'WATCH');
        }

        tbody.innerHTML = '';

        markets.forEach(m => {
          const tr = document.createElement('tr');
          const link = m.slug
            ? 'https://polymarket.com/market/' + m.slug
            : null;

          tr.innerHTML = `
            <td>${flagBadge(m.flag)}</td>
            <td>${m.candidateScore ?? ''}</td>
            <td>${catBadge(m.category)}</td>
            <td>${m.question || ''}</td>
            <td>${fmtPrice(m.yesPrice)}</td>
            <td>${fmtPrice(m.noPrice)}</td>
            <td>${fmtInt(m.volume24hr)}</td>
            <td>${fmtInt(m.liquidity)}</td>
            <td>${m.endDate ? new Date(m.endDate).toLocaleString('sk-SK') : ''}</td>
            <td class="summary">${m.summary || ''}</td>
            <td>${link ? '<a href="' + link + '" target="_blank" rel="noopener noreferrer">Open</a>' : ''}</td>
          `;
          tbody.appendChild(tr);
        });

        countBox.textContent = 'Zobrazené markety: ' + markets.length;
        errorEl.style.display = 'none';
      } catch (err) {
        errorEl.textContent = 'Chyba pri načítaní markets: ' + err.message;
        errorEl.style.display = 'block';
      }
    }

    document.getElementById('category').addEventListener('change', loadMarkets);
    document.getElementById('minLiquidity').addEventListener('change', loadMarkets);
    document.getElementById('minVolume').addEventListener('change', loadMarkets);
    document.getElementById('hidePass').addEventListener('change', loadMarkets);
    document.getElementById('watchOnly').addEventListener('change', loadMarkets);

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