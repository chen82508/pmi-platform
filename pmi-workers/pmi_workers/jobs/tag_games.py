"""tag-games — cache each sports market's canonical identity into core_market_game_tag.

The sports analogue of `tag-races`: an LLM classifies every candidate sports
market ONCE (sport / league / market_class / teams / event / market_type / line)
and the result is persisted, so the sports snapshot groups + collapses a game or
outright across venues with a plain WHERE — no per-request LLM, no per-request
compute. Incremental + cached: only markets not already tagged at the current
CLASSIFIER_VERSION are sent to the LLM; a prompt change bumps the version and
re-tags. See engine/sports_tag.py + prompts/factors/game-tag-extraction.

Scope is ONE broad "looks like a sports market" prefilter — the LLM assigns the
real sport, so the prefilter only over-catches cheaply and every sport (MLB now,
NFL/NBA/soccer later) is tagged by the same pass with no code change. `sport` on
the row (LLM-assigned) is the source of truth; the snapshot renders per sport.
"""

from __future__ import annotations

import asyncio

import structlog
from pmi_core.db import session_scope
from pmi_core.engine.polymarket_sports import parse_polymarket_sports
from pmi_core.engine.sports_tag import CLASSIFIER_VERSION, NON_GAME_SENTINEL, tag_market
from pmi_core.models import CoreMarket, CoreMarketGameTag
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.tag_games")

# Venues whose sports markets name both teams in the title, so the LLM can read
# them. Gemini is NOT here — its titles are city-only and it has a structured
# instrumentSymbol, so a deterministic tagger (tag-gemini) owns it instead.
_VENUES = ["kalshi", "polymarket", "manifold", "metaculus"]

# ONE broad prefilter: league names, marquee events, bet-shape keywords, and the
# "X vs Y" matchup pattern. Precision is the LLM's job (it assigns the sport and
# drops non-matches with value=0) — this only has to be a cheap over-catch.
# Per-sport prefilters are deliberately avoided: team nicknames overlap across
# sports ("Mariners"/"Rangers"), so the right axis is "which sport?" (the LLM),
# not the keyword. Adding a sport needs NO change here.
_SPORTS_PREFILTER = (
    r"\m(MLB|NFL|NBA|NHL|NCAA|UFC|MMA|PGA|ATP|WTA|EPL)\M|"
    r"world series|super bowl|stanley cup|nba finals|champions league|"
    r"premier league|la liga|europa league|world cup|\mNLCS\M|\mALCS\M|pennant|"
    r"\m(moneyline|run ?line|puck line|spread|handicap)\M|over/under|\mO/U\M|"
    r"(win|beat|defeat|advance|qualif\w*).{0,25}\m(series|cup|championship|title|"
    r"pennant|bowl|finals?|open|masters|division|conference|playoffs?)\M|"
    r"\mvs\.?\M"
)
# esports is a separate vertical — keep its games out of the broad "vs" catch.
_ESPORTS_EXCLUDE = (
    r"\m(CS2|Valorant|Dota|LoL)\M|counter-strike|league of legends|"
    r"rocket league|rainbow six|\mesports?\M"
)

# Bound one run's LLM spend; the cron backfills the rest over subsequent runs
# (the tag table is the cache, so nothing is re-billed).
_MAX_PER_RUN = 6000
_CONCURRENCY = 8
# Upsert in chunks: Postgres caps a statement at 32767 bind params, and 15
# columns/row means ~2184 rows max — 400 stays well under AND commits progress
# every chunk, so a mid-run death never loses more than one chunk of LLM work.
_UPSERT_CHUNK = 400


