"""D6 — deterministic daily war summaries: template-from-numbers, omit-never-
invent, citations gated on real URLs and deduped across roles."""

from pmi_core.engine.war_summary import compose_region_summary, compose_world_summary


def _payload() -> dict:
    def contract(mid, title, vol, url="u"):
        return {"market_id": mid, "title": title, "venue": "polymarket",
                "yes_pct": 20.0, "volume_24h": vol, "url": f"{url}{mid}" if url else None}

    return {
        "world_heat": 13.4,
        "world_delta_24h": -0.41,
        "world_delta_7d": -2.94,
        "breadth_7d": {"up": 16, "down": 23, "flat": 14},
        "venue_volume": {"polymarket": 221000.0, "manifold": 351000.0, "metaculus": 114.0},
        "concentration": {"top4_share": 0.6825, "bottom30_share": 0.0369, "n_countries_priced": 47},
        "countries": {
            "iran": {"slug": "iran", "name": "Iran", "region": "middle-east", "heat": 40.5,
                     "delta_7d": 1.1, "volume_24h": 239000.0,
                     "top_contracts": [contract(10, "US x Iran?", 200000.0)]},
            "bahrain": {"slug": "bahrain", "name": "Bahrain", "region": "middle-east", "heat": 65.4,
                        "delta_7d": 61.4, "volume_24h": 500.0,
                        "top_contracts": [contract(11, "Bahrain unrest?", 500.0)]},
            "united-states": {"slug": "united-states", "name": "United States", "region": "americas",
                              "heat": 23.9, "delta_7d": -1.37, "volume_24h": 333000.0,
                              "top_contracts": [contract(10, "US x Iran?", 200000.0),
                                                contract(12, "US strike?", 90000.0)]},
            "chad": {"slug": "chad", "name": "Chad", "region": "africa", "heat": 3.0,
                     "delta_7d": None, "volume_24h": None,
                     "top_contracts": [{"market_id": 13, "title": "no url", "venue": "manifold",
                                        "yes_pct": 5.0, "volume_24h": 10.0, "url": None}]},
        },
        "regions": {
            "middle-east": {"region": "middle-east", "name": "Middle East", "heat": 34.8,
                            "delta_24h": -1.34, "delta_7d": 2.41, "volume_24h": 278000.0},
            "americas": {"region": "americas", "name": "Americas", "heat": 17.1,
                         "delta_24h": -0.28, "delta_7d": -2.36, "volume_24h": 352000.0},
            "africa": {"region": "africa", "name": "Africa", "heat": 3.3,
                       "delta_24h": -0.01, "delta_7d": -8.32, "volume_24h": 576.0},
        },
    }


def test_world_summary_facts_and_citations():
    s = compose_world_summary(_payload())
    text = " ".join(s["paragraphs"])
    # Reading + band + both move windows, worded from the payload's numbers.
    assert "13.4" in text and "calm" in text
    assert "down 0.4 over the last day" in text and "down 2.9 over the week" in text
    # Breadth, biggest mover (Bahrain +61.4), regional high/low.
    assert "16 countries priced higher over the week, 23 lower and 14 unchanged" in text
    assert "Bahrain, up 61.4 to 65.4" in text
    assert "Middle East carries the highest regional reading at 34.8" in text
    assert "Africa the lowest at 3.3" in text
    # Real-money book excludes play-money venues ($221K, not $572K).
    assert "$221K" in text and "572" not in text
    assert "68% of the country-attributed volume" in text
    # Citations: deepest book (US) first; market 10 deduped so the hottest
    # country's (Bahrain 11) and mover's roles stay distinct; url-less skipped.
    roles = {c["role"]: c["market_id"] for c in s["citations"]}
    assert roles["deepest book"] == 10
    assert roles["highest reading"] == 11
    assert all(c["url"] for c in s["citations"])


def test_world_summary_dedupes_shared_market_across_roles():
    p = _payload()
    # Make the mover also the deepest-book country → its only contract (10)
    # is taken by "deepest book"; the mover role falls to its next contract.
    p["countries"]["bahrain"]["delta_7d"] = 0.01  # mover becomes united-states
    s = compose_world_summary(p)
    ids = [c["market_id"] for c in s["citations"]]
    assert len(ids) == len(set(ids))  # never the same contract twice
    mover = next(c for c in s["citations"] if c["role"] == "biggest weekly move")
    assert mover["market_id"] == 12  # US's second contract, not a duplicate 10


def test_region_summary_rank_share_and_scope():
    s = compose_region_summary(_payload(), "middle-east")
    text = " ".join(s["paragraphs"])
    assert "Middle East reads 34.8" in text and "1st of the 3 regions" in text
    assert "up 2.4 over the week" in text
    # Breadth counts only the region's own countries (Iran up, Bahrain up).
    assert "2 countries priced higher" in text
    assert "Bahrain, up 61.4" in text
    # Share of the region-attributed book: 278K / (278K+352K+0.576K) ≈ 44%.
    assert "$278K" in text and "44%" in text


def test_missing_facts_are_omitted_never_invented():
    s = compose_world_summary({"world_heat": 20.0, "countries": {}, "regions": {}})
    text = " ".join(s["paragraphs"])
    assert "20.0" in text and "low" in text
    assert "book" not in text and "countries priced" not in text  # no fabricated facts
    assert s["citations"] == []
    assert compose_world_summary({"world_heat": None}) is None
    assert compose_region_summary(_payload(), "no-such-region") is None
