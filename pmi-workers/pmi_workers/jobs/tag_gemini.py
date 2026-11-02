"""tag-gemini — deterministic NFL/MLB tagging for Gemini, no LLM.

Gemini titles are city-only ("Detroit vs Minnesota"), so the LLM mislabels the
sport AND the teams (a Guardians/Rays game tagged "Cleveland Browns vs Tampa Bay
Buccaneers", a regular game's event hallucinated to "World Series" from the
"Chicago WS" abbreviation). The instrumentSymbol has sport + teams + date
exactly, so this job reads it (engine.gemini_symbol) and tags the games for the
sports we render; every other Gemini market is left untagged, off the boards.

Mirrors tag-sports-kalshi: a per-venue deterministic tagger, so Gemini never
touches the LLM pass (it's removed from tag-games' venue list). Upserts every
run, so it also heals the LLM's old mis-tags in place.
"""

from __future__ import annotations

import structlog
from pmi_core.db import session_scope
from pmi_core.engine import gemini_symbol
from pmi_core.engine.sports_tag import CLASSIFIER_VERSION
from sqlalchemy import text

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.tag_gemini")

_SYM = "raw::jsonb -> 'contract' ->> 'instrumentSymbol'"

# Gemini games are moneyline-only (modeled winner markets); event/line/side are
# cleared so an LLM's old "World Series" / spread guess can't survive the upsert.
_UPSERT = text("""
  INSERT INTO core_market_game_tag
    (market_id, sport, market_class, market_type, home_team, away_team, game_date,
     line_value, side, event, competitor, resolved_via, classifier_version, tagged_at)
  VALUES (:mid,:sport,'game','moneyline',:home,:away,:date,NULL,NULL,NULL,NULL,
          'gemini-symbol',:ver, now())
  ON CONFLICT (market_id) DO UPDATE SET
    sport=:sport, market_class='game', market_type='moneyline', home_team=:home,
    away_team=:away, game_date=:date, line_value=NULL, side=NULL, event=NULL,
    competitor=NULL, resolved_via='gemini-symbol', classifier_version=:ver, tagged_at=now()
""")


@register("tag-gemini", queue="batch")
async def run() -> dict:
    like = " OR ".join(f"{_SYM} LIKE 'GEMI-{t}-%'" for t in gemini_symbol.served_sport_tokens())
    tagged = skipped = 0
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    f"SELECT id, {_SYM} AS sym FROM core_markets "
                    f"WHERE venue='gemini' AND resolved_at IS NULL AND ({like})"
                )
            )
        ).all()
        for mid, sym in rows:
            g = gemini_symbol.parse(sym)
            if g is None or g.sport == "other" or not (g.home and g.away):
                skipped += 1  # non-served sport, or a code we can't resolve
                continue
            await session.execute(_UPSERT, {
                "mid": mid, "sport": g.sport, "home": g.home, "away": g.away,
                "date": g.game_date, "ver": CLASSIFIER_VERSION,
            })
            tagged += 1
        await session.commit()

    log.info("tag_gemini.done", found=len(rows), tagged=tagged, skipped=skipped)
    return {"found": len(rows), "tagged": tagged, "skipped": skipped}
