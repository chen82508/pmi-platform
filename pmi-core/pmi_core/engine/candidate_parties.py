"""Curated candidate → party overrides for election legs (pmi_core/config/
candidate_parties.yaml).

A candidate-name leg ("Will Justin Murphy win the New Jersey general election
for US Senate in 2026?") carries no party in its title, so the classifier
resolves it from Polymarket's groupItemTitle tag, the audited
``republican_on_yes`` factor, or the LLM extraction's ``party``. Those hints are
occasionally wrong (gpt-4o-mini filed New Jersey's Republican Justin Murphy as a
Democrat on three venues, and folded Idaho's independent Todd Achilles in as a
Republican), which flips that leg's contribution to P(R). This table is the
human correction layer — highest precedence, never a guess: a name is listed
only after a venue leg was observed mis-partied.

Static reference data read at classification time; ``"I"`` means independent /
third-party, which the classifier drops (there is no party to credit).
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import yaml as _yaml_mod

    _YAML_OK = True
except ImportError:  # pragma: no cover - yaml is a hard dep of pmi-core
    _YAML_OK = False

_PATH = Path(__file__).parent.parent / "config" / "candidate_parties.yaml"
_cache: dict | None = None

# Trailing party tag on a label ("Jon Husted (R)") — stripped before lookup so
# a venue that both names the party and misspells it still resolves.
_SUFFIX_RE = re.compile(r"\s*\((?:[A-Za-z]+)(?:[-–][A-Z]{2})?\)\s*$")


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    if not _PATH.exists() or not _YAML_OK:
        _cache = {}
        return _cache
    with open(_PATH) as f:
        _cache = _yaml_mod.safe_load(f) or {}
    return _cache


def normalize_name(label: str | None) -> str:
    """Lower-cased, whitespace-collapsed candidate name without a party suffix."""
    if not label:
        return ""
    return re.sub(r"\s+", " ", _SUFFIX_RE.sub("", label)).strip().lower()


def _office_of(key: str) -> tuple[str, str]:
    """(office, state-key) for an entity key: 'California-11' → house, else
    the state-keyed offices are tried in order (senate, governor)."""
    if "-" in key and key.rsplit("-", 1)[1].isdigit():
        return "house", key
    return "", key


def override_party(label: str | None, key: str, office: str | None = None) -> str | None:
    """Curated party for ``label`` in the race ``key``, or None when unlisted.

    ``office`` narrows the lookup (``senate`` / ``governor`` / ``house``); when
    omitted a house key ("California-11") implies house and a state key is
    looked up under senate then governor. Returns "R" / "D" / "I".
    """
    name = normalize_name(label)
    if not name:
        return None
    table = _load()
    inferred, race_key = _office_of(key)
    offices = [office] if office else ([inferred] if inferred else ["senate", "governor"])
    for off in offices:
        by_state = table.get(off) or {}
        party = (by_state.get(race_key) or {}).get(name)
        if party in ("R", "D", "I"):
            return party
    return None
