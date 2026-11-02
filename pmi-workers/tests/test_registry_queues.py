"""Every job declares which worker lane it belongs to.

The lane used to be the caller's choice (`enqueue --queue batch` in the
crontab). One missing flag put a multi-hour whole-registry pass in the fast
lane, where it held a `worker_concurrency` slot until it finished and every
10-minute board snapshot queued behind it. These tests pin the routing to the
job so a new producer — or a new cron line — can't reintroduce that.

Four lanes since 2026-10-20 (each is one claim-loop container, see
docker-compose.yml; a lane nothing claims is a job that never runs):

* default  — on-demand `score` and the WS `reeval-market` fan-out: ~700 short
             jobs an hour that must not share a lane with anything a page
             waits on.
* snapshot — the */10 page snapshots. Sharing `default` with the fan-out had
             them queued 20-80 min, so every board flagged DELAYED while its
             scores were fresh.
* batch    — the whole-registry passes a page never blocks on.
* long     — the multi-hour ones (`daily` = embed + full re-score, ~2.5 h;
             `tag-games`; backtest `workflow`s). Serialised on their own so
             they no longer park `hourly` / `reeval-drifted` for a morning.
"""

from __future__ import annotations

import pytest

from pmi_workers import jobs  # noqa: F401  — populates the registry
from pmi_workers.registry import DEFAULT_QUEUE, all_names, queue_for

SNAPSHOT = {
    "climate-snapshot",
    "esports-snapshot",
    "gdp-board-snapshot",
    "governor-board-snapshot",
    "home-snapshot",
    "house-board-snapshot",
    "maga-snapshot",
    "markets-board-snapshot",
    "movers-snapshot",
    "senate-board-snapshot",
    "sports-snapshot",
    "war-snapshot",
    "worlds-snapshot",
}

LONG = {"daily", "embed-markets", "tag-games", "retag-polymarket-sports", "workflow"}

# No page blocks on these results — most scan every current index (or every
# market). `venue-spread-snapshot` is ~20s but is still a whole-registry scan
# feeding a cache, so it belongs behind the same door; the daily editorial notes
# (`insights-snapshot`'s gap-pairs self-join, `war-summary`'s compose) likewise
# only rewrite ts_page_cache on a cron beat. The `tag-*` jobs classify markets
# into tags the snapshots read later (no page waits on them), so they ride the
# batch lane too.
BATCH = {
    "board-trend-backfill",
    "hourly",
    "insights-snapshot",
    "llm-batch-submit",
    "page-cache-retention",
    "reeval-drifted",
    "score-all",
    "tag-gemini",
    "tag-races",
    "tag-sports-kalshi",
    "tag-sports-polymarket",
    "venue-spread-snapshot",
    "war-summary",
    "weekly-report",
}


def test_page_snapshots_are_on_the_snapshot_lane() -> None:
    for name in sorted(SNAPSHOT):
        assert queue_for(name) == "snapshot", f"{name} would queue behind the WS fan-out"


def test_multi_hour_passes_are_on_the_long_lane() -> None:
    for name in sorted(LONG):
        assert queue_for(name) == "long", f"{name} would park the hourly pass"


def test_whole_registry_passes_are_on_the_batch_lane() -> None:
    for name in sorted(BATCH):
        assert queue_for(name) == "batch", f"{name} must not run in the fast lane"


def test_everything_else_is_on_the_default_lane() -> None:
    for name in sorted(set(all_names()) - SNAPSHOT - LONG - BATCH):
        assert queue_for(name) == DEFAULT_QUEUE, f"{name} would never be claimed"


def test_no_snapshot_job_lands_in_the_default_lane() -> None:
    """A new `*-snapshot` job registered without a lane is the 10-20 bug again."""
    for name in all_names():
        if name.endswith("-snapshot"):
            assert queue_for(name) in {"snapshot", "batch"}, name


@pytest.mark.parametrize("name", ["score", "score:war-index-2026", "reeval-market", "refresh-market-priority"])
def test_on_demand_and_fan_out_jobs_stay_fast(name: str) -> None:
    assert queue_for(name) == DEFAULT_QUEUE


def test_unregistered_name_falls_back_to_default() -> None:
    assert queue_for("no-such-job") == DEFAULT_QUEUE
