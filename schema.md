# Smart Market Watchlist — Database Schema & Architecture

This document details the database schema, entity relationships, indexing strategies, and architectural design principles behind the **Smart Market Watchlist** system.

---

## 1. Architectural Philosophy & Design Principles

The schema is built around three core architectural tenets:

### A. Market Truth vs. User Relevance
- **Market Truth (Objective)**: Did an asset break out, cross its moving average, or experience abnormal volume? This is an objective fact about the symbol, calculated **once** regardless of whether 1 user or 10,000 users are tracking it. Detected events are written to an append-only sequence log (`symbol_events`).
- **User Relevance (Subjective)**: "What changed since *I* last looked?" is a cheap user-specific read cursor (`user_watch_state.last_seen_seq`) into that shared append-only log.
- **Scaling Complexity**: The detection workload scales with **$\mathcal{O}(\text{unique symbols})$**, **not** $\mathcal{O}(\text{users} \times \text{symbols})$.

### B. Event Log over Snapshot Diffing
Traditional watchlist designs diff two price snapshots (when a user leaves vs. when they return). This pattern collapses under multi-device usage (e.g., checking on mobile at 9:00 AM, desktop at 9:30 AM).  
By assigning a strictly monotonic sequence (`seq`) per symbol and persisting a user cursor (`last_seen_seq`), state resolution is always deterministic:
$$\text{Unseen Events} = \{ e \in \text{symbol\_events} \mid e.\text{symbol} = s \land e.\text{seq} > \text{cursor}.\text{last\_seen\_seq} \}$$

### C. $O(1)$ Statistical State Tracking via EMAs
Rather than maintaining an unbounded time-series table and executing expensive window functions on every tick, all rolling statistics (mean, variance/dispersion, short/long moving averages) are maintained as **Exponential Moving Averages (EMAs)** directly within the `symbols` record.

---

## 2. Entity-Relationship Diagram (ERD)

```mermaid
erDiagram
    users ||--o{ watchlists : "owns (1:N)"
    users ||--o{ user_watch_state : "tracks read cursor (1:N)"
    watchlists ||--o{ watchlist_items : "contains (1:N)"
    symbols ||--o{ watchlist_items : "referenced by (1:N)"
    symbols ||--o{ symbol_events : "generates (1:N)"
    symbols ||--o{ price_ticks : "records history (1:N)"
    symbols ||--o{ user_watch_state : "tracked by (1:N)"

    users {
        int id PK
        string username UK "Indexed"
        string password_hash
        datetime created_at
    }

    watchlists {
        int id PK
        int user_id FK
        string name
        datetime created_at
    }

    watchlist_items {
        int id PK
        int watchlist_id FK "UQ(watchlist_id, symbol)"
        string symbol FK
        datetime added_at
    }

    symbols {
        string symbol PK "Ticker (e.g. AAPL, IBM)"
        string name
        float last_price
        float prev_close
        float day_open
        float day_high
        float day_low
        float last_volume
        float volume_mean "Rolling EMA"
        float volume_std "Rolling EMA"
        float price_mean "Return EMA"
        float price_std "Return dispersion EMA"
        float sma_short "Fast EMA"
        float sma_long "Slow EMA"
        int prev_cross_sign "MA trend tracker"
        string source "Provenance (live_alphavantage, etc.)"
        boolean is_stale
        int event_seq_counter "Monotonic sequence counter"
        datetime updated_at
    }

    symbol_events {
        int id PK
        string symbol FK "Indexed, UQ(symbol, seq)"
        int seq "Per-symbol monotonic ID"
        string event_type "price_move, volume_spike, ma_cross, etc."
        float score "Attention weight (0-100)"
        text message "Human-readable description"
        float price_at_event
        datetime timestamp "Indexed"
    }

    price_ticks {
        int id PK
        string symbol FK "Indexed"
        float price
        datetime timestamp "Indexed"
    }

    user_watch_state {
        int id PK
        int user_id FK "UQ(user_id, symbol)"
        string symbol FK
        int last_seen_seq "Read cursor"
        datetime last_seen_at
    }
```

---

## 3. Table Definitions & Column Specifications

### 3.1. `users`
Stores user identities and credentials for authentication.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | `INTEGER` | Primary Key, Autoincrement | Unique user identifier |
| `username` | `VARCHAR` | Unique, Not Null, Index | Unique login handle (3–32 characters) |
| `password_hash`| `VARCHAR` | Not Null | Bcrypt hashed password |
| `created_at` | `DATETIME`| Default: UTC Now | Account creation timestamp |

