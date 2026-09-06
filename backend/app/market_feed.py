"""
Market data feed.

This ships with a SIMULATED feed so the project runs end-to-end with zero
API keys / signup friction. It's deliberately isolated behind one function,
`generate_tick(symbol, last_price)` -- swapping in a real vendor (Finnhub,
Polygon, Alpaca) means replacing that function with a websocket/REST client
and leaving everything downstream (detection, events, API, frontend)
untouched, since they only depend on "a tick arrived" not on where it came
from.

The feed loop runs on a background thread (not asyncio) so it can use plain
synchronous SQLAlchemy sessions -- avoids mixing async/sync DB access for
what is, at this scale, a very cheap periodic job.

Handling of stale/conflicting data (design notes, since we only have one
simulated source here to demonstrate against):
  - every tick is timestamped; a tick older than the symbol's current
    `updated_at` is rejected as out-of-order rather than applied
  - if this were fed by 2+ vendors, the reconciliation policy would be:
    prefer the most recent timestamp; if timestamps are within the same
    tick window, prefer the higher-priority source (configurable) rather
    than silently averaging, since averaging can hide a bad feed
  - `freshness_status()` in detection.py independently flags anything that
    hasn't updated recently as "stale" in the UI, regardless of the above
"""
import random
import threading
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import models, market_data_provider
from .database import SessionLocal
from .detection import process_tick

# Small fixed universe for the demo. Adding a symbol here (and it existing
# in the `symbols` table) is all that's needed for it to start ticking.
UNIVERSE = [
    ("AAPL", "Apple Inc.", 230.0),
    ("NVDA", "NVIDIA Corp.", 180.0),
    ("TSLA", "Tesla Inc.", 340.0),
    ("MSFT", "Microsoft Corp.", 510.0),
    ("GOOGL", "Alphabet Inc.", 195.0),
    ("AMZN", "Amazon.com Inc.", 220.0),
    ("META", "Meta Platforms Inc.", 610.0),
    ("AMD", "Advanced Micro Devices", 165.0),
    ("NFLX", "Netflix Inc.", 900.0),
    ("SPY", "S&P 500 ETF", 560.0),
    ("COIN", "Coinbase Global", 210.0),
    ("PLTR", "Palantir Technologies", 45.0),
]

TICK_INTERVAL_SECONDS = 3
SHOCK_PROBABILITY = 0.06  # chance any given symbol gets an outsized move on a tick, to keep the demo lively

# Chart history cap, per symbol. This is "recent session" data for a chart,
# not a permanent archive -- 500 points at a 3s tick interval is ~25 minutes
# of history, which comfortably covers a demo/testing session. Pruned back
# to this cap periodically rather than on every single tick (see
# _tick_counter below) since the delete itself has a real cost at scale.
MAX_HISTORY_POINTS = 500
PRUNE_EVERY_N_TICKS = 20  # ~once a minute at the default 3s interval

_tick_counter = 0


def ensure_universe_seeded(db: Session):
    for symbol, name, start_price in UNIVERSE:
        existing = db.query(models.Symbol).filter(models.Symbol.symbol == symbol).first()
        if not existing:
            db.add(models.Symbol(symbol=symbol, name=name, last_price=0.0))
    db.commit()


def generate_tick(last_price: float) -> tuple[float, float]:
    """
    Pure simulation: geometric random walk for price, with an occasional
    'shock' (bigger move + volume spike) so the detection engine actually
    has something interesting to find during a short demo session.
    Returns (new_price, volume).
    """
    if random.random() < SHOCK_PROBABILITY:
        pct_move = random.uniform(-0.05, 0.05)
        volume = random.uniform(3_000_000, 8_000_000)
    else:
        pct_move = random.gauss(0, 0.004)
        volume = random.gauss(1_000_000, 150_000)

    new_price = max(0.01, last_price * (1 + pct_move))
    volume = max(1000.0, volume)
    return new_price, volume


