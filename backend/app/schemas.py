from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    # Capped at 72: bcrypt's hard input limit. A longer password reaching
    # hash_password() raises ValueError inside the bcrypt backend (an
    # unhandled 500 on register) rather than failing with a clear message.
    password: str = Field(min_length=6, max_length=72)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class WatchlistCreate(BaseModel):
    name: str


class WatchlistOut(BaseModel):
    id: int
    name: str
    created_at: datetime
    symbols: List[str] = []

    class Config:
        from_attributes = True


class ItemCreate(BaseModel):
    symbol: str
    name: Optional[str] = None  # from a search result, for symbols not yet tracked locally


class SymbolSearchResult(BaseModel):
    symbol: str
    name: str
    source: str  # "live" | "local" -- honest about whether this came from the vendor or the fallback


class SymbolSnapshot(BaseModel):
    symbol: str
    name: str
    last_price: float
    prev_close: float
    change_pct: float
    day_high: float
    day_low: float
    last_volume: float
    volume_mean: float
    rel_volume: float
    freshness: str        # "live" | "delayed" | "stale"
    source: Optional[str] = "simulated"  # "live_finnhub" | "live_quote" | "simulated"
    updated_at: datetime



class EventOut(BaseModel):
    seq: int
    event_type: str
    score: float
    message: str
    price_at_event: float
    timestamp: datetime

    class Config:
        from_attributes = True


class PricePoint(BaseModel):
    price: float
    timestamp: datetime

    class Config:
        from_attributes = True


class WatchlistSymbolState(BaseModel):
    snapshot: SymbolSnapshot
    meaning_score: float
    new_events: List[EventOut] = []   # events since this user last checked THIS symbol
    unseen_count: int = 0


class WatchlistStateOut(BaseModel):
    watchlist_id: int
    name: str
    generated_at: datetime
    items: List[WatchlistSymbolState]
