"""
Meaningful-change detection engine.

Design decision: instead of fixed global thresholds ("3% move is meaningful"),
every signal is scored as a Z-SCORE against that SPECIFIC symbol's own recent
behavior. A 3% move is noise for a stock that swings 5% daily and huge for
one that swings 0.3% daily. This makes the "meaning score" comparable across
very different instruments, which fixed thresholds can't do.

Rolling statistics are kept as exponential moving averages on the Symbol row
(see models.py) rather than a stored tick window, so this is O(1) per tick
regardless of symbol count or how far back "rolling" means -- the design
choice that lets this run continuously for a large universe of symbols
without a separate time-series datastore.

This module is pure logic: given the previous Symbol state and a new tick,
it returns (updated fields, list of detected events). It has no knowledge
of HTTP, the DB session, or the feed source -- that separation is what makes
it trivially unit-testable and swappable onto a real market data feed later.
"""
from dataclasses import dataclass, field
from typing import List
from datetime import datetime, timezone

# Tunable thresholds -- exposed at the top so they're easy to reason about
# and to eventually make per-user or per-trader-profile configurable.
PRICE_Z_THRESHOLD = 2.0
VOLUME_Z_THRESHOLD = 2.0
EMA_ALPHA_RETURN = 0.06      # how fast the "normal" return distribution adapts
EMA_ALPHA_VOLUME = 0.06
EMA_ALPHA_SMA_SHORT = 0.25   # ~ fast moving average
EMA_ALPHA_SMA_LONG = 0.05    # ~ slow moving average
MIN_PRICE_STD = 0.0015       # floor so a dead-quiet stock doesn't produce infinite z-scores
MIN_VOLUME_STD = 50_000.0


@dataclass
class DetectedEvent:
    event_type: str
    score: float
    message: str


@dataclass
class DetectionResult:
    events: List[DetectedEvent] = field(default_factory=list)
    meaning_score: float = 0.0


def _ema(old: float, new: float, alpha: float) -> float:
    return old + alpha * (new - old)


def process_tick(sym, price: float, volume: float, ts: datetime) -> DetectionResult:
    """
    Mutates `sym` (a models.Symbol instance) in place with updated rolling
    stats, and returns the events detected on this tick. Caller is
    responsible for committing `sym` and persisting the returned events as
    SymbolEvent rows with the next sequence number(s).
    """
    result = DetectionResult()
    is_first_tick = sym.last_price in (None, 0.0)

    if is_first_tick:
        # Bootstrap -- nothing to compare against yet.
        sym.last_price = price
        sym.prev_close = price
        sym.day_open = price
        sym.day_high = price
        sym.day_low = price
        sym.sma_short = price
        sym.sma_long = price
        sym.last_volume = volume
        sym.volume_mean = volume
        sym.updated_at = ts
        return result

    prev_price = sym.last_price
    ret = (price - prev_price) / prev_price if prev_price else 0.0

    # --- price z-score against this symbol's own recent return distribution ---
    z_price = ret / max(sym.price_std, MIN_PRICE_STD)
    sym.price_mean = _ema(sym.price_mean, ret, EMA_ALPHA_RETURN)
    # dispersion tracked via EMA of |deviation|, scaled to approximate a std dev
    dev = abs(ret - sym.price_mean)
    sym.price_std = max(_ema(sym.price_std, dev * 1.25, EMA_ALPHA_RETURN), MIN_PRICE_STD)

    if abs(z_price) >= PRICE_Z_THRESHOLD:
        direction = "up" if ret > 0 else "down"
        pct = ret * 100
        score = min(45, 15 * abs(z_price))
        result.events.append(DetectedEvent(
            "price_move", score,
            f"Moved {direction} {pct:+.2f}% -- {abs(z_price):.1f}x its typical move for this symbol."
        ))

    # --- volume z-score against this symbol's own recent volume distribution ---
    z_volume = (volume - sym.volume_mean) / max(sym.volume_std, MIN_VOLUME_STD)
    sym.volume_mean = _ema(sym.volume_mean, volume, EMA_ALPHA_VOLUME)
    vol_dev = abs(volume - sym.volume_mean)
    sym.volume_std = max(_ema(sym.volume_std, vol_dev * 1.25, EMA_ALPHA_VOLUME), MIN_VOLUME_STD)

    if z_volume >= VOLUME_Z_THRESHOLD:
        rel_vol = volume / sym.volume_mean if sym.volume_mean else 1.0
        score = min(30, 10 * z_volume)
        result.events.append(DetectedEvent(
            "volume_spike", score,
            f"Volume running {rel_vol:.1f}x its recent average ({z_volume:.1f} std devs above normal)."
        ))

    # --- moving average cross (fast EMA crossing slow EMA) ---
    # A deadband around zero separation avoids flagging float-noise whipsaws
    # as trend changes -- only a separation exceeding ~0.15% of price counts
    # as a genuine sign, so the sign can only flip on a real divergence.
    sym.sma_short = _ema(sym.sma_short, price, EMA_ALPHA_SMA_SHORT)
    sym.sma_long = _ema(sym.sma_long, price, EMA_ALPHA_SMA_LONG)
    separation = (sym.sma_short - sym.sma_long) / price if price else 0.0
    deadband = 0.0015
    if separation > deadband:
        new_sign = 1
    elif separation < -deadband:
        new_sign = -1
    else:
        new_sign = sym.prev_cross_sign  # inside the deadband -- hold previous sign, don't flicker

    if sym.prev_cross_sign != 0 and new_sign != 0 and new_sign != sym.prev_cross_sign:
        direction = "bullish" if new_sign > 0 else "bearish"
        result.events.append(DetectedEvent(
            "ma_cross", 20,
            f"Short-term trend crossed {direction} against the longer-term trend."
        ))
    sym.prev_cross_sign = new_sign

    # --- new day high / low ---
    if price > sym.day_high:
        sym.day_high = price
        result.events.append(DetectedEvent("new_high", 12, "Set a new high for the session."))
    if price < sym.day_low:
        sym.day_low = price
        result.events.append(DetectedEvent("new_low", 12, "Set a new low for the session."))

    sym.last_price = price
    sym.last_volume = volume
    sym.updated_at = ts

    result.meaning_score = min(100.0, sum(e.score for e in result.events))
    return result


def freshness_status(updated_at: datetime, now: datetime = None) -> str:
    """
    Data freshness classification. In this demo the only source is the
    simulated feed, so "stale" mainly triggers if the background feed loop
    itself stalls -- but the same three-tier classification is exactly what
    you'd want with a real vendor feed that can silently stop pushing ticks.
    """
    now = now or datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    age = (now - updated_at).total_seconds()
    if age <= 8:
        return "live"
    if age <= 30:
        return "delayed"
    return "stale"
