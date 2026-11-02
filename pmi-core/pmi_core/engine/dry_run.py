"""In-process dry run of the PMI pipeline — **never touches the database**.

Loads an IndexDef YAML + a JSON fixture of markets, runs the keyword/category
selectors in pure Python, evaluates every factor with the deterministic stub
(`factor_evaluator._stub_score`), and finally calls the real `aggregate()` so
the output mirrors what a live `pmi-core score` would produce on the same
inputs — minus DB writes and minus MLflow.

Usage (see `pmi-core dry-run` CLI):

    from pathlib import Path
    from pmi_core.dsl.ir import load_index_def
    from pmi_core.engine.dry_run import dry_run
    ir, _yaml, _sha = load_index_def("pmi_core/index_defs/war-index-2026.yaml")
    report = dry_run(ir, Path("pmi-demo/fixtures/markets.json"))

The report shape is stable enough to be diffed across YAML revisions, which
is the whole point — `dry-run` lets you change a selector or factor weight
and immediately see which markets enter the bucket and what the score moves
to, without spinning up Postgres / Alembic / MLflow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pmi_core.dsl.ir import CategorySelector, IndexDef, KeywordSelector
from pmi_core.engine.aggregator import MarketEvaluations, aggregate
from pmi_core.engine.factor_evaluator import _stub_score
from pmi_core.llm.base import parse_factor_response
from pmi_core.llm.fixture_client import _answer_for
from pmi_core.models import AuditEvaluation, CoreMarket


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _market_from_fixture(idx: int, m: dict[str, Any]) -> CoreMarket:
    """Build an UNATTACHED `CoreMarket` so the aggregator/evaluator can read it.

    We synthesise `id` from the fixture index because real `core_markets.id` is
    only assigned at INSERT time, and dry-run never touches the DB.
    """
    return CoreMarket(
        id=idx + 1,
        venue=m.get("venue") or "polymarket",
        external_id=str(m.get("external_id") or m.get("id") or f"fixture-{idx}"),
        slug=m.get("slug"),
        title=str(m.get("title") or "(untitled fixture)"),
        description=m.get("description"),
        category=m.get("category"),
        tags=m.get("tags"),
        opens_at=_parse_dt(m.get("opens_at")),
        closes_at=_parse_dt(m.get("closes_at")),
        resolved_at=_parse_dt(m.get("resolved_at")),
        raw=m,
    )


def _matches(market: CoreMarket, ir: IndexDef) -> list[str]:
    """Return the list of selector tags that matched this market (for the report).

    Mirrors `pmi_core.engine.selector.select_markets` but in-process and with
    per-market provenance so the dry-run report can explain *why* a market was
    selected.
    """
    matched: list[str] = []
    title_lower = (market.title or "").lower()
    for sel in ir.selectors:
        if isinstance(sel, KeywordSelector):
            for term in sel.terms:
                if term.lower() in title_lower:
                    matched.append(f"keyword:{term}")
        elif isinstance(sel, CategorySelector):
            if market.category == sel.polymarket_tag:
                matched.append(f"category:{sel.polymarket_tag}")
        # SemanticSelector deliberately ignored (P2+; needs embeddings).
    return matched


def _fake_evaluation(
    market: CoreMarket, factor_id: str, factor_type: str
) -> AuditEvaluation:
    """Mint an UNATTACHED `AuditEvaluation`.

    Fixture markets that carry ``judgements`` are answered from those labels
    (the same answers the ``fixture/`` provider replays in the full stack);
    anything else falls back to the deterministic stub.

    `aggregator.aggregate()` only reads `.value_numeric` + `.id`; everything
    else is set so the JSON dump in the dry-run report is self-explanatory.
    """
    from pmi_core.dsl.ir import FactorSpec

    spec = FactorSpec(id=factor_id, type=factor_type, prompt_ref=f"prompts/factors/{factor_id}-v1")
    if isinstance(market.raw, dict) and "judgements" in market.raw:
        model_id = "fixture/labels"
        payload = {"confidence": 1.0, **_answer_for(market, factor_id)}
        value_numeric, value_label, confidence, _ = parse_factor_response(
            json.dumps(payload), spec
        )
    else:
        model_id = "stub-deterministic-v1"
        value_numeric, value_label, confidence = _stub_score(market, spec)
    return AuditEvaluation(
        id=None,
        market_id=market.id,
        index_definition_id=0,  # not persisted; lineage column meaningless in dry-run
        factor_id=factor_id,
        prompt_id=0,
        prompt_sha256="dryrun-no-prompt-row",
        model_id=model_id,
        temperature=None,
        value_numeric=value_numeric,
        value_label=value_label,
        confidence=confidence,
        model_response={"stub": model_id.startswith("stub-"), "dry_run": True},
        cost_usd=0.0,
        latency_ms=0,
        mlflow_run_id=None,
        evaluated_at=datetime.now(UTC),
    )


@dataclass(slots=True)
class DryRunReport:
    """JSON-serialisable record of what dry-run did. Kept flat for diffability."""

    index_id: str
    version: int
    yaml_sha256: str
    fixture_path: str
    fixture_market_count: int
    selectors: dict[str, Any]
    factors: list[dict[str, Any]]
    aggregation: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_id": self.index_id,
            "version": self.version,
            "yaml_sha256": self.yaml_sha256,
            "fixture_path": self.fixture_path,
            "fixture_market_count": self.fixture_market_count,
            "selectors": self.selectors,
            "factors": self.factors,
            "aggregation": self.aggregation,
        }


def dry_run(
    ir: IndexDef,
    fixture_path: Path,
    *,
    yaml_sha256: str = "",
) -> DryRunReport:
    """Run the whole pipeline in-memory and return a structured report.

    Notes for callers:
    - All factor values come from `_stub_score`, so flipping the same factor on
      the same market across runs is reproducible.
    - The aggregator dispatches on `aggregation.formula`: `seat_projection_sum`
      → Poisson-binomial E[R seats]; anything else → 0..100 weighted average.
      `aggregation.formula_used` reflects which branch actually ran (and will
      equal `formula_declared` for both supported formulas).
    """
    fixture_data = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    if not isinstance(fixture_data, list):
        raise ValueError(f"{fixture_path}: expected a JSON list of markets")

    all_markets = [_market_from_fixture(i, m) for i, m in enumerate(fixture_data)]

    selected: list[tuple[CoreMarket, list[str]]] = []
    for market in all_markets:
        tags = _matches(market, ir)
        if tags:
            selected.append((market, tags))

    factor_payloads: list[dict[str, Any]] = []
    market_rows: list[MarketEvaluations] = []

    market_to_evals: dict[int, dict[str, AuditEvaluation]] = {
        m.id: {} for m, _ in selected
    }

    for factor in ir.factors:
        evaluations: list[dict[str, Any]] = []
        for market, _ in selected:
            ev = _fake_evaluation(market, factor.id, factor.type)
            market_to_evals[market.id][factor.id] = ev
            evaluations.append(
                {
                    "market_id": market.id,
                    "market_title": (market.title or "")[:120],
                    "value_numeric": ev.value_numeric,
                    "value_label": ev.value_label,
                    "confidence": round(ev.confidence, 3),
                }
            )
        factor_payloads.append(
            {
                "factor_id": factor.id,
                "factor_type": factor.type,
                "weight": factor.weight,
                "prompt_ref": factor.prompt_ref,
                "evaluations": evaluations,
            }
        )

    for market, _ in selected:
        raw = market.raw or {}
        last_price_raw = raw.get("last_price")
        last_price = (
            float(last_price_raw) if isinstance(last_price_raw, (int, float)) else None
        )
        # CORR-3.4: fixtures don't carry orderbook depth (the CLOB poller is
        # online-only), so dry-run reads volume_24h directly off the fixture
        # row as the liquidity proxy. Real ticks prefer depth_1pct when
        # available — see pipeline._latest_orderbook_depths.
        vol_raw = raw.get("volume_24h")
        liquidity = (
            float(vol_raw) if isinstance(vol_raw, (int, float)) and vol_raw > 0 else None
        )
        market_rows.append(
            MarketEvaluations(
                market=market,
                by_factor=market_to_evals[market.id],
                last_price=last_price,
                liquidity=liquidity,
            )
        )

    agg = aggregate(market_rows, ir)

    return DryRunReport(
        index_id=ir.id,
        version=ir.version,
        yaml_sha256=yaml_sha256,
        fixture_path=str(fixture_path),
        fixture_market_count=len(all_markets),
        selectors={
            "candidates_returned": len(selected),
            "markets": [
                {
                    "id": m.id,
                    "title": (m.title or "")[:120],
                    "category": m.category,
                    "last_price": (m.raw or {}).get("last_price"),
                    "matched_by": tags,
                }
                for m, tags in selected
            ],
        },
        factors=factor_payloads,
        aggregation={
            "score": agg.score,
            "component_count": agg.component_count,
            "component_evaluation_ids_present": bool(agg.component_evaluation_ids),
            "breakdown": agg.breakdown,
            "formula_declared": ir.aggregation.formula,
            # The aggregator now dispatches on `formula`: `seat_projection_sum`
            # routes to the Poisson-binomial seat path, everything else to the
            # 0..100 weighted average. `breakdown["formula"]` records which ran.
            "formula_used": agg.breakdown.get("formula", "weighted_average_x_100"),
        },
    )
