"""Links in our own emails carry the newsletter UTM tags.

docs/utm-conventions.md §1: ``utm_source=newsletter``, ``utm_medium=email``,
and ``utm_campaign`` names the mailing (``weekly-report`` / ``new-index``).
Email clients send no referrer, so an untagged click is filed under Direct
in GA4. The per-subscriber unsubscribe link stays untagged: it is not a visit
worth attributing.
"""

from __future__ import annotations

UTM_SOURCE = "newsletter"
UTM_MEDIUM = "email"


def tagged(url: str, campaign: str) -> str:
    """``url`` with the newsletter UTM parameters appended (``?`` or ``&`` as needed)."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}utm_source={UTM_SOURCE}&utm_medium={UTM_MEDIUM}&utm_campaign={campaign}"