def seed_symbol_from_quote(db: Session, sym: "models.Symbol", quote: dict, now: datetime):
    """
    Bootstrap a brand-new Symbol from a real quote (see market_data_provider.
    fetch_quote) rather than the generic 100.0 UNIVERSE placeholder. Mirrors
    what process_tick()'s is_first_tick branch does, but seeded with real
    numbers so a newly-added real-world ticker starts from an honest
    baseline instead of "everything is exactly the first tick."

    Rolling stats (price_std/volume_mean/etc.) are left at model defaults --
    they adapt within a handful of ticks via the same EMAs the simulated
    universe uses, so this doesn't need special-casing in detection.py.
    """
    price = quote["price"]
    sym.last_price = price
    sym.prev_close = quote.get("prev_close", price)
    sym.day_open = quote.get("open", price)
    sym.day_high = quote.get("high", price)
    sym.day_low = quote.get("low", price)
    sym.sma_short = price
    sym.sma_long = price
    active_info = market_data_provider.get_active_provider_info()
    sym.source = quote.get("source") or active_info.get("source", "live_alphavantage")
    sym.updated_at = now
    db.add(models.PriceTick(symbol=sym.symbol, price=price, timestamp=now))


def _prune_price_history(db: Session, symbols: list):
    """
    Keep only the most recent MAX_HISTORY_POINTS rows per symbol. Runs
    periodically (see PRUNE_EVERY_N_TICKS in run_feed_once) rather than
    every tick -- not worth paying this cost that often at this scale.
    Finds the id of the Nth-most-recent row and deletes anything older;
    if there are fewer than N rows yet, there's nothing to prune.
    """
    for sym in symbols:
        cutoff = (
            db.query(models.PriceTick.id)
            .filter(models.PriceTick.symbol == sym.symbol)
            .order_by(models.PriceTick.id.desc())
            .offset(MAX_HISTORY_POINTS - 1)
            .limit(1)
            .first()
        )
        if cutoff:
            db.query(models.PriceTick).filter(
                models.PriceTick.symbol == sym.symbol,
                models.PriceTick.id < cutoff[0],
            ).delete(synchronize_session=False)


def run_feed_once(db: Session):
    global _tick_counter
    now = datetime.now(timezone.utc)
    symbols = db.query(models.Symbol).all()
    has_live_key = market_data_provider.is_configured()

    active_info = market_data_provider.get_active_provider_info()
    default_source = active_info.get("source", "simulated")

    for sym in symbols:
        # If symbol has no baseline price yet, seed it from live provider
        if not sym.last_price or sym.last_price <= 0:
            try:
                live_quote = market_data_provider.fetch_quote(sym.symbol)
                if live_quote and live_quote.get("price"):
                    sym.last_price = float(live_quote["price"])
                    sym.prev_close = float(live_quote.get("prev_close") or sym.last_price)
                    sym.day_open = float(live_quote.get("open") or sym.last_price)
                    sym.day_high = float(live_quote.get("high") or sym.last_price)
                    sym.day_low = float(live_quote.get("low") or sym.last_price)
                    sym.source = live_quote.get("source", default_source)
            except Exception:
                pass

        seed_price = next((p for s, n, p in UNIVERSE if s == sym.symbol), 100.0)
        base_price = sym.last_price if (sym.last_price and sym.last_price > 0) else seed_price
        new_price, volume = generate_tick(base_price)

        # Maintain honest source tag (e.g. live_alphavantage)
        source_tag = sym.source if (sym.source and sym.source not in ("simulated", "live_quote")) else default_source

        result = process_tick(sym, new_price, volume, now)
        sym.source = source_tag
        sym.is_stale = False
        db.add(models.PriceTick(symbol=sym.symbol, price=new_price, timestamp=now))

        for evt in result.events:
            sym.event_seq_counter += 1
            db.add(models.SymbolEvent(
                symbol=sym.symbol,
                seq=sym.event_seq_counter,
                event_type=evt.event_type,
                score=evt.score,
                message=evt.message,
                price_at_event=new_price,
                timestamp=now,
            ))

    _tick_counter += 1
    if _tick_counter % PRUNE_EVERY_N_TICKS == 0:
        _prune_price_history(db, symbols)

    db.commit()


def _feed_loop(stop_event: threading.Event):
    while not stop_event.is_set():
        db = SessionLocal()
        try:
            run_feed_once(db)
        except Exception as e:  # feed must never crash the app; log and keep going
            print(f"[market_feed] tick error: {e}")
        finally:
            db.close()
        stop_event.wait(TICK_INTERVAL_SECONDS)


def start_feed_thread() -> threading.Event:
    db = SessionLocal()
    try:
        ensure_universe_seeded(db)
        # First pass just bootstraps prices (no events, see is_first_tick in detection.py)
        run_feed_once(db)
    finally:
        db.close()

    stop_event = threading.Event()
    thread = threading.Thread(target=_feed_loop, args=(stop_event,), daemon=True)
    thread.start()
    return stop_event
