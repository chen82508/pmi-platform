"""tag-sports-polymarket — correct Polymarket MLB game tags from the slug, no LLM.

Polymarket titles ("Brewers vs. Dodgers") name both teams but carry no date, so the
LLM tagger (`tag-games`) guesses the game date — and gets it wrong (Oct 23 for an
Oct 15 game, even 2023), which drops the market out of the game board's window or
into the wrong bucket, so it never folds across exchanges with the Kalshi game.

The real date (and the matchup) is in the market's `raw.slug` — mlb-mil-lad-2026-10-15
— which this job parses deterministically to overwrite the LLM's date/teams/side/line.
It's a corrector, not a discoverer: candidates are the markets the LLM already
flagged as MLB games (a cheap market_id-indexed join, not a title/raw scan), and the
slug prefix also filters out the non-MLB markets the LLM mis-tags as mlb (cpbl-/npb-/
cs2-… → parse returns None → left alone). New games appear once the hourly `tag-games`
pass tags them; this run then fixes their dates.
"""

from __future__ import annotations

import structlog
from pmi_core.db import session_scope
from pmi_core.engine.polymarket_slug import parse_mlb_game
from pmi_core.engine.sports_tag import CLASSIFIER_VERSION
from sqlalchemy import text

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.tag_sports_polymarket")

# Candidates = Polymarket markets the LLM tagged as MLB games. market_id-indexed
# join, so this never touches the title trigram / raw scan that times out at scale.
_CANDIDATES = text("""
  SELECT m.id, m.raw->>'slug' AS slug
  FROM core_market_game_tag g JOIN core_markets m ON m.id = g.market_id
  WHERE m.venue = 'polymarket' AND g.sport = 'mlb' AND m.resolved_at IS NULL
""")

_UPSERT = text("""
  INSERT INTO core_market_game_tag
    (market_id, sport, market_class, market_type, home_team, away_team, game_date,
     line_value, side, resolved_via, classifier_version, tagged_at)
  VALUES (:mid,'mlb','game',:mt,:home,:away,:date,:line,:side,'slug',:ver, now())
  ON CONFLICT (market_id) DO UPDATE SET
    sport='mlb', market_class='game', market_type=:mt, home_team=:home,
    away_team=:away, game_date=:date, line_value=:line, side=:side,
    resolved_via='slug', classifier_version=:ver, tagged_at=now()
""")


@register("tag-sports-polymarket", queue="batch")
async def run() -> dict:
    tagged = {"moneyline": 0, "total": 0}
    async with session_scope() as session:
        rows = (await session.execute(_CANDIDATES)).all()
        for mid, slug in rows:
            pg = parse_mlb_game(slug)
            if pg is None:  # non-MLB slug or non-game market → leave the LLM tag be
                continue
            await session.execute(_UPSERT, {
                "mid": mid, "mt": pg.market_type, "home": pg.home, "away": pg.away,
                "date": pg.game_date, "line": pg.line, "side": pg.side,
                "ver": CLASSIFIER_VERSION,
            })
            tagged[pg.market_type] += 1
        await session.commit()

    log.info("tag_sports_polymarket.done", candidates=len(rows), tagged=tagged)
    return {"candidates": len(rows), "tagged": tagged}
