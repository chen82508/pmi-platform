#!/usr/bin/env python3
"""
Auto-generate pmi-core/pmi_core/config/race_calendar.yaml from Wikipedia.

Data sources
------------
Senate   : https://en.wikipedia.org/wiki/Classes_of_United_States_senators
  Parses "States with a class N senator:" lines from each class section.
  Class 1 → election 2030 | Class 2 → 2026 | Class 3 → 2028

Governor : https://en.wikipedia.org/wiki/YYYY_United_States_gubernatorial_elections
  Parses "[[YYYY StateName gubernatorial election|StateName]]" links.
  2026 base page lists 36 states; remaining 4-year states inferred as 2028 cycle;
  5 odd-year states (KY/LA/MS/NJ/VA) are hardcoded because they don't appear
  on even-year pages.

Usage
-----
  python scripts/update_race_calendar.py
  python scripts/update_race_calendar.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

OUTPUT_PATH = Path(__file__).parent.parent / "pmi_core" / "config" / "race_calendar.yaml"

_STATE_MAP: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}

# Governor term lengths: 2-year for NH/VT only, 4-year for all others.
_GOV_PERIOD: dict[str, int] = {"NH": 2, "VT": 2}

# Odd-year states that never appear on even-year Wikipedia gubernatorial pages.
# Format: code → (next_election_year, period_years)
_ODD_YEAR_GOVERNORS: dict[str, tuple[int, int]] = {
    "KY": (2027, 4),
    "LA": (2027, 4),
    "MS": (2027, 4),
    "NJ": (2029, 4),  # 2025 already past → next is 2029
    "VA": (2029, 4),  # same
}


def _fetch_wikitext(page_title: str) -> str:
    params = urllib.parse.urlencode({
        "action": "parse",
        "page": page_title,
        "prop": "wikitext",
        "format": "json",
        "formatversion": "2",
    })
    url = f"https://en.wikipedia.org/w/api.php?{params}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "pmi-race-calendar/1.0 (pmi_data_platform)"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    if "error" in data:
        raise ValueError(f"Wikipedia API error for '{page_title}': {data['error']}")
    return data["parse"]["wikitext"]


# ---------------------------------------------------------------------------
# Senate
# ---------------------------------------------------------------------------

def fetch_senate_election_years() -> dict[str, list[int]]:
    """
    Returns {state_code: [election_year, ...]} for all 50 states × 2 seats.

    Source: "Classes of United States senators" — each class section lists
    which states' seats belong to that class.
    """
    print("  Fetching Classes of United States senators …")
    wikitext = _fetch_wikitext("Classes of United States senators")

    # Split wikitext into per-class sections on ==Class N== headings.
    class_sections: dict[str, str] = {}
    for m in re.finditer(r"==Class (\d)[^=]*==", wikitext):
        cls = m.group(1)
        start = m.end()
        nxt = re.search(r"\n==", wikitext[start:])
        end = start + (nxt.start() if nxt else len(wikitext[start:]))
        class_sections[cls] = wikitext[start:end]

    # Class 1 → senators elected in 2024 → next election 2030
    # Class 2 → senators elected in 2020 → next election 2026
    # Class 3 → senators elected in 2022 → next election 2028
    class_year = {"1": 2030, "2": 2026, "3": 2028}

    result: dict[str, list[int]] = {}
    for cls, section in class_sections.items():
        year = class_year.get(cls)
        if year is None:
            continue
        # Each state appears as: [[List of United States senators from X|X]]
        # or [[List of United States senators from X#Class N|X]]
        for m in re.finditer(
            r"\[\[List of United States senators from [^\]|]+\|([^\]#]+)\]\]",
            section,
        ):
            name = m.group(1).strip().lower()
            code = _STATE_MAP.get(name)
            if code:
                result.setdefault(code, [])
                if year not in result[code]:
                    result[code].append(year)

    n = len(result)
    missing = sorted(c for c in _STATE_MAP.values() if c not in result)
    print(f"  → {n} states parsed.{' Missing: ' + str(missing) if missing else ''}")
    return result


# ---------------------------------------------------------------------------
# Governor
# ---------------------------------------------------------------------------

def _parse_gov_states(wikitext: str, year: int) -> set[str]:
    """Extract states from '[[YEAR StateName gubernatorial election|StateName]]' links."""
    states: set[str] = set()
    for m in re.finditer(
        rf"\[\[{year}\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+gubernatorial\s+election\|",
        wikitext,
    ):
        code = _STATE_MAP.get(m.group(1).strip().lower())
        if code:
            states.add(code)
    return states


def fetch_governor_cycles() -> dict[str, dict]:
    """
    Returns {state_code: {next: year, period: years}} for all 50 states.

    Strategy:
    1. Fetch 2026 page → 36 states on the 2026 cycle.
    2. Odd-year states (KY/LA/MS/NJ/VA) are hardcoded — they never appear on
       even-year Wikipedia pages.
    3. Remaining states (had elections in 2024) → 2028 cycle.
    """
    print("  Fetching 2026 United States gubernatorial elections …")
    wt2026 = _fetch_wikitext("2026 United States gubernatorial elections")
    states_2026 = _parse_gov_states(wt2026, 2026)
    print(f"  → {len(states_2026)} states on 2026 cycle.")

    result: dict[str, dict] = {}

    # Odd-year states first.
    for code, (next_yr, period) in _ODD_YEAR_GOVERNORS.items():
        result[code] = {"next": next_yr, "period": period}

    # Even-year states.
    all_codes = set(_STATE_MAP.values())
    remaining = all_codes - set(_ODD_YEAR_GOVERNORS) - states_2026

    for code in all_codes:
        if code in result:
            continue
        period = _GOV_PERIOD.get(code, 4)
        if code in states_2026:
            result[code] = {"next": 2026, "period": period}
        else:
            # Not in 2026 list → elections in 2024 cycle → next is 2028.
            result[code] = {"next": 2028, "period": period}

    if remaining:
        print(f"  → {len(remaining)} states inferred as 2028 cycle: {sorted(remaining)}")

    return result


# ---------------------------------------------------------------------------
# YAML output
# ---------------------------------------------------------------------------

def _format_yaml(senate: dict[str, list[int]], governor: dict[str, dict]) -> str:
    lines = [
        "# Auto-generated by scripts/update_race_calendar.py",
        "# Sources:",
        "#   Senate  : https://en.wikipedia.org/wiki/Classes_of_United_States_senators",
        "#   Governor: https://en.wikipedia.org/wiki/2026_United_States_gubernatorial_elections",
        "# Re-run after any special election or appointment.",
        "",
        "senate:",
        "  # state_code: [election_year, ...]",
        "  # Each state has exactly 2 entries (one per senate seat, in different class years).",
    ]
    for code in sorted(senate):
        years = sorted(senate[code])
        lines.append(f"  {code}: {years}")

    lines += [
        "",
        "governor:",
        "  # state_code: {next: year, period: years}",
        "  # 'next' = next scheduled election (from the generation date).",
        "  # 'period' = term length (2 = NH/VT; 4 = all others).",
        "  # Odd-year states: KY/LA/MS (2027 cycle), NJ/VA (2029 cycle).",
    ]
    for code in sorted(governor):
        info = governor[code]
        lines.append(f"  {code}: {{next: {info['next']}, period: {info['period']}}}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print YAML but do not write.")
    args = parser.parse_args()

    print("=== update_race_calendar.py ===")
    print("Senate:")
    senate = fetch_senate_election_years()
    print("Governor:")
    governor = fetch_governor_cycles()

    yaml_text = _format_yaml(senate, governor)

    if args.dry_run:
        print("\n--- Generated YAML (dry-run) ---")
        print(yaml_text)
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(yaml_text)
    print(f"\n✓ Written to {OUTPUT_PATH}")
    print(f"  Senate entries  : {len(senate)}")
    print(f"  Governor entries: {len(governor)}")


if __name__ == "__main__":
    main()
