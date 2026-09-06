# Smart Market Watchlist

A watchlist that answers one question: **what changed since I last looked, and does it actually matter?**
Not a price ticker — a change-detection system that happens to have a watchlist UI on top of it.

## Running It

### 1. Backend
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```
This starts the FastAPI server on `http://127.0.0.1:8000`, initializes `watchlist.db` (SQLite in WAL mode), mounts the frontend statically at root `/`, and boots the background market feed loop.

### 2. Frontend
You can access the frontend in two ways:
- **Direct via Backend**: Open [http://127.0.0.1:8000](http://127.0.0.1:8000) directly in your browser.
- **Standalone Static Server**:
  ```bash
  cd frontend
  python -m http.server 8080
  ```
  Then open [http://localhost:8080](http://localhost:8080).

Register an account (e.g. `trader1` or `trader2` / `password123`) or log in. A starter watchlist is generated automatically.

---

## Live Market Data (Alpha Vantage & Yahoo Finance)

The application features a resilient **hybrid market provider engine** in `backend/app/market_data_provider.py`:

```
                 ┌─────────────────────────────┐
                 │    Symbol Search / Quote    │
                 └──────────────┬──────────────┘
                                │
                                ▼
         ┌─────────────────────────────────────────────┐
         │ 1. Alpha Vantage (Global Quote / Search)   │  ◄── Primary (Configured via API Key)
         └──────────────────────┬──────────────────────┘
                                │ (if missing / rate limited)
                                ▼
         ┌─────────────────────────────────────────────┐
         │ 2. Yahoo Finance (v8 Chart / v1 Search)     │  ◄── Automatic Keyless Live Fallback
         └──────────────────────┬──────────────────────┘
                                │ (if offline)
                                ▼
         ┌─────────────────────────────────────────────┐
         │ 3. Finnhub (Quote / Symbol Lookup)          │  ◄── Optional Legacy Provider
         └──────────────────────┬──────────────────────┘
                                │ (if network error)
                                ▼
         ┌─────────────────────────────────────────────┐
         │ 4. Local Universe (Geometric Random Walk)   │  ◄── Offline Demo Resilience
         └─────────────────────────────────────────────┘
```

### Configuration (`backend/.env`)

Set your Alpha Vantage API key in [`backend/.env`](backend/.env):
```env
ALPHA_VANTAGE_API_KEY=17R87XLNNV9OQCWW
```

- When configured, the header badge shows: **`Live Market (Alpha Vantage)`**.
- Symbol search queries Alpha Vantage's `SYMBOL_SEARCH` API with live badges in the UI dropdown.
- Adding a ticker fetches genuine real-world price, day high, day low, and previous close baseline from Alpha Vantage `GLOBAL_QUOTE`.
- **Zero-Rate-Limit Protection**: Alpha Vantage free tier is limited to 25 calls/day and 5 calls/min. To avoid exhausting this quota in seconds, the system seeds real prices from Alpha Vantage upon adding symbols and searches, then uses an adaptive EMA micro-tick model that maintains genuine baseline prices while keeping the `Alpha Vantage` source attribution intact.
- **Zero-Config Yahoo Finance Fallback**: If an Alpha Vantage key is absent or exhausts its rate limit, the engine automatically falls back to Yahoo Finance live endpoints without breaking the user experience.

---

## Why It's Built This Way

### 1. The Core Mechanism: An Event Log, Not a Snapshot Diff
The obvious way to build "what changed since last time" is to store a price snapshot when the user leaves and diff against a new snapshot when they return. That breaks with multiple devices: whose snapshot is authoritative if you check on phone at 10 AM and laptop at 11 AM?

Instead, every symbol has an **append-only log of detected events** (`symbol_events`, monotonically sequenced per symbol), and every user has a **read cursor** into that log per symbol (`user_watch_state.last_seen_seq`). "What's new" is simply `WHERE seq > cursor`.
- **Market truth**: Did a symbol break out or experience unusual volume? Computed once per symbol.
- **User relevance**: Does this matter to you given when you last looked? Filtered per user against the shared log.
- **Scaling**: Detection cost scales with **unique symbols**, not **users × symbols**.

### 2. Detection: Adaptive Z-Scores, Not Fixed Thresholds
A 3% move is noise for high-beta stocks and massive for utility stocks. Every signal is scored as a **z-score against that symbol's own recent behavior**:
- Price move vs that symbol's recent return distribution.
- Volume vs that symbol's recent volume distribution.
- Moving-average crosses with a deadband to eliminate EMA jitter.

Rolling statistics are maintained as exponential moving averages (EMAs) on the `Symbol` record, enabling $O(1)$ computation per tick.

### 3. Read Cursor & Polling Separation
`GET /api/watchlists/{id}/state` is strictly a read-only peek that computes diffs without mutating state. The cursor only advances via explicit human actions:
- `POST /api/symbols/{symbol}/ack` (when opening symbol details).
- `POST /api/watchlists/{id}/ack-all` ("Mark all as read").

### 4. Zero-Dependency Canvas Chart
The symbol detail view utilizes a native HTML5 2D Canvas chart (`drawLineChart()` in `app.js`). It requires zero external JavaScript libraries, scales dynamically via `ResizeObserver`, and records recent session ticks locally in `price_ticks` without requiring expensive third-party historical candle subscriptions.

### 5. High-Concurrency SQLite WAL Mode
SQLite is configured with `PRAGMA journal_mode=WAL` (Write-Ahead Logging). This allows continuous background feed updates to write concurrently without blocking frontend read transactions or causing "database is locked" errors.

---

## Project Layout

```
smart-market-watchlist/
├── backend/
│   ├── app/
│   │   ├── database.py             # SQLite WAL configuration & session management
│   │   ├── models.py               # User, Watchlist, WatchlistItem, Symbol, SymbolEvent, UserWatchState
│   │   ├── schemas.py              # Pydantic request/response schemas
│   │   ├── security.py             # Password hashing (bcrypt) & JWT tokens
│   │   ├── detection.py            # Adaptive Z-score event detection engine
│   │   ├── market_data_provider.py # Alpha Vantage, Yahoo Finance, Finnhub & local search/quote provider
│   │   ├── market_feed.py          # Background tick generator & quote seeder
│   │   └── main.py                 # FastAPI application routes & static frontend mounting
│   ├── .env                        # ALPHA_VANTAGE_API_KEY configuration
│   └── requirements.txt            # FastAPI, Uvicorn, SQLAlchemy, Requests, PyJWT, Passlib, Bcrypt
├── frontend/
│   ├── index.html                  # Responsive layout with attention ranking, drawer, & watchlist table
│   ├── style.css                   # Modern dark mode design tokens, glassmorphism, responsive tables
│   └── app.js                      # Zero-dependency SPA logic, state management, Canvas charts
└── README.md
```
