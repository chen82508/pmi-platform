"""Deterministic votes derived from market structure, not from a model.

The point of these is narrow but important: they are the only labeller in the
protocol that cannot be wrong *in the same direction* as the models. A
model-only gold set is circular — you are validating an LLM's notion of
membership with another LLM's notion of membership — and the usual fix is a
human. A structural rule is the cheap substitute: it is derived from title
syntax and venue metadata, so where it disagrees with the models the item is
genuinely contested and gets flagged rather than silently accepted.

They abstain freely. ``None`` means "this rule has no opinion", which is the
common case and is much better than guessing: an abstention costs nothing,
whereas a confident wrong structural vote would poison the consensus.

``btc_price_membership`` reuses :func:`pmi_core.engine.moneyness.extract_strike`
rather than re-implementing dollar parsing, so the gold set and the production
moneyness weighting agree about what "has a strike" means.
"""

from __future__ import annotations

import re

from pmi_core.engine.moneyness import extract_strike

# BTC named in the title at a word boundary. `\bbtc\b` rather than substring so
# "BTCUSD-ish" tickers and words containing the letters don't trip it.
_BTC_RE = re.compile(r"\b(bitcoin|btc)\b", re.IGNORECASE)

# The negative classes the rubric (prompts/factors/btc_price_relevance-v1.md)
# enumerates: Bitcoin is the subject but the DOLLAR PRICE does not settle it.
# Each pattern here is a paraphrase of a bullet in that rubric — when the rubric
# changes, these change with it, and the gold version bumps.
_NOT_PRICE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mention", re.compile(r"\b(say|says|mention|mentions|tweet|tweets|post|posts)\b", re.I)),
    ("treasury", re.compile(r"\b(treasury|reserve|holdings?|adopt|adoption|buy[s]? bitcoin|add .{0,20}\bbtc\b)\b", re.I)),
    ("regulation", re.compile(r"\b(etf|sec\b|regulat|approv|ban|legal tender|lawsuit|court)\b", re.I)),
    ("network", re.compile(r"\b(hash ?rate|halving|mempool|difficulty|transaction fees?|node[s]?|miner[s]?|mining)\b", re.I)),
    ("dominance", re.compile(r"\b(dominance|market ?cap|flippening|vs\.? ?eth|ratio)\b", re.I)),
)

# Title shapes that ARE a price question even without a "$" strike.
_PRICE_SHAPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(up or down|higher or lower)\b", re.I),
    re.compile(r"\ball[- ]time high\b", re.I),
    re.compile(r"\b(price|close[sd]?|trade[sd]?)\b.{0,30}\b(above|below|between|exceed|reach|hit)\b", re.I),
    re.compile(r"\b(above|below|exceed|reach|hit)\b.{0,20}\$", re.I),
)


def btc_price_membership(title: str | None, description: str | None = None) -> bool | None:
    """Structural vote on "does this resolve on BTC's spot price".

    Returns True / False / None (abstain). The logic is intentionally
    conservative and mirrors the rubric's own tie-break ("when in doubt about
    whether the dollar price settles it, prefer 0") — except that here doubt
    produces an abstention rather than a 0, because a gold vote and a
    production judgment have different costs of being wrong.
    """
    if not title:
        return None
    text = title if not description else f"{title}\n{description}"
    if not _BTC_RE.search(text):
        # Not a Bitcoin market at all. Confident negative for this taxonomy —
        # this is what makes the rule useful on the corpus_random stratum,
        # where nearly every item is trivially out of scope.
        return False

    for _name, pat in _NOT_PRICE_PATTERNS:
        if pat.search(text):
            # An excluded class is named. Do not try to adjudicate a title that
            # is BOTH ("Will the ETF approval push BTC above $80k") — abstain
            # and let the models argue about it.
            if extract_strike(title) is not None or _has_price_shape(title):
                return None
            return False

    if extract_strike(title) is not None:
        return True
    if _has_price_shape(text):
        return True
    return None


def _has_price_shape(text: str) -> bool:
    return any(p.search(text) for p in _PRICE_SHAPE_PATTERNS)


