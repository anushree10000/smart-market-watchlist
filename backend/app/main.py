import os
from datetime import datetime, timezone
from typing import List
from fastapi.middleware.cors import CORSMiddleware

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from . import models, schemas, security, market_feed, market_data_provider
from .database import engine, get_db
from .detection import freshness_status

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Smart Market Watchlist API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_feed_stop_event = None


@app.on_event("startup")
def on_startup():
    global _feed_stop_event
    _feed_stop_event = market_feed.start_feed_thread()


@app.on_event("shutdown")
def on_shutdown():
    if _feed_stop_event:
        _feed_stop_event.set()


# ---------------------------------------------------------------- auth ----

@app.post("/api/auth/register", response_model=schemas.Token)
def register(payload: schemas.UserCreate, db: Session = Depends(get_db)):
    if db.query(models.User).filter(models.User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="Username already taken")
    user = models.User(username=payload.username, password_hash=security.hash_password(payload.password))
    db.add(user)
    db.flush()  # assigns user.id within the same transaction, without committing yet
    # Give the new user a starter watchlist so the app isn't empty on first login.
    # Committed together with the user so a failure here can't leave a user with no watchlist.
    starter = models.Watchlist(user_id=user.id, name="My Watchlist")
    db.add(starter)
    db.commit()
    db.refresh(user)
    token = security.create_access_token({"sub": user.username})
    return schemas.Token(access_token=token)


