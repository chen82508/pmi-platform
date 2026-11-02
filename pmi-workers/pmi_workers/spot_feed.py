"""Best-effort external spot price for a bull/bear index, from public exchange
tickers with a FALLBACK CHAIN.

Called ONLY from the snapshot cron (every ~10 min), never on the request path —
the result is baked into the cached board payload, so page requests make zero
external calls. Sources are tried in order; the first in-band value wins, so one
exchange being down/rate-limited never blocks the others. If EVERY source fails
the function returns None and the caller falls back to the ladder-implied spot,
so a dead API can never blank the board.

Kept in pmi-workers (not pmi-core) on purpose: pmi-core's compute stays free of
network I/O; the worker fetches the spot and passes it in as `spot_override`.
"""

from __future__ import annotations

import httpx
import structlog

log = structlog.get_logger("pmi_workers.spot_feed")

_TIMEOUT = 4.0  # seconds per source — fail fast, move to the next


async def _binance(c: httpx.AsyncClient) -> float:
    r = await c.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"})
    r.raise_for_status()
    return float(r.json()["price"])


async def _okx(c: httpx.AsyncClient) -> float:
    r = await c.get("https://www.okx.com/api/v5/market/ticker", params={"instId": "BTC-USDT"})
    r.raise_for_status()
    return float(r.json()["data"][0]["last"])


async def _coinbase(c: httpx.AsyncClient) -> float:
    r = await c.get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
    r.raise_for_status()
    return float(r.json()["data"]["amount"])


async def _kraken(c: httpx.AsyncClient) -> float:
    r = await c.get("https://api.kraken.com/0/public/Ticker", params={"pair": "XBTUSD"})
    r.raise_for_status()
    d = r.json()["result"]
    return float(d[next(iter(d))]["c"][0])  # ["c"] = last-trade [price, lot]


# (name, coro). Order = preference; all keyless public spot tickers. OKX/
# Coinbase/Kraken are reachable from the us-west EC2; Binance geo-blocks that
# region (451), so it sits LAST as a long-shot rather than a wasted first hop.
_BTC_SOURCES = [
    ("okx", _okx),
    ("coinbase", _coinbase),
    ("kraken", _kraken),
    ("binance", _binance),
]

# Reject an obviously-wrong tick (fat-finger, wrong pair, HTML error body parsed
# as a number). Wide on purpose — this only guards against nonsense, not drift.
_BTC_MIN, _BTC_MAX = 1_000.0, 10_000_000.0


async def fetch_btc_spot() -> float | None:
    """Current BTC/USD spot from the first healthy exchange, or None if all fail."""
    return await _first_healthy("btc", _BTC_SOURCES, _BTC_MIN, _BTC_MAX)


# ── S&P 500 index level (SPX) ──────────────────────────────────────────────
# Keyless index quotes are scarcer than crypto tickers; Yahoo's chart API is the
# reliable one, so we hit both of its hosts, then Stooq best-effort. The S&P
# range-bin anchors are SPX-scale (~7000s), so this spot lets their E[price]
# distribution reading land on the bull/bear axis. If all fail → None → the
# distribution anchors stay "—" (graceful, same as before).
_YH = "/v8/finance/chart/%5EGSPC?range=1d&interval=1d"
_UA = {"User-Agent": "Mozilla/5.0"}


def _yahoo(host: str):
    async def fn(c: httpx.AsyncClient) -> float:
        r = await c.get(f"https://{host}.finance.yahoo.com{_YH}", headers=_UA)
        r.raise_for_status()
        return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])

    return fn


async def _stooq(c: httpx.AsyncClient) -> float:
    r = await c.get("https://stooq.com/q/l/?s=^spx&f=c&e=csv", headers=_UA)
    r.raise_for_status()
    return float(r.text.strip().splitlines()[-1])


_SPX_SOURCES = [
    ("yahoo-q1", _yahoo("query1")),
    ("yahoo-q2", _yahoo("query2")),
    ("stooq", _stooq),
]
_SPX_MIN, _SPX_MAX = 1_000.0, 100_000.0


async def fetch_sp500_spot() -> float | None:
    """Current S&P 500 (SPX) index level from the first healthy source, else None."""
    return await _first_healthy("sp500", _SPX_SOURCES, _SPX_MIN, _SPX_MAX)


async def _first_healthy(label, sources, lo, hi) -> float | None:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        for name, fn in sources:
            try:
                v = await fn(c)
            except Exception as e:  # network / parse / rate-limit → try next
                log.warning("spot_feed.source_failed", label=label, source=name, error=str(e))
                continue
            if v and lo < v < hi:
                log.info("spot_feed.ok", label=label, source=name, spot=v)
                return v
            log.warning("spot_feed.out_of_band", label=label, source=name, value=v)
    log.warning("spot_feed.all_failed", label=label)
    return None