def btc_major_move(title: str | None, description: str | None = None) -> bool | None:
    """No reliable structural signal for "is this a substantive move".

    Kept as an explicit abstain rather than omitted so the protocol can ask
    every registered question uniformly, and so it is obvious that this factor
    has NO non-model check behind it — its gold labels rest on model consensus
    alone and should be read with that caveat.
    """
    return None


def btc_direction(title: str | None, description: str | None = None) -> float | None:
    """Structural vote on what a YES resolution means for price.

    The dominant BTC market shapes across our venues are explicitly directional
    ("above $X", "exceed $X", "all time high" → YES means up; "below $X" → YES
    means down), which the index def itself documents. Range bins ("between $X
    and $Y") carry no direction and abstain.
    """
    if not title:
        return None
    text = title if not description else f"{title}\n{description}"
    if not _BTC_RE.search(text):
        return None
    lowered = text.lower()
    if re.search(r"\bbetween\b.{0,40}\band\b", lowered) or re.search(
        r"[0-9]\s*(?:to|–|—)\s*\$?[0-9]", lowered
    ):
        return None  # range bin — no directional meaning
    up = re.search(r"\b(above|exceed|reach|hit|higher|all[- ]time high|greater than|or more|or above)\b", lowered)
    down = re.search(r"\b(below|under|less than|lower|drop|crash|fall|or less)\b", lowered)
    if up and not down:
        return 1.0
    if down and not up:
        return -1.0
    return None


# ── S&P 500 ────────────────────────────────────────────────────────────────
# Same three questions as BTC, one structural difference that matters: BTC
# strikes are dollar amounts and the index def enables the moneyness parser on
# them, so `btc_price_membership` can lean on `extract_strike`. S&P strikes are
# bare index points ("above 6845.5", "below 4000"), which `extract_strike`
# cannot read — the def disables moneyness for exactly that reason. So the
# price-shape detection here has to work off the comparison words and a bare
# number rather than a `$`.
_SP500_RE = re.compile(r"\b(s&p ?500|s ?& ?p|spx|sp500|spy)\b", re.IGNORECASE)

# Negative classes, one per bullet in prompts/factors/sp500_price_relevance-v1.md.
_SP500_NOT_PRICE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ranking", re.compile(r"\b(best|worst) (performance|performing)\b|\boutperform|\bversus\b|\bvs\.? (gold|bitcoin|nasdaq|dow)\b", re.I)),
    ("membership", re.compile(r"\b(added to|removed from|join|joins|dropped from|inclusion in|added into)\b", re.I)),
    ("valuation", re.compile(r"\b(p/e|price[- ]to[- ]earnings|earnings|total market cap|valuation|dividend)\b", re.I)),
    ("macro_basket", re.compile(r"\b(recession|unemployment|cpi|inflation|fed funds|yield curve)\b", re.I)),
)

# Title shapes that ARE an index-level question. A bare number after a
# comparison word counts here (unlike BTC, where `$` is required) because that
# is how every kalshi S&P strike is written.
#
# The digit counts are load-bearing, not defensive padding. "exceed 30" is a P/E
# ratio and "below 5" is a percentage; the S&P 500 has not closed under 1000
# since 1998, so an index-level threshold written without a `$` is always four
# digits or more. Allowing a bare two-digit number here made the rule abstain on
# every valuation market instead of rejecting them.
_CMP = r"(?:above|below|between|exceed|exceeds|reach|reaches|hit|hits)"
_SP500_PRICE_SHAPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(up or down|higher or lower)\b", re.I),
    re.compile(r"\ball[- ]time high\b", re.I),
    # Dollar-quoted (polymarket SPY/SPX): 3+ digits, since SPY trades near $700.
    re.compile(rf"\b{_CMP}\b\s*\(?\s*(?:HIGH|LOW)?\s*\)?\s*\$\s*[0-9][0-9,]{{2,}}", re.I),
    # Bare index points (kalshi ladders): 4+ digits.
    re.compile(rf"\b{_CMP}\b\s*[0-9][0-9,]{{3,}}(?:\.[0-9]+)?", re.I),
    re.compile(r"\((?:HIGH|LOW)\)"),  # polymarket "hit (LOW) $720" candle markets
    re.compile(r"\b(close[sd]?|closing|end|finish)\b.{0,25}\b(above|below|higher|lower)\b", re.I),
)


