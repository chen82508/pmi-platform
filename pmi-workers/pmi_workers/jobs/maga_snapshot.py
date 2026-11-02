"""maga-snapshot job — pre-compute MAGA heat into ts_page_cache (JSONB).

Runs every 10 minutes (crontab: `*/10 * * * *`).

**Source = the YAML engine** (2026-06-24 switch). Reads the latest persisted
``ts_index_scores`` for the three declarative MAGA chamber indexes
(maga-{senate,governor,house}-2026, ``formula: entity_lean_mean``) and composes
them — via ``engine.maga_compose`` — into the same ``ts_page_cache`` payload the
``/maga/*`` endpoints already serve. So the website shows the validated YAML
numbers while keeping the existing fast cache-read path (no per-request compute).

This replaces the legacy ``state_lean`` / ``aggregate_state_detail`` regex path.
Composition is pure dict work over the persisted breakdowns — milliseconds — so
the job is just three indexed score reads + the cache write.

Trend history: ts_page_cache IS the trend store. Each run appends one row; the
trend endpoint reads past rows' heat over time.

Note: per-race ``contracts`` drill-down is not carried in the breakdown, so the
per-state ``groups`` ship heat + counts with an empty ``contracts`` list (the
endpoint defaults it). Wiring component markets through is a follow-up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from pmi_core.db import session_scope
from pmi_core.engine import trend_store
from pmi_core.engine.maga_compose import (
    compose_state_choropleth,
    top_maga_pmi,
)
from pmi_core.engine.seat_mapping import HOUSE_SEATS, state_code
from pmi_core.models import CoreIndexDefinition, CoreMarket, TsIndexScore, TsPageCache
from sqlalchemy import insert, select

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.maga_snapshot")

CYCLE_YEAR = 2026
MAGA_INDEXES = {
    "senate": "maga-senate-2026",
    "governor": "maga-governor-2026",
    "house": "maga-house-2026",
}


async def _latest_breakdowns(
    session,
) -> tuple[dict[str, tuple[float | None, dict | None]], dict[str, int | None]]:
    """({chamber: (national_score, breakdown)}, {chamber: score_row_id}) from
    the newest score of each index."""
    out: dict[str, tuple[float | None, dict | None]] = {}
    source_ids: dict[str, int | None] = {}
    for chamber, index_id in MAGA_INDEXES.items():
        row = (
            await session.execute(
                select(TsIndexScore.score, TsIndexScore.breakdown, TsIndexScore.id)
                .join(
                    CoreIndexDefinition,
                    TsIndexScore.index_definition_id == CoreIndexDefinition.id,
                )
                .where(
                    CoreIndexDefinition.index_id == index_id,
                    CoreIndexDefinition.is_current.is_(True),
                )
                .order_by(TsIndexScore.as_of.desc())
                .limit(1)
            )
        ).first()
        # score is NUMERIC (Decimal) — cast to float so the composed payload is
        # JSON-serializable; breakdown is a JSON column (already floats).
        if row:
            out[chamber] = (float(row.score) if row.score is not None else None, row.breakdown)
            source_ids[chamber] = row.id
        else:
            out[chamber] = (None, None)
            source_ids[chamber] = None
    return out, source_ids


async def _last_snapshot_source_ids(session, cycle_year: int) -> dict | None:
    """source_score_ids of the newest existing ``maga:{cycle}`` cache row."""
    payload = await session.scalar(
        select(TsPageCache.payload)
        .where(TsPageCache.cache_key == f"maga:{cycle_year}")
        .order_by(TsPageCache.computed_at.desc())
        .limit(1)
    )
    return (payload or {}).get("source_score_ids")


def _district_of(entity_key: str) -> int | None:
    """House entity key 'California-20' -> 20 (at-large already canonical -1)."""
    _, _, tail = entity_key.rpartition("-")
    return int(tail) if tail.isdigit() else None


async def _payload_before(session, cache_key: str, cutoff: datetime) -> dict | None:
    """Freshest cache payload at or before ``cutoff`` — a delta baseline."""
    return await session.scalar(
        select(TsPageCache.payload)
        .where(
            TsPageCache.cache_key == cache_key,
            TsPageCache.computed_at <= cutoff,
        )
        .order_by(TsPageCache.computed_at.desc())
        .limit(1)
    )


def _delta(cur: float | None, old: float | None) -> float | None:
    return round(cur - old, 2) if (cur is not None and old is not None) else None


def _race_key(g: dict) -> tuple:
    """Stable identity of a race group across snapshots."""
    return (g.get("state_code"), g.get("office"), g.get("district"))


@register("maga-snapshot", queue="snapshot")
async def run(cycle_year: int = CYCLE_YEAR) -> dict:
    """Compose ts_page_cache from the YAML chamber breakdowns (no re-scoring)."""
    computed_at = datetime.now(UTC)

    async with session_scope() as session:
        bd, source_ids = await _latest_breakdowns(session)
        senate_s, senate_bd = bd["senate"]
        gov_s, gov_bd = bd["governor"]
        house_s, house_bd = bd["house"]

        if not any((senate_bd, gov_bd, house_bd)):
            log.warning("maga_snapshot.no_scores")
            return {"groups": 0, "states": 0}

        # The job runs */10 but the three source scores update hourly — same
        # source rows → the payload would be byte-identical; skip the append.
        if any(v is not None for v in source_ids.values()) and (
            source_ids == await _last_snapshot_source_ids(session, cycle_year)
        ):
            log.info("maga_snapshot.unchanged", source_score_ids=source_ids)
            return {"groups": 0, "states": 0, "skipped": "unchanged"}

        # L4 national + L3 per-state choropleth (both equal-weight, by name)
        national_heat = top_maga_pmi([senate_s, gov_s, house_s])
        choropleth = compose_state_choropleth([senate_bd, gov_bd, house_bd])

        # Per-state groups: senate + governor entities (one per state) + house
        # districts (per district). Keyed by 2-letter code.
        groups_by_state: dict[str, list[dict]] = {}
        n_markets_by_state: dict[str, int] = {}

        def _add(code: str, group: dict, n: int) -> None:
            # Distinct venues this race's contracts trade on — for the race
            # card's exchange chips. Survives the contracts-strip in groups_list
            # (only "contracts" is dropped), so the by-groups payload keeps it.
            group.setdefault(
                "exchanges",
                sorted(
                    {c["venue"] for c in group.get("contracts", []) if c.get("venue")}
                ),
            )
            groups_by_state.setdefault(code, []).append(group)
            n_markets_by_state[code] = n_markets_by_state.get(code, 0) + n

        for chamber, breakdown in (("senate", senate_bd), ("governor", gov_bd)):
            for state_name, row in ((breakdown or {}).get("entities") or {}).items():
                code = row.get("code") or state_code(state_name)
                if not code:
                    continue
                _add(code, {
                    "state_code": code, "state": state_name, "office": chamber,
                    "district": None, "heat": row["heat"],
                    "n_markets": row.get("n_markets", 0),
                    "volume_24h": sum((c.get("volume_24h") or 0) for c in row.get("contracts", [])),
                    "base_question": f"{state_name} {chamber} 2026",
                    "venue_lineage": row.get("venue_lineage", []),
                    # §F(a): no real-money liquidity behind this race this tick —
                    # the heat is standing quotes (or, prior_applied, the
                    # incumbent-hold prior), not a traded price. Pages say so.
                    "no_market": bool(row.get("no_market", False)),
                    "prior_applied": bool(row.get("prior_applied", False)),
                "contracts": row.get("contracts", []),
                }, row.get("n_markets", 0))

        for key, row in ((house_bd or {}).get("entities") or {}).items():
            code = row.get("code", "")
            scode, _, _ = code.rpartition("-")
            state_name = key.rsplit("-", 1)[0]
            if not scode:
                continue
            # Defensive: drop phantom districts beyond a state real seat count
            # (e.g. a WA-34 parsed out of a "vote count above 340K" market — WA
            # has 10). extraction._district_key clamps per-state upstream, but a
            # stale pre-fix breakdown can still carry one until the next re-score.
            district = _district_of(key)
            seats = HOUSE_SEATS.get(scode)
            if seats is None or district is None or not 1 <= district <= seats:
                continue
            _add(scode, {
                "state_code": scode, "state": state_name, "office": "house",
                "district": district, "heat": row["heat"],
                "n_markets": row.get("n_markets", 0),
                    "volume_24h": sum((c.get("volume_24h") or 0) for c in row.get("contracts", [])),
                "base_question": f"{key} US House 2026",
                "venue_lineage": row.get("venue_lineage", []),
                "no_market": bool(row.get("no_market", False)),
                "prior_applied": bool(row.get("prior_applied", False)),
                "contracts": row.get("contracts", []),
            }, row.get("n_markets", 0))

        # Contract urls: the frozen breakdown often carries url=None (polymarket
        # url is derived at ingest, so an old breakdown predates it), which would
        # leave a drill-down card unclickable. Backfill from core_markets.
        _cons = [
            c
            for gs in groups_by_state.values()
            for g in gs
            for c in g.get("contracts", [])
        ]
        _need = list({c["market_id"] for c in _cons if not c.get("url") and c.get("market_id")})
        if _need:
            _urls = dict(
                (
                    await session.execute(
                        select(CoreMarket.id, CoreMarket.url).where(
                            CoreMarket.id.in_(_need), CoreMarket.url.isnot(None)
                        )
                    )
                ).all()
            )
            for c in _cons:
                if not c.get("url"):
                    _u = _urls.get(c.get("market_id"))
                    if _u:
                        c["url"] = _u

        # National payload: per-state choropleth + flat groups list
        name_to_code = {}
        for breakdown in (senate_bd, gov_bd, house_bd):
            for st, r in ((breakdown or {}).get("entities") or {}).items():
                if r.get("code"):
                    name_to_code.setdefault(st, r["code"].rpartition("-")[0] or r["code"])
        # house groups (state rollup) carry state-name → code too
        for st, r in ((house_bd or {}).get("groups") or {}).items():
            if r.get("code"):
                name_to_code.setdefault(st, r["code"])

        by_state: dict[str, dict] = {}
        for state_name, heat in choropleth.items():
            code = name_to_code.get(state_name) or state_code(state_name)
            if not code:
                continue
            offices = sorted({g["office"] for g in groups_by_state.get(code, [])})
            # Per-office heat (addressable for the office-detail trend, so its
            # chart plots the SAME quantity as the office's headline PMI tile —
            # the state-level `heat` above mixes all offices and diverges).
            _office_heats: dict[str, list[float]] = {}
            for g in groups_by_state.get(code, []):
                _office_heats.setdefault(g["office"], []).append(float(g["heat"]))
            office_heat = {
                o: round(sum(hs) / len(hs), 2) for o, hs in _office_heats.items()
            }
            # Per-district heat (2-digit key) so a house-district trend can plot
            # that single district, matching its detail headline.
            district_heat = {
                f"{int(g['district']):02d}": round(float(g["heat"]), 2)
                for g in groups_by_state.get(code, [])
                if g["office"] == "house" and g.get("district") is not None
            }
            by_state[code] = {
                "state": state_name, "state_code": code, "heat": heat,
                "office_heat": office_heat,
                "district_heat": district_heat,
                "n_markets": n_markets_by_state.get(code, 0),
                "offices": offices,
                "volume_24h": sum(g["volume_24h"] for g in groups_by_state.get(code, [])),
                # Distinct venues across ALL the state's races → state card chips.
                "exchanges": sorted({
                    c["venue"]
                    for g in groups_by_state.get(code, [])
                    for c in g.get("contracts", [])
                    if c.get("venue")
                }),
            }

        # Δ14d for every race group + state, diffed against the freshest
        # maga:{cycle} payload ≥14d ago (D7 — one cache read, not N trend
        # queries; heat history predates the field, so deltas populate from the
        # first enriched run). Injected into the ORIGINAL group dicts so both
        # the flat groups list and the per-state detail cache inherit it.
        prev14 = await _payload_before(
            session, f"maga:{cycle_year}", computed_at - timedelta(days=14)
        ) or {}
        prev24 = await _payload_before(
            session, f"maga:{cycle_year}", computed_at - timedelta(hours=24)
        ) or {}
        old_group_heat = {
            _race_key(g): g.get("heat") for g in prev14.get("groups") or []
        }
        for gs in groups_by_state.values():
            for g in gs:
                g["delta_14d"] = _delta(g.get("heat"), old_group_heat.get(_race_key(g)))
        old_states = prev14.get("states") or {}
        for code, row in by_state.items():
            row["delta_14d"] = _delta(
                row.get("heat"), (old_states.get(code) or {}).get("heat")
            )

        groups_list = [
            {k: v for k, v in g.items() if k != "contracts"}
            for gs in groups_by_state.values() for g in gs
        ]

        # Per-chamber national PMI (the three chamber scores L4 top_maga_pmi
        # averages) + per-chamber contract totals, so the By-Senate/House/
        # Governor tabs can show each chamber's own number instead of the
        # combined national_heat. Emitted from the SAME snapshot, so the "all"
        # view (national_heat) always equals the average of the three shown.
        # Score aggregates by average; contract counts aggregate by sum.
        n_markets_by_chamber: dict[str, int] = {}
        for g in groups_list:
            n_markets_by_chamber[g["office"]] = (
                n_markets_by_chamber.get(g["office"], 0) + g.get("n_markets", 0)
            )
        chambers = {
            "senate": {"pmi": round(senate_s, 2) if senate_s is not None else None,
                       "n_markets": n_markets_by_chamber.get("senate", 0)},
            "governor": {"pmi": round(gov_s, 2) if gov_s is not None else None,
                         "n_markets": n_markets_by_chamber.get("governor", 0)},
            "house": {"pmi": round(house_s, 2) if house_s is not None else None,
                      "n_markets": n_markets_by_chamber.get("house", 0)},
        }
        # Per-chamber Δ24h/Δ14d against the same baselines (the v4 hero + tabs
        # show each chamber's own move, not just the combined national one).
        for ch, stat in chambers.items():
            for lab, prev in (("delta_24h", prev24), ("delta_14d", prev14)):
                stat[lab] = _delta(
                    stat.get("pmi"), ((prev.get("chambers") or {}).get(ch) or {}).get("pmi")
                )

        # Distinct contributing venues across the three chambers (breakdown.venues),
        # so the UI shows the real exchange count + icons, not a hardcoded number.
        exchanges = sorted({
            venue
            for _score, breakdown in bd.values()
            for venue in ((breakdown or {}).get("venues") or {})
        })

        maga_key = f"maga:{cycle_year}"
        maga_payload = {
            "computed_at": computed_at.isoformat(),
            "cycle_year": cycle_year,
            "source_score_ids": source_ids,
            "national_heat": round(national_heat, 2) if national_heat is not None else None,
            # National Δ + book total (v4 hero delta / SHARE-OF-BOOK
            # denominator; race-attributed sum, same basis as states').
            "delta_24h": _delta(
                round(national_heat, 2) if national_heat is not None else None,
                prev24.get("national_heat"),
            ),
            "delta_14d": _delta(
                round(national_heat, 2) if national_heat is not None else None,
                prev14.get("national_heat"),
            ),
            "volume_24h": round(sum(s.get("volume_24h") or 0 for s in by_state.values()), 2),
            "chambers": chambers,
            "n_states": len(by_state),
            "n_markets": sum(n_markets_by_state.values()),
            "exchanges": exchanges,
            "states": by_state,
            "groups": groups_list,
        }
        await session.execute(
            insert(TsPageCache).values(
                cache_key=maga_key, payload=maga_payload, computed_at=computed_at
            )
        )

        # Slim trend companion (trend_store): /maga/trends and
        # /maga/by-state/*/trends read this instead of detoasting the full
        # `maga:{cycle}` board blob once per snapshot across the window.
        if (slim := trend_store.slim_values(maga_key, maga_payload, computed_at)) is not None:
            await session.execute(insert(TsPageCache).values(**slim))

        state_cache_rows = [
            {
                "cache_key": f"maga-state:{code}:{cycle_year}",
                "payload": {
                    "state": by_state[code]["state"], "state_code": code,
                    "heat": by_state[code]["heat"],
                    "n_markets": by_state[code]["n_markets"],
                    # sum of the state's group volumes (was hardcoded 0.0 — the
                    # per-state page showed $0 while its offices carried real volume)
                    "volume_24h": by_state[code]["volume_24h"],
                    "offices": by_state[code]["offices"],
                    "groups": groups_by_state.get(code, []),
                },
                "computed_at": computed_at,
            }
            for code in by_state
        ]
        if state_cache_rows:
            await session.execute(insert(TsPageCache), state_cache_rows)

        await session.commit()

    log.info(
        "maga_snapshot.done",
        source="yaml",
        groups=len(groups_list),
        states=len(by_state),
        national_heat=round(national_heat, 2) if national_heat is not None else None,
        computed_at=computed_at.isoformat(),
    )
    return {"groups": len(groups_list), "states": len(by_state)}
