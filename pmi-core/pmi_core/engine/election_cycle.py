"""Election cycle detection — which even-year election a market resolves for.

The classification stage (``seat_mapping`` / ``extraction``) reads only the
market TITLE, but several venues phrase their per-race titles WITHOUT a year:

    kalshi  "Will Republicans win the Senate race in California?"   (ticker SENATECA-28-D)
    kalshi  "Will Republican win the House race for AZ-04?"          (ticker KXHOUSERACE-AZ04-26-D)

The cycle is only in the ticker / resolution date, not the title. Without it a
2028 California Senate market (no 2026 race exists there) lands in the 2026
index — the cross-cycle pollution that inflated maga-senate to 50 "states".

``election_cycle`` recovers the cycle from every signal a market carries, in
descending order of trust:

  1. an explicit 4-digit even year in the title  ("... in 2026")   — polymarket/forecastex
  2. kalshi's 2-digit year ticker segment        (``-26`` / ``-28``)
  3. forecastex's embedded MMDDYY election date   (``MXXME_110326_SB`` → 2026)
  4. ``closes_at`` folded to the nearest even election year         — last resort

Returns ``None`` when no signal resolves — callers FAIL OPEN (keep the market,
let the entity-level ``race_calendar`` gate catch phantom races) rather than
silently dropping, matching the platform's cold-start philosophy.

Pure-Python, no DB / network — unit-testable in isolation. Nothing imports this
yet; the classifier wires it in when the extraction inputs carry external_id /
closes_at (the plumbing change).
"""

from __future__ import annotations

import re
from datetime import datetime

# US general elections fall on even years. Accept 2024..2040 as plausible cycles
# so a stray "$2026" price or a 2-digit noise segment can't coin a real-looking
# cycle far outside the window we index.
_MIN_CYCLE = 2024
_MAX_CYCLE = 2040

# An explicit 4-digit cycle in the title: "2026 Senate", "... race in 2026".
_TITLE_YEAR_RE = re.compile(r"\b(20[2-4]\d)\b")

# A kalshi ticker year segment: a bare 2-digit token between hyphens or at the
# end. "SENATECA-28-D" → 28, "KXHOUSERACE-AZ04-26-D" → 26, "SENATEAL-28-2-D" →
# 28 (the trailing "-2" is a single digit, not a year). "AZ04" is not bare
# digits, so the state-district token is never mistaken for a year.
_TICKER_YEAR_RE = re.compile(r"-(\d{2})(?=-|$)")

# A forecastex embedded election date: "_MMDDYY_" ("MXXME_110326_SB" → 11/03/26,
# "G02TX_110326_DC" → 11/03/26). Only the YY tail is used for the cycle.
_TICKER_DATE_RE = re.compile(r"_(\d{2})(\d{2})(\d{2})_")


def _even_in_window(year: int) -> int | None:
    """The year itself if it's an even cycle in the accepted window, else None."""
    if year % 2 == 0 and _MIN_CYCLE <= year <= _MAX_CYCLE:
        return year
    return None


def _cycle_from_title(title: str | None) -> int | None:
    if not title:
        return None
    for m in _TITLE_YEAR_RE.finditer(title):
        cycle = _even_in_window(int(m.group(1)))
        if cycle is not None:
            return cycle
    return None


def _cycle_from_ticker(external_id: str | None) -> int | None:
    if not external_id:
        return None
    # 2-digit year segment (kalshi). Take the first that lands on an even cycle.
    for m in _TICKER_YEAR_RE.finditer(external_id):
        cycle = _even_in_window(2000 + int(m.group(1)))
        if cycle is not None:
            return cycle
    # Embedded MMDDYY date (forecastex).
    dm = _TICKER_DATE_RE.search(external_id)
    if dm is not None:
        cycle = _even_in_window(2000 + int(dm.group(3)))
        if cycle is not None:
            return cycle
    return None


def _cycle_from_close(closes_at: datetime | None) -> int | None:
    """Fold a resolution date to the even election year it settles.

    A general-election market resolves on or shortly after the November even-year
    vote, so the cycle is the even year in ``{closes.year, closes.year - 1}``:
    forecastex closes the election year itself (2026-11), kalshi a year later
    (2027-11-03 for a 2026 race, 2029-11-07 for 2028).
    """
    if closes_at is None:
        return None
    year = closes_at.year
    if year % 2 == 1:
        year -= 1
    return _even_in_window(year)


def election_cycle(
    venue: str | None,
    title: str | None,
    external_id: str | None,
    closes_at: datetime | None,
) -> int | None:
    """The even-year cycle a market resolves for, or None if undeterminable.

    ``venue`` is accepted for symmetry / future venue-specific rules but the
    signal order (title → ticker → date → closes_at) is venue-agnostic today: a
    ticker year is a ticker year whoever wrote it. None ⇒ fail open.
    """
    return (
        _cycle_from_title(title)
        or _cycle_from_ticker(external_id)
        or _cycle_from_close(closes_at)
    )
