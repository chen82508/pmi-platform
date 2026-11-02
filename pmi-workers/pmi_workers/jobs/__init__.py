"""Job package — importing it triggers @register side-effects.

Every job module must import `from pmi_workers.registry import register` and
decorate at least one coroutine. Add new modules below to enroll them at
process start.
"""

from __future__ import annotations

from pmi_workers.jobs import (  # noqa: F401  registration side-effects
    board_trend_backfill,
    climate_snapshot,
    daily,
    embed_markets,
    esports_snapshot,
    gdp_board_snapshot,
    governor_board_snapshot,
    home_snapshot,
    hourly,
    house_board_snapshot,
    insights_snapshot,
    llm_batch,
    maga_snapshot,
    markets_board_snapshot,
    movers_snapshot,
    notify_new_indexes,
    page_cache_retention,
    reeval_drifted,
    reeval_market,
    refresh_market_priority,
    score,
    score_all,
    senate_board_snapshot,
    sports_snapshot,
    tag_games,
    tag_gemini,
    tag_races,
    tag_sports_kalshi,
    tag_sports_polymarket,
    venue_spread_snapshot,
    war_snapshot,
    war_summary,
    weekly_report,
    workflow,
    worlds_snapshot,
)