- **Cascades**: Deleting a user cascades and deletes all associated `watchlists` and `user_watch_state` records.

---

### 3.2. `watchlists`
Named collections of watched symbols owned by a user.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | `INTEGER` | Primary Key, Autoincrement | Unique watchlist identifier |
| `user_id` | `INTEGER` | Foreign Key (`users.id`), Not Null | Watchlist owner reference |
| `name` | `VARCHAR` | Not Null | User-defined watchlist title |
| `created_at` | `DATETIME`| Default: UTC Now | Watchlist creation timestamp |

- **Cascades**: Deleting a watchlist cascades and deletes all associated `watchlist_items`.

---

### 3.3. `watchlist_items`
Join table establishing many-to-many relationships between watchlists and symbols.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | `INTEGER` | Primary Key, Autoincrement | Unique join item record identifier |
| `watchlist_id` | `INTEGER` | Foreign Key (`watchlists.id`), Not Null | Watchlist reference |
| `symbol` | `VARCHAR` | Foreign Key (`symbols.symbol`), Not Null | Symbol identifier |
| `added_at` | `DATETIME`| Default: UTC Now | Timestamp symbol was added to list |

- **Composite Unique Constraint**: `uq_watchlist_symbol (watchlist_id, symbol)` prevents duplicate entries of the same ticker inside a single watchlist.

---

### 3.4. `symbols`
Current-state cache and rolling statistical baseline for all tracked tickers.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `symbol` | `VARCHAR` | Primary Key | Upper-case ticker symbol (e.g., `NVDA`, `IBM`) |
| `name` | `VARCHAR` | Default: `""` | Company or asset description |
| `last_price` | `FLOAT` | Default: `0.0` | Most recent traded price |
| `prev_close` | `FLOAT` | Default: `0.0` | Previous session close |
| `day_open` | `FLOAT` | Default: `0.0` | Session opening price |
| `day_high` | `FLOAT` | Default: `0.0` | Current session high |
| `day_low` | `FLOAT` | Default: `0.0` | Current session low |
| `last_volume` | `FLOAT` | Default: `0.0` | Volume recorded during the latest tick |
| `volume_mean` | `FLOAT` | Default: `1,000,000.0` | Exponential moving average of tick volume |
| `volume_std` | `FLOAT` | Default: `200,000.0` | Exponential moving dispersion of tick volume |
| `price_mean` | `FLOAT` | Default: `0.0` | Exponential moving average of percentage returns |
| `price_std` | `FLOAT` | Default: `0.002` | Return dispersion EMA (floored to avoid div-by-zero) |
| `sma_short` | `FLOAT` | Default: `0.0` | Fast price EMA (decay factor $\alpha \approx 0.15$) |
| `sma_long` | `FLOAT` | Default: `0.0` | Slow price EMA (decay factor $\alpha \approx 0.03$) |
| `prev_cross_sign` | `INTEGER` | Default: `0` | Sign of $(sma_{short} - sma_{long})$: `-1`, `0`, or `+1` |
| `source` | `VARCHAR` | Default: `"simulated"` | Data provenance (`live_alphavantage`, `live_yahoo`, etc.) |
| `is_stale` | `BOOLEAN` | Default: `False` | Heartbeat health flag |
| `event_seq_counter`| `INTEGER` | Default: `0` | Monotonic counter incremented on detected events |
| `updated_at` | `DATETIME`| Default: UTC Now | Timestamp of last received tick |

---

### 3.5. `symbol_events`
Append-only chronological audit log of statistically detected market anomalies.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | `INTEGER` | Primary Key, Autoincrement | Global event row identifier |
| `symbol` | `VARCHAR` | Foreign Key (`symbols.symbol`), Not Null, Index | Target symbol |
| `seq` | `INTEGER` | Not Null | Per-symbol monotonic sequence identifier |
| `event_type` | `VARCHAR` | Not Null | Anomaly category |
| `score` | `FLOAT` | Default: `0.0` | Attention score contribution ($0 \dots 100$) |
| `message` | `TEXT` | Nullable | Human-readable explanation of the anomaly |
| `price_at_event` | `FLOAT` | Nullable | Snapshot price at the moment event fired |
| `timestamp` | `DATETIME`| Default: UTC Now, Index | Event generation timestamp |