@app.post("/api/auth/login", response_model=schemas.Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == form_data.username).first()
    if not user or not security.verify_password(form_data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    token = security.create_access_token({"sub": user.username})
    return schemas.Token(access_token=token)


@app.get("/api/market/status")
def market_status():
    return market_data_provider.get_active_provider_info()


# ----------------------------------------------------------- symbols ------

@app.get("/api/symbols", response_model=List[schemas.SymbolSnapshot])
def list_symbols(db: Session = Depends(get_db), _user: models.User = Depends(security.get_current_user)):
    return [_to_snapshot(s) for s in db.query(models.Symbol).order_by(models.Symbol.symbol).all()]


@app.get("/api/symbols/{symbol}/events", response_model=List[schemas.EventOut])
def symbol_events(symbol: str, since_seq: int = 0, db: Session = Depends(get_db),
                   _user: models.User = Depends(security.get_current_user)):
    sym = db.query(models.Symbol).filter(models.Symbol.symbol == symbol.upper()).first()
    if not sym:
        raise HTTPException(status_code=404, detail="Unknown symbol")
    events = (
        db.query(models.SymbolEvent)
        .filter(models.SymbolEvent.symbol == symbol.upper(), models.SymbolEvent.seq > since_seq)
        .order_by(models.SymbolEvent.seq.desc())
        .limit(100)
        .all()
    )
    return events


@app.get("/api/symbols/{symbol}/history", response_model=List[schemas.PricePoint])
def symbol_history(symbol: str, limit: int = 300, db: Session = Depends(get_db),
                    _user: models.User = Depends(security.get_current_user)):
    """
    Chart data. Sourced from our own recorded ticks (see PriceTick in
    models.py), not a vendor historical-candle endpoint -- Finnhub's free
    tier doesn't include one for US stocks. Returned oldest-first, since
    that's the order a chart wants to plot in.
    """
    sym = db.query(models.Symbol).filter(models.Symbol.symbol == symbol.upper()).first()
    if not sym:
        raise HTTPException(status_code=404, detail="Unknown symbol")
    points = (
        db.query(models.PriceTick)
        .filter(models.PriceTick.symbol == symbol.upper())
        .order_by(models.PriceTick.id.desc())
        .limit(limit)
        .all()
    )
    points.reverse()
    return points


@app.get("/api/symbols/search", response_model=List[schemas.SymbolSearchResult])
def search_symbols(q: str, _user: models.User = Depends(security.get_current_user)):
    """
    Live ticker lookup for the add-symbol search box. Falls back to a local
    substring match over the simulated universe if no provider key is
    configured or the live call fails -- see market_data_provider for why
    that's a fallback here but a hard error for quote lookups in add_item.
    """
    return market_data_provider.search_symbols(q)


# --------------------------------------------------------- watchlists -----

@app.get("/api/watchlists", response_model=List[schemas.WatchlistOut])
def list_watchlists(db: Session = Depends(get_db), user: models.User = Depends(security.get_current_user)):
    wls = db.query(models.Watchlist).filter(models.Watchlist.user_id == user.id).all()
    out = []
    for w in wls:
        out.append(schemas.WatchlistOut(
            id=w.id, name=w.name, created_at=w.created_at,
            symbols=[i.symbol for i in w.items],
        ))
    return out


@app.post("/api/watchlists", response_model=schemas.WatchlistOut)
def create_watchlist(payload: schemas.WatchlistCreate, db: Session = Depends(get_db),
                      user: models.User = Depends(security.get_current_user)):
    w = models.Watchlist(user_id=user.id, name=payload.name)
    db.add(w)
    db.commit()
    db.refresh(w)
    return schemas.WatchlistOut(id=w.id, name=w.name, created_at=w.created_at, symbols=[])


@app.delete("/api/watchlists/{watchlist_id}")
def delete_watchlist(watchlist_id: int, db: Session = Depends(get_db),
                      user: models.User = Depends(security.get_current_user)):
    w = _get_owned_watchlist(db, watchlist_id, user)
    db.delete(w)
    db.commit()
    return {"ok": True}


@app.post("/api/watchlists/{watchlist_id}/items")
def add_item(watchlist_id: int, payload: schemas.ItemCreate, db: Session = Depends(get_db),
             user: models.User = Depends(security.get_current_user)):
    w = _get_owned_watchlist(db, watchlist_id, user)
    symbol = payload.symbol.upper().strip()

    sym = db.query(models.Symbol).filter(models.Symbol.symbol == symbol).first()
    if not sym:
        # Not a symbol we're tracking yet -- try to onboard it via a real quote
        # rather than rejecting outright. Unlike search, this has no fallback:
        # if we can't get a genuine starting price, we say so rather than
        # inventing one that would silently corrupt the detection engine.
        quote = market_data_provider.fetch_quote(symbol)
        if not quote:
            detail = (
                f"Couldn't find a live quote for '{symbol}'."
                if market_data_provider.is_configured()
                else f"Unknown symbol '{symbol}' (live lookup isn't configured on this server)."
            )
            raise HTTPException(status_code=404, detail=detail)
        sym = models.Symbol(
            symbol=symbol,
            name=(payload.name or symbol).strip(),
            source=quote.get("source", "live_alphavantage"),
        )
        db.add(sym)
        db.flush()
        market_feed.seed_symbol_from_quote(db, sym, quote, datetime.now(timezone.utc))

    exists = db.query(models.WatchlistItem).filter(
        models.WatchlistItem.watchlist_id == w.id, models.WatchlistItem.symbol == symbol
    ).first()
    if exists:
        raise HTTPException(status_code=409, detail="Symbol already in this watchlist")
    db.add(models.WatchlistItem(watchlist_id=w.id, symbol=symbol))
    db.commit()
    return {"ok": True}


@app.delete("/api/watchlists/{watchlist_id}/items/{symbol}")
def remove_item(watchlist_id: int, symbol: str, db: Session = Depends(get_db),
                 user: models.User = Depends(security.get_current_user)):
    w = _get_owned_watchlist(db, watchlist_id, user)
    item = db.query(models.WatchlistItem).filter(
        models.WatchlistItem.watchlist_id == w.id, models.WatchlistItem.symbol == symbol.upper()
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Symbol not in this watchlist")
    db.delete(item)
    db.commit()
    return {"ok": True}


@app.get("/api/watchlists/{watchlist_id}/state", response_model=schemas.WatchlistStateOut)
def watchlist_state(watchlist_id: int, db: Session = Depends(get_db),
                     user: models.User = Depends(security.get_current_user)):
    """
    The core "since you last checked" endpoint -- READ-ONLY / non-mutating.

    For each symbol in the watchlist, this computes "everything with seq > your
    cursor" and returns it, WITHOUT moving the cursor. That's deliberate: the
    frontend polls this every few seconds to keep prices live, and if polling
    silently marked things as seen, "since you last checked" would collapse
    into "since 5 seconds ago" for anyone who leaves the tab open -- which
    defeats the entire point of the feature.

    The cursor only advances via the explicit ack endpoints below, which fire
    when the user actually reviews a symbol (opens its detail) or explicitly
    dismisses the "since you last checked" panel. That's what makes "checked"
    mean something a human did, not something a timer did.
    """
    w = _get_owned_watchlist(db, watchlist_id, user)
    items_out = []

    for item in w.items:
        sym = db.query(models.Symbol).filter(models.Symbol.symbol == item.symbol).first()
        if not sym:
            continue

        cursor = db.query(models.UserWatchState).filter(
            models.UserWatchState.user_id == user.id, models.UserWatchState.symbol == sym.symbol
        ).first()
        last_seen_seq = cursor.last_seen_seq if cursor else 0

        new_events = (
            db.query(models.SymbolEvent)
            .filter(models.SymbolEvent.symbol == sym.symbol, models.SymbolEvent.seq > last_seen_seq)
            .order_by(models.SymbolEvent.seq.desc())
            .all()
        )

        meaning_score = min(100.0, sum(e.score for e in new_events))

        items_out.append(schemas.WatchlistSymbolState(
            snapshot=_to_snapshot(sym),
            meaning_score=round(meaning_score, 1),
            new_events=[schemas.EventOut.model_validate(e) for e in new_events],
            unseen_count=len(new_events),
        ))

    # Most-changed-first: this is the whole point -- don't make traders scan alphabetically.
    items_out.sort(key=lambda x: x.meaning_score, reverse=True)

    return schemas.WatchlistStateOut(
        watchlist_id=w.id, name=w.name, generated_at=datetime.now(timezone.utc), items=items_out
    )


def _advance_cursor(db: Session, user_id: int, symbol: str):
    sym = db.query(models.Symbol).filter(models.Symbol.symbol == symbol).first()
    if not sym:
        return
    cursor = db.query(models.UserWatchState).filter(
        models.UserWatchState.user_id == user_id, models.UserWatchState.symbol == symbol
    ).first()
    if not cursor:
        cursor = models.UserWatchState(user_id=user_id, symbol=symbol, last_seen_seq=0)
        db.add(cursor)
    cursor.last_seen_seq = sym.event_seq_counter
    cursor.last_seen_at = datetime.now(timezone.utc)


@app.post("/api/watchlists/{watchlist_id}/symbols/{symbol}/ack")
def ack_symbol(watchlist_id: int, symbol: str, db: Session = Depends(get_db),
               user: models.User = Depends(security.get_current_user)):
    """Mark one symbol's events as reviewed -- fires when the user opens its detail view."""
    _get_owned_watchlist(db, watchlist_id, user)  # ownership check
    _advance_cursor(db, user.id, symbol.upper())
    db.commit()
    return {"ok": True}


@app.post("/api/watchlists/{watchlist_id}/ack-all")
def ack_all(watchlist_id: int, db: Session = Depends(get_db),
            user: models.User = Depends(security.get_current_user)):
    """Mark every symbol in this watchlist as reviewed -- the 'mark all as read' action."""
    w = _get_owned_watchlist(db, watchlist_id, user)
    for item in w.items:
        _advance_cursor(db, user.id, item.symbol)
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------- helpers --

def _get_owned_watchlist(db: Session, watchlist_id: int, user: models.User) -> models.Watchlist:
    w = db.query(models.Watchlist).filter(
        models.Watchlist.id == watchlist_id, models.Watchlist.user_id == user.id
    ).first()
    if not w:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return w


def _to_snapshot(sym: models.Symbol) -> schemas.SymbolSnapshot:
    change_pct = ((sym.last_price - sym.prev_close) / sym.prev_close * 100) if sym.prev_close else 0.0
    rel_vol = (sym.last_volume / sym.volume_mean) if sym.volume_mean else 1.0
    return schemas.SymbolSnapshot(
        symbol=sym.symbol,
        name=sym.name,
        last_price=round(sym.last_price, 2),
        prev_close=round(sym.prev_close, 2),
        change_pct=round(change_pct, 2),
        day_high=round(sym.day_high, 2),
        day_low=round(sym.day_low, 2),
        last_volume=round(sym.last_volume),
        volume_mean=round(sym.volume_mean),
        rel_volume=round(rel_vol, 2),
        freshness=freshness_status(sym.updated_at),
        source=getattr(sym, "source", "simulated"),
        updated_at=sym.updated_at,
    )


# -------------------------------------------------------- static frontend --
frontend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend"))
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

