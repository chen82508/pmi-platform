"""Incumbency config loader — the seat→party reference behind HELD BY / flips."""

from pmi_core.engine.incumbency import incumbent_party, is_open_seat


def test_senate_known_seats():
    # Sourced from Wikipedia 2026 Senate elections (see config header).
    assert incumbent_party("senate", "TX", 2026) == "R"  # Cornyn seat
    assert incumbent_party("senate", "GA", 2026) == "D"  # Ossoff seat
    assert incumbent_party("senate", "FL", 2026) == "R"  # Rubio-vacated special
    assert incumbent_party("senate", "OH", 2026) == "R"  # Vance-vacated special


def test_governor_known_seats():
    assert incumbent_party("governor", "CA", 2026) == "D"
    assert incumbent_party("governor", "GA", 2026) == "R"  # Kemp (termed out)
    assert incumbent_party("governor", "NV", 2026) == "R"  # Lombardo


def test_case_insensitive_state():
    assert incumbent_party("senate", "tx", 2026) == "R"


def test_house_district_padding_normalized():
    # The board serves zero-padded district codes (AK-01); they must resolve to the
    # roster's unpadded AK-1, exactly like the aggregator's own race code does.
    assert incumbent_party("house", "AK-01", 2026) == incumbent_party("house", "AK-1", 2026) == "R"
    assert incumbent_party("house", "CA-03", 2026) == incumbent_party("house", "CA-3", 2026) == "R"  # Kiley
    assert incumbent_party("house", "CA-20", 2026) == "R"
    assert incumbent_party("house", "AL-02", 2026) == "D"  # post-Milligan majority-Black
    # 2-letter states (senate/gov) are unaffected by the normalization.
    assert incumbent_party("senate", "NC", 2026) == "R"


def test_house_compiled_per_district():
    # House IS compiled (119th Congress), keyed per-district (STATE-DISTRICT).
    assert incumbent_party("house", "CA-20", 2026) == "R"  # David Valadao
    assert incumbent_party("house", "CA-43", 2026) == "D"  # safe D (Compton)
    assert incumbent_party("house", "AK-1", 2026) == "R"   # at-large → district 1
    assert incumbent_party("house", "AL-2", 2026) == "D"   # post-Milligan majority-Black
    # A 2-letter state (no district) does NOT resolve a house seat — needs the code.
    assert incumbent_party("house", "TX", 2026) is None
    # An unlisted district → None (falls back to the thin read at the call site).
    assert incumbent_party("house", "ZZ-9", 2026) is None


def test_unknown_or_wrong_cycle_returns_none():
    assert incumbent_party("senate", "ZZ", 2026) is None
    assert incumbent_party("senate", "TX", 2030) is None
    assert incumbent_party("", "TX", 2026) is None


def test_open_seats_governor():
    # Term-limited / retiring governors → open (the zero-liquidity prior must not
    # assume the departing party holds; an open seat can flip).
    assert is_open_seat("governor", "KS", 2026) is True  # Kelly (D) term-limited
    assert is_open_seat("governor", "AK", 2026) is True  # Dunleavy (R) term-limited
    # A running incumbent → NOT open, so the incumbent-hold prior still applies.
    assert is_open_seat("governor", "NV", 2026) is False  # Lombardo running


def test_open_seats_senate():
    assert is_open_seat("senate", "NC", 2026) is True  # Tillis retiring
    assert is_open_seat("senate", "TX", 2026) is True  # Cornyn lost primary
    assert is_open_seat("senate", "NM", 2026) is False  # Luján running
    assert is_open_seat("senate", "WV", 2026) is False  # Capito running


def test_open_seat_case_insensitive_and_unknown():
    assert is_open_seat("governor", "ks", 2026) is True
    assert is_open_seat("house", "TX", 2026) is False  # house not compiled → never open
    assert is_open_seat("senate", "ZZ", 2026) is False
    assert is_open_seat("", "TX", 2026) is False
