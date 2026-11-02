"""Declarative PMI definition: YAML → Pydantic IR (`IndexDef`)."""

from pmi_core.dsl.ir import (
    AggregationSpec,
    CollapseSpec,
    FactorSpec,
    IndexDef,
    PublishSpec,
    SelectorSpec,
    WeightingSpec,
    load_index_def,
)

__all__ = [
    "AggregationSpec",
    "CollapseSpec",
    "FactorSpec",
    "IndexDef",
    "PublishSpec",
    "SelectorSpec",
    "WeightingSpec",
    "load_index_def",
]
