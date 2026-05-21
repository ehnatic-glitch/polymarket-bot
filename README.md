# Polymarket Sniper v2.0 — Lean & Mean

Flask aplikácia, ktorá implementuje **Polymarket Framework v2.0** ako live dashboard
nad Polymarket Gamma + Data API.

---

## Čo systém robí

Pre každý aktívny Polymarket trh aplikuje 4 piliere frameworku:

1. **Pillar 1 — Kill-Switch** (3 binárne otázky):
   - Q1 Edge Check: má trh text/oracle/time-decay edge, alebo je to smerová lotéria?
   - Q2 Correlation Check: nepridávame tretiu pozíciu do toho istého klastra?
   - Q3 Liquidity Check: vieme z pozície vyskočiť?
   - Aj jedno NIE = okamžitý PASS.

2. **Pillar 2 — Tier Sizing**:
   - **Tier A (30 USDC)**: clear text/oracle edge + strong catalyst + excellent liquidity
   - **Tier B (15 USDC)**: time-decay setups, good liquidity
   - **Tier C (8 USDC)**: asymetrické centovky (1-5¢)
   - **PASS**: nič z toho

3. **Pillar 3 — Exit Engine** (automaticky podľa entry ceny):
   - **< 25¢**: free-roll plán (predaj N shares pre vytiahnutie 100% vkladu)
   - **25-40¢**: štandardné TP1/TP2
   - **> 40¢**: mandatory time-stop dátum

4. **Pillar 4 — Devil's Advocate**: auto-generovaný worst-case scenár pre každý trh.

**Discipline layer** vynucuje frameworkové limity:
- Hard blocks: portfolio capacity (>3 CORE + 4 SANDBOX), averaging-down, expozícia >200 USDC
- Soft warnings: oversize (>1.3× recommended stake)

---

## Deployment na Render

### Príprava súborov

V repozitári musia byť tieto 3 súbory:

```
app.py
requirements.txt
README.md  (voliteľné)
```

### Konfigurácia Render service

- **Environment**: Python 3
- **Build command**: `pip install -r requirements.txt`
- **Start command**: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60`
- **Port**: automaticky cez `$PORT` (Render to nastaví)

Ak preferuješ jednoduchšie nasadenie bez gunicornu:
- **Start command**: `python app.py`

(Flask dev server zvládne pár requestov, ale pre produkciu odporúčam gunicorn.)

### Po deploye

Otvor: `https://<tvoja-service>.onrender.com/dashboard`

Voliteľne vlož svoju Polymarket proxy wallet pre live portfolio monitoring
(uloží sa do localStorage prehliadača).

---

## API endpointy

| Method | Endpoint | Popis |
|---|---|---|
| GET | `/` | API info |
| GET | `/health` | Healthcheck |
| GET | `/markets?min_liquidity=100000&hide_pass=true&category=Politics&wallet=0x...` | Hlavný scanner |
| POST | `/analyze-market` | Analyzuj konkrétny trh. Body: `{slug, wallet?}` |
| POST | `/pre-trade-check` | Pre-trade gate. Body: `{market_slug, intended_side, intended_stake_usdc, intended_price?, wallet?}` |
| GET | `/portfolio-status?wallet=0x...` | Snapshot portfolio voči v2.0 limitom |
| GET | `/market-trades?slug=...&limit=20&min_amount=200000` | Whale trades pre market |
| GET | `/wallet-history?wallet=0x...&limit=30` | Trade history wallety |
| GET | `/leaderboard?limit=10` | Top traders |
| GET | `/dashboard` | UI |

---

## Konfigurácia (úprava v app.py)

```python
APP_CONFIG = {
    "bankroll_usdc": 500.0,           # tvoj celkový bankroll
    "max_total_exposure_usdc": 200.0, # max 40% bankrollu naraz
    "max_core_positions": 3,          # framework limit
    "max_sandbox_positions": 4,       # framework limit
    "tier_a_stake": 30.0,
    "tier_b_stake": 15.0,
    "tier_c_stake_default": 8.0,
    "default_min_liquidity": 100000.0,
    "whale_trade_min_notional": 200000.0,
}
```

---

## Bezpečnosť

- App **iba číta** Polymarket API. Neexekvuje žiadne trades.
- Wallet adresa sa používa len na čítanie pozícií a histórie (verejné dáta).
- Žiadne private keys, žiadne signing, žiadne fondové operácie.
- Po kliknutí EXECUTE sa otvorí Polymarket URL v novom tabe a používateľ klikne BUY ručne.

---

## Poznámky

- Pre live whale flow musí mať trh aspoň 1 trade > $200k notional (default filter). Pre menej likvidné trhy bude whale panel prázdny — to je správne.
- Leaderboard endpoint Polymarketu sa môže občas správať pomaly alebo vrátiť 5xx. App to ošetrí a UI zobrazí prázdny zoznam namiesto crash-u.
- Correlation Check (Pillar 1 Q2) funguje len ak je wallet pripojená — bez wallety appka nevie, čo už držíš.