- **Composite Unique Constraint**: `uq_symbol_seq (symbol, seq)` ensures strict sequence integrity per symbol.
- **Recognized Event Types**:
  - `price_move`: Rapid directional price velocity exceeding adaptive Z-score threshold.
  - `volume_spike`: Volume surge exceeding $Z \ge 2.5$ of rolling volume baseline.
  - `ma_cross`: Golden cross / Death cross between fast and slow EMAs beyond deadband.
  - `new_high`: Price established a new intra-day session high.
  - `new_low`: Price established a new intra-day session low.
  - `gap`: Significant opening gap relative to previous session close.

---

### 3.6. `price_ticks`
Session-scoped tick buffer used to render client-side HTML5 Canvas charts.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | `INTEGER` | Primary Key, Autoincrement | Record identifier |
| `symbol` | `VARCHAR` | Foreign Key (`symbols.symbol`), Not Null, Index | Target symbol |
| `price` | `FLOAT` | Not Null | Price snapshot |
| `timestamp` | `DATETIME`| Default: UTC Now, Index | Tick timestamp |

- **Pruning Policy**: Maintained at a fixed rolling window (capped at `MAX_HISTORY_POINTS = 500`, roughly ~25 minutes of session ticks). Older points are pruned in batches every 20 ticks (`PRUNE_EVERY_N_TICKS`) to prevent storage degradation.

---

### 3.7. `user_watch_state`
Per-user read cursor tracking unread market events.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | `INTEGER` | Primary Key, Autoincrement | Record identifier |
| `user_id` | `INTEGER` | Foreign Key (`users.id`), Not Null | User reference |
| `symbol` | `VARCHAR` | Foreign Key (`symbols.symbol`), Not Null | Symbol reference |
| `last_seen_seq` | `INTEGER` | Default: `0` | Highest `seq` in `symbol_events` acknowledged by user |
| `last_seen_at` | `DATETIME`| Default: UTC Now | Timestamp of last acknowledgment |

- **Composite Unique Constraint**: `uq_user_symbol_state (user_id, symbol)` guarantees a single, deterministic read cursor per user per symbol.

---

## 4. Concurrency & Engine Pragmas (SQLite WAL Mode)

SQLite is configured in [`backend/app/database.py`](backend/app/database.py) to run in **Write-Ahead Logging (WAL)** mode on every connection:

```python
@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()
```

### Why This Matters:
1. **Zero Contention Between Feed & API**: In SQLite's standard rollback journal mode, writing locks the entire database, causing readers to block. Under WAL mode, **readers and writers execute simultaneously**.
2. **Crash Resilience**: `synchronous=NORMAL` maintains write safety while avoiding synchronous disk flush bottlenecks on every 3-second tick.
3. **Timeout Safeguard**: The connection pool specifies a 15-second busy timeout (`connect_args={"timeout": 15}`) to cleanly handle transient multi-writer interleaves.

---

## 5. State Progression & Polling Workflow

```
[ Frontend Client ]                                      [ SQLite Database ]
         │                                                        │
         │  1. GET /api/watchlists/{id}/state (every 5s)          │
         ├───────────────────────────────────────────────────────►│
         │     (Read-only peek: computes diff where               │
         │      symbol_events.seq > user_watch_state.last_seen_seq│
         │      WITHOUT modifying last_seen_seq)                  │
         │◄───────────────────────────────────────────────────────┤
         │                                                        │
         │  2. User opens symbol drawer / clicks ticker           │
         │  POST /api/symbols/{sym}/ack                           │
         ├───────────────────────────────────────────────────────►│
         │     (Advances user_watch_state.last_seen_seq to max seq│
         │      acknowledged by explicit human action)            │
         │◄───────────────────────────────────────────────────────┤
```

---

## 6. Migration Guide to PostgreSQL

The schema strictly adheres to standard SQLAlchemy types with zero proprietary dialect dependencies. To transition from SQLite to PostgreSQL:

1. Update `SQLALCHEMY_DATABASE_URL` in [`backend/app/database.py`](backend/app/database.py):
   ```python
   SQLALCHEMY_DATABASE_URL = "postgresql+psycopg2://user:password@localhost:5432/watchlist_db"
   ```
2. Remove SQLite-specific connection arguments (`check_same_thread`, `PRAGMA journal_mode=WAL`).
3. Run Alembic or allow `Base.metadata.create_all(bind=engine)` to initialize tables directly in PostgreSQL.
