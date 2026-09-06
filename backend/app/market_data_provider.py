"""
Live market data provider: Yahoo Finance (Keyless Live Quotes), Alpha Vantage, and Finnhub.

Multi-Provider Architecture:
  1. Yahoo Finance (Default & Active out of the box - Zero API key needed!):
     - Real-time stock quotes (regularMarketPrice, high, low, open, prev_close, volume).
     - Global symbol search suggestions (US equities, Indian NSE/BSE, Crypto, ETFs).
     - No rate-limit friction or registration required.
  2. Alpha Vantage (Optional):
     - Activated if ALPHA_VANTAGE_API_KEY is configured in backend/.env.
  3. Finnhub (Optional):
     - Activated if FINNHUB_API_KEY or MARKET_DATA_API_KEY is configured.
  4. Local Universe (Graceful offline/fallback).
"""
import os
import time
from pathlib import Path
import requests
from typing import Optional

DEFAULT_UNIVERSE = [
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

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
REQUEST_TIMEOUT_SECONDS = 10.0
CACHE_TTL_SECONDS = 60


_search_cache: dict[str, tuple[float, list[dict]]] = {}
_quote_cache: dict[str, tuple[float, Optional[dict]]] = {}


def _load_env_file():
    """Load key-value pairs from .env if present."""
    candidates = [
        Path(__file__).resolve().parent.parent / ".env",
        Path(__file__).resolve().parent.parent.parent / ".env",
        Path.cwd() / ".env",
        Path.cwd() / "backend" / ".env",
        Path(r"d:\smart-market-watchlist-fixed (3)\backend\.env"),
    ]
    for env_path in candidates:
        if env_path.exists():
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k, v = k.strip(), v.strip().strip("'\"")
                            if k and v:
                                os.environ[k] = v
            except Exception:
                pass


_load_env_file()


def get_alpha_vantage_key() -> str:
    _load_env_file()
    key = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()
    return key if key and "your_" not in key else ""


def get_finnhub_key() -> str:
    _load_env_file()
    key = os.environ.get("FINNHUB_API_KEY", os.environ.get("MARKET_DATA_API_KEY", "")).strip()
    return key if key and "your_" not in key else ""


def get_active_provider_info() -> dict:
    _load_env_file()
    av_key = get_alpha_vantage_key()
    if av_key:
        return {
            "provider": "Alpha Vantage",
            "source": "live_alphavantage",
            "badge": "Live Market (Alpha Vantage)",
            "is_live": True,
        }
    fh_key = get_finnhub_key()
    if fh_key:
        return {
            "provider": "Finnhub",
            "source": "live_finnhub",
            "badge": "Live Market (Finnhub)",
            "is_live": True,
        }
    return {
        "provider": "Yahoo Finance",
        "source": "live_yahoo",
        "badge": "Live Market (Yahoo Finance)",
        "is_live": True,
    }


def _local_universe_search(query: str, limit: int) -> list[dict]:
    q = query.strip().upper()
    if not q:
        return []
    matches = [
        {"symbol": s, "name": n, "source": "local"}
        for s, n, _ in DEFAULT_UNIVERSE
        if q in s or q in n.upper()
    ]
    return matches[:limit]



# ---------------------------------------------------- Yahoo Finance Engine ----

def _fetch_yahoo_quote(symbol: str) -> Optional[dict]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return None
        data = resp.json()
        result_list = data.get("chart", {}).get("result")
        if not result_list:
            return None
        meta = result_list[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        if price is None or float(price) <= 0:
            return None

        price = float(price)
        prev_close = float(meta.get("chartPreviousClose") or meta.get("previousClose") or price)
        day_open = float(meta.get("regularMarketOpen") or price)
        day_high = float(meta.get("regularMarketDayHigh") or price)
        day_low = float(meta.get("regularMarketDayLow") or price)
        volume = float(meta.get("regularMarketVolume") or 1_000_000.0)

        return {
            "price": price,
            "open": day_open,
            "high": day_high,
            "low": day_low,
            "prev_close": prev_close,
            "volume": volume,
            "source": "live_yahoo",
        }
    except Exception as e:
        return None


def _search_yahoo(query: str, limit: int = 8) -> list[dict]:
    url = f"https://query2.finance.yahoo.com/v1/finance/search?q={query}&quotesCount={limit}&newsCount=0"
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return []
        data = resp.json()
        quotes = data.get("quotes", [])
        results = []
        for q in quotes:
            sym = q.get("symbol")
            if not sym or "^" in sym:
                continue
            name = q.get("longname") or q.get("shortname") or sym
            results.append({"symbol": sym, "name": name, "source": "live"})
        return results[:limit]
    except Exception:
        return []


def _fetch_alpha_vantage_quote(symbol: str, api_key: str) -> Optional[dict]:
    url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={symbol}&apikey={api_key}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        data = resp.json().get("Global Quote", {})
        price = data.get("05. price")
        if not price or float(price) <= 0:
            return None
        return {
            "price": float(price),
            "open": float(data.get("02. open") or price),
            "high": float(data.get("03. high") or price),
            "low": float(data.get("04. low") or price),
            "prev_close": float(data.get("08. previous close") or price),
            "volume": float(data.get("06. volume") or 1_000_000.0),
            "source": "live_alphavantage",
        }
    except Exception:
        return None


def _search_alpha_vantage(query: str, api_key: str, limit: int = 8) -> list[dict]:
    url = f"https://www.alphavantage.co/query?function=SYMBOL_SEARCH&keywords={query}&apikey={api_key}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        matches = resp.json().get("bestMatches", [])
        results = []
        for m in matches:
            sym = m.get("1. symbol")
            name = m.get("2. name", sym)
            if sym:
                results.append({"symbol": sym, "name": name, "source": "live_alphavantage"})
        return results[:limit]
    except Exception:
        return []




# --------------------------------------------------------- Finnhub Engine ----

def _fetch_finnhub_quote(symbol: str, api_key: str) -> Optional[dict]:
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={api_key}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        data = resp.json()
        price = data.get("c")
        if not price or float(price) <= 0:
            return None
        return {
            "price": float(price),
            "open": float(data.get("o") or price),
            "high": float(data.get("h") or price),
            "low": float(data.get("l") or price),
            "prev_close": float(data.get("pc") or price),
            "volume": 1_000_000.0,
            "source": "live_finnhub",
        }
    except Exception:
        return None


# ------------------------------------------------------- Public Interface ----

def search_symbols(query: str, limit: int = 8) -> list[dict]:
    """
    Search symbols across live market feeds with graceful local fallback.
    Prioritizes Alpha Vantage when configured.
    """
    query = query.strip()
    if len(query) < 1:
        return []

    cache_key = f"{query.upper()}:{limit}"
    cached = _search_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    results = []

    # 1. If Alpha Vantage key is present, prioritize Alpha Vantage!
    av_key = get_alpha_vantage_key()
    if av_key:
        try:
            results = _search_alpha_vantage(query, av_key, limit)
        except Exception:
            results = []

    # 2. Try Yahoo Finance live search (fast & zero-rate-limit fallback)
    if not results:
        try:
            results = _search_yahoo(query, limit)
        except Exception:
            results = []

    # 3. If Finnhub key is present and previous returned empty, try Finnhub
    if not results:
        fh_key = get_finnhub_key()
        if fh_key:
            try:
                resp = requests.get(
                    f"https://finnhub.io/api/v1/search?q={query}&token={fh_key}",
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                data = resp.json()
                results = [
                    {"symbol": item["symbol"], "name": item.get("description", item["symbol"]), "source": "live"}
                    for item in data.get("result", [])
                    if item.get("type") in ("Common Stock", "ETP", "ETF", "")
                ][:limit]
            except Exception:
                pass

    # 4. Fallback to local demo universe if network failed
    if not results:
        results = _local_universe_search(query, limit)

    _search_cache[cache_key] = (time.time(), results)
    return results



def fetch_quote(symbol: str) -> Optional[dict]:
    """
    Returns live {price, open, high, low, prev_close, volume, source} quote.
    """
    symbol = symbol.strip().upper()
    cached = _quote_cache.get(symbol)
    if cached and (time.time() - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    result = None

    # Priority 1: Alpha Vantage (if configured)
    av_key = get_alpha_vantage_key()
    if av_key:
        result = _fetch_alpha_vantage_quote(symbol, av_key)

    # Priority 2: Yahoo Finance (Live real-time, No Key Needed)
    if not result:
        result = _fetch_yahoo_quote(symbol)

    # Priority 3: Finnhub (if configured)
    if not result:
        fh_key = get_finnhub_key()
        if fh_key:
            result = _fetch_finnhub_quote(symbol, fh_key)


    _quote_cache[symbol] = (time.time(), result)
    return result


def is_configured() -> bool:
    """Always true now since Yahoo Finance provides zero-config live data!"""
    return True