def sp500_price_membership(title: str | None, description: str | None = None) -> bool | None:
    """Structural vote on "does this resolve on the S&P 500's index level"."""
    if not title:
        return None
    text = title if not description else f"{title}\n{description}"
    if not _SP500_RE.search(text):
        # Not an S&P market at all — the confident negative that makes this rule
        # useful on the corpus_random stratum.
        return False

    for _name, pat in _SP500_NOT_PRICE_PATTERNS:
        if pat.search(text):
            # Both an excluded class AND a level question ("will a recession
            # push the S&P below 4000") — abstain, let the models argue.
            if _has_sp500_price_shape(title):
                return None
            return False

    if _has_sp500_price_shape(text):
        return True
    return None


def _has_sp500_price_shape(text: str) -> bool:
    return any(p.search(text) for p in _SP500_PRICE_SHAPE_PATTERNS)


def sp500_major_move(title: str | None, description: str | None = None) -> bool | None:
    """No structural signal for "is this a substantive move" — see btc_major_move."""
    return None


def sp500_direction(title: str | None, description: str | None = None) -> float | None:
    """Structural vote on what a YES resolution means for the index level."""
    if not title:
        return None
    text = title if not description else f"{title}\n{description}"
    if not _SP500_RE.search(text):
        return None
    lowered = text.lower()
    if re.search(r"\bbetween\b.{0,40}\band\b", lowered) or re.search(
        r"[0-9]\s*(?:to|–|—)\s*\$?[0-9]", lowered
    ):
        return None  # range bin — no directional meaning
    # "(HIGH)" / "(LOW)" is polymarket's candle notation and is the direction
    # itself, so it is checked before the generic words: "hit (LOW) $720" has
    # "hit" in it and would otherwise read as bullish.
    if re.search(r"\(\s*low\s*\)", lowered):
        return -1.0
    if re.search(r"\(\s*high\s*\)", lowered):
        return 1.0
    up = re.search(r"\b(above|exceed|exceeds|reach|reaches|hit|hits|higher|all[- ]time high|greater than|or more|or above|gain)\b", lowered)
    down = re.search(r"\b(below|under|less than|lower|drop|crash|fall|decline|correction|or less)\b", lowered)
    if up and not down:
        return 1.0
    if down and not up:
        return -1.0
    return None


# Registry so the labeller can ask for a taxonomy's structural votes without
# knowing which functions exist. A question absent here simply has no
# structural check.
#
# `gdp_us_growth_membership` is deliberately ABSENT. The obvious deterministic
# rule for it is `gdp_board.is_us` + `anchor_of`, which is the system under
# test — using it as the protocol's independent vote would be pure circularity,
# scoring the regex chain against itself. GDP's gold therefore rests on model
# consensus alone; `--exclude-labeller` cannot rescue it after the fact, so the
# two labellers have to be genuinely independent of each other.
STRUCTURAL_VOTERS = {
    "btc_price_relevance": btc_price_membership,
    "btc_major_move": btc_major_move,
    "btc_direction": btc_direction,
    "sp500_price_relevance": sp500_price_membership,
    "sp500_major_move": sp500_major_move,
    "sp500_direction": sp500_direction,
}


def structural_vote(question: str, title: str | None, description: str | None) -> float | None:
    fn = STRUCTURAL_VOTERS.get(question)
    if fn is None:
        return None
    out = fn(title, description)
    if out is None:
        return None
    if isinstance(out, bool):
        return 1.0 if out else 0.0
    return float(out)


__all__ = [
    "STRUCTURAL_VOTERS",
    "btc_direction",
    "btc_major_move",
    "btc_price_membership",
    "sp500_direction",
    "sp500_major_move",
    "sp500_price_membership",
    "structural_vote",
]