async def _candidates(session) -> list[CoreMarket]:
    """Open, sports-looking markets not yet tagged at this classifier version."""
    tagged = select(CoreMarketGameTag.market_id).where(
        CoreMarketGameTag.classifier_version == CLASSIFIER_VERSION
    )
    stmt = (
        select(CoreMarket)
        .where(
            CoreMarket.resolved_at.is_(None),
            CoreMarket.venue.in_(_VENUES),
            CoreMarket.title.op("~*")(_SPORTS_PREFILTER),
            CoreMarket.title.op("!~*")(_ESPORTS_EXCLUDE),
            CoreMarket.id.not_in(tagged),
        )
        .limit(_MAX_PER_RUN)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _tag_all(markets: list[CoreMarket]) -> tuple[list[dict], float, int]:
    """Tag markets → (rows, total LLM cost_usd, n tagged without the LLM).

    Polymarket sports markets carry structured fields (slug / sportsMarketType /
    line) that tag them deterministically — free, and more accurate than the LLM
    (see engine/polymarket_sports). Only the rest go to the LLM, bounded."""
    det: list[dict] = []
    llm: list[CoreMarket] = []
    for m in markets:
        row = parse_polymarket_sports(m)
        if row is None:
            llm.append(m)
        else:
            det.append(row)
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(m: CoreMarket) -> tuple[dict | None, float]:
        async with sem:
            try:
                return await tag_market(m)
            except Exception as e:  # one bad market must not sink the batch
                log.warning("tag_games.market_failed", market_id=m.id, error=str(e))
                return None, 0.0

    results = await asyncio.gather(*(one(m) for m in llm))
    rows = det + [r for r, _ in results if r is not None]
    cost = sum(c for _, c in results)
    return rows, cost, len(det)


async def _upsert(session, rows: list[dict]) -> None:
    """Upsert a chunk of tag rows; a re-tag overwrites in place."""
    stmt = pg_insert(CoreMarketGameTag).values(rows)
    update_cols = {
        c.name: stmt.excluded[c.name]
        for c in CoreMarketGameTag.__table__.columns
        if c.name != "market_id"
    }
    await session.execute(
        stmt.on_conflict_do_update(index_elements=["market_id"], set_=update_cols)
    )


@register("tag-games", queue="long")
async def run() -> dict:
    per_sport: dict[str, int] = {}
    tagged = 0
    non_game = 0  # negative-cached (persisted) so they aren't re-billed next run
    deterministic = 0  # tagged from venue-structured fields, no LLM call
    total_cost = 0.0
    async with session_scope() as session:
        markets = await _candidates(session)
        # Tag + upsert + commit one chunk at a time so progress persists even if
        # the run dies mid-way (and each insert stays under the bind-param cap).
        for i in range(0, len(markets), _UPSERT_CHUNK):
            chunk = markets[i : i + _UPSERT_CHUNK]
            rows, cost, n_det = await _tag_all(chunk)
            total_cost += cost
            deterministic += n_det
            if rows:
                await _upsert(session, rows)
                await session.commit()
                for r in rows:
                    if r["sport"] == NON_GAME_SENTINEL:
                        non_game += 1
                    else:
                        tagged += 1
                        per_sport[r["sport"]] = per_sport.get(r["sport"], 0) + 1

    log.info(
        "tag_games.done",
        candidates=len(markets),
        tagged=tagged,
        non_game=non_game,
        deterministic=deterministic,
        per_sport=per_sport,
        cost_usd=round(total_cost, 4),
    )
    return {
        "candidates": len(markets),
        "tagged": tagged,
        "non_game": non_game,
        "deterministic": deterministic,
        "per_sport": per_sport,
        "cost_usd": round(total_cost, 4),
    }


_RETAG_PAGE = 2000


@register("retag-polymarket-sports", queue="long")
async def retag_polymarket_sports() -> dict:
    """One-off: rewrite open Polymarket rows the LLM tagged before the
    deterministic parser existed (e.g. CFB tagged nfl with NFL team names, which
    could attach to an NFL matchup). Not on cron — run once after deploy, and
    again only if the parser changes. Pages by market_id (PK on both tables) so no
    statement scans the 391k-row tag table joined to core_markets at once."""
    last, scanned, rewritten = 0, 0, 0
    async with session_scope() as session:
        while True:
            ids = (await session.execute(text(
                "SELECT market_id FROM core_market_game_tag "
                "WHERE resolved_via = 'llm_fallback' AND market_id > :last "
                "ORDER BY market_id LIMIT :n"
            ), {"last": last, "n": _RETAG_PAGE})).scalars().all()
            if not ids:
                break
            last = ids[-1]
            scanned += len(ids)
            markets = (await session.execute(
                select(CoreMarket).where(
                    CoreMarket.id.in_(ids),
                    CoreMarket.venue == "polymarket",
                    CoreMarket.resolved_at.is_(None),
                )
            )).scalars().all()
            rows = [r for r in map(parse_polymarket_sports, markets) if r is not None]
            for i in range(0, len(rows), _UPSERT_CHUNK):
                await _upsert(session, rows[i : i + _UPSERT_CHUNK])
            await session.commit()
            rewritten += len(rows)
    log.info("retag_polymarket_sports.done", scanned=scanned, rewritten=rewritten)
    return {"scanned": scanned, "rewritten": rewritten}
