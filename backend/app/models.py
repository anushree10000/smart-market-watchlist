"""
Schema design notes
--------------------

The core idea: market truth is computed ONCE per symbol, independent of how
many users watch it. "Did NVDA break out" is an objective fact about NVDA,
not about any particular user. So detection writes to `symbol_events`
(an append-only, per-symbol sequence log) exactly once per real event,
no matter how many watchlists contain that symbol.

"What changed since I last checked" then becomes a per-user, per-symbol
cursor (`user_watch_state.last_seen_seq`) into that shared log — cheap to
store, trivially consistent across devices (any device just asks "what's
newer than sequence N"), and it gives us a full activity timeline for free
without any extra bookkeeping.

This turns the scaling problem from (users x symbols) into (unique symbols)
for the expensive part (detection), with a cheap fan-out/diff step per user.
"""
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, UniqueConstraint, Text, Boolean
)
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from .database import Base


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=utcnow)

    watchlists = relationship("Watchlist", back_populates="owner", cascade="all, delete-orphan")


class Watchlist(Base):
    __tablename__ = "watchlists"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=utcnow)

    owner = relationship("User", back_populates="watchlists")
    items = relationship("WatchlistItem", back_populates="watchlist", cascade="all, delete-orphan")


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    id = Column(Integer, primary_key=True)
    watchlist_id = Column(Integer, ForeignKey("watchlists.id"), nullable=False)
    symbol = Column(String, ForeignKey("symbols.symbol"), nullable=False)
    added_at = Column(DateTime, default=utcnow)

    watchlist = relationship("Watchlist", back_populates="items")

    __table_args__ = (UniqueConstraint("watchlist_id", "symbol", name="uq_watchlist_symbol"),)


class Symbol(Base):
    """
    Current-state cache for a symbol, updated in place on every tick.

    Rolling statistics (price_mean/price_std, volume_mean/volume_std,
    sma_short/sma_long) are maintained as EXPONENTIAL MOVING AVERAGES rather
    than by storing a full tick history and recomputing. This is an O(1)
    memory/compute update per tick regardless of how far back "rolling"
    conceptually goes — the design choice that makes per-symbol detection
    cheap enough to run continuously for thousands of symbols without a
    separate time-series store for the MVP.
    """
    __tablename__ = "symbols"

    symbol = Column(String, primary_key=True)
    name = Column(String, default="")

    last_price = Column(Float, default=0.0)
    prev_close = Column(Float, default=0.0)
    day_open = Column(Float, default=0.0)
    day_high = Column(Float, default=0.0)
    day_low = Column(Float, default=0.0)

    last_volume = Column(Float, default=0.0)
    volume_mean = Column(Float, default=1_000_000.0)
    volume_std = Column(Float, default=200_000.0)

    price_mean = Column(Float, default=0.0)      # EMA of returns
    price_std = Column(Float, default=0.002)     # EMA of |return| dispersion, floor to avoid div/0

    sma_short = Column(Float, default=0.0)        # fast EMA of price
    sma_long = Column(Float, default=0.0)         # slow EMA of price
    prev_cross_sign = Column(Integer, default=0)  # -1, 0, +1 -> sign(sma_short - sma_long) last tick

    source = Column(String, default="simulated")   # which feed provided the last tick
    is_stale = Column(Boolean, default=False)

    event_seq_counter = Column(Integer, default=0)  # monotonic per-symbol sequence for symbol_events
    updated_at = Column(DateTime, default=utcnow)


class SymbolEvent(Base):
    """
    Append-only log of detected, meaningful events for a symbol.
    `seq` is monotonic PER SYMBOL (not global) so a user's cursor
    ("I've seen up to seq 42 for NVDA") is meaningful and stable.
    """
    __tablename__ = "symbol_events"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, ForeignKey("symbols.symbol"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    event_type = Column(String, nullable=False)   # price_move | volume_spike | ma_cross | new_high | new_low | gap
    score = Column(Float, default=0.0)             # contribution to meaning score, 0-100
    message = Column(Text)
    price_at_event = Column(Float)
    timestamp = Column(DateTime, default=utcnow, index=True)

    __table_args__ = (UniqueConstraint("symbol", "seq", name="uq_symbol_seq"),)


class PriceTick(Base):
    """
    Price history for charting. Deliberately NOT sourced from a vendor's
    historical-candle endpoint -- Finnhub's free tier doesn't include one
    (US stock candles are premium-only and 403 on a free key). Since the
    feed loop already produces a real price every 3 seconds for every
    symbol it's tracking, that IS the history: no extra API dependency,
    works identically for the simulated universe and real onboarded
    tickers, and never breaks on an API tier change.

    Capped per symbol (see market_feed.MAX_HISTORY_POINTS) rather than kept
    forever -- this is chart data for "recent session," not a permanent
    OHLC archive, so unbounded growth isn't worth the disk/query cost.
    """
    __tablename__ = "price_ticks"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, ForeignKey("symbols.symbol"), nullable=False, index=True)
    price = Column(Float, nullable=False)
    timestamp = Column(DateTime, default=utcnow, index=True)


class UserWatchState(Base):
    """
    Per-user, per-symbol read cursor into `symbol_events`.
    This is the ENTIRE mechanism behind "since you last checked" and it is
    naturally multi-device consistent: whichever device asks first sees
    everything newer than last_seen_seq, then advances the cursor.
    """
    __tablename__ = "user_watch_state"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    symbol = Column(String, ForeignKey("symbols.symbol"), nullable=False)
    last_seen_seq = Column(Integer, default=0)
    last_seen_at = Column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_user_symbol_state"),)
