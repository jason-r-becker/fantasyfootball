import pytest

from fantasyfootball.player_matching import (
    PlayerMatcher,
    PlayerMatchError,
    normalize_player_name,
    normalize_position,
)


def test_name_normalization_handles_suffixes_punctuation_and_accents():
    assert normalize_player_name("Marvin Harrison Jr.") == "marvinharrison"
    assert normalize_player_name("D'Andre Swift") == "dandreswift"
    assert normalize_player_name("José Núñez III") == "josenunez"
    assert normalize_position("PK") == "K"
    assert normalize_position("D/ST") == "DST"


def test_exact_and_confirmed_aliases_share_attribute_safeguards():
    matcher = PlayerMatcher(
        [
            {"Player": "Marvin Harrison Jr.", "Position": "WR", "Team": "ARI"},
            {"Player": "Example Runner", "Position": "RB", "Team": "JAX"},
        ],
        {"M. Harrison": "Marvin Harrison Jr."},
    )

    exact = matcher.match("Marvin Harrison", position="WR", team="ARI")
    alias = matcher.match("M. Harrison", position="WR", team="ARI")
    team_alias = matcher.match("Example Runner", position="RB", team="JAC")

    assert exact.canonical == "Marvin Harrison Jr."
    assert exact.method == "exact"
    assert alias.canonical == "Marvin Harrison Jr."
    assert alias.method == "alias"
    assert team_alias.canonical == "Example Runner"


@pytest.mark.parametrize(
    ("source", "canonical"),
    [
        ("SFO", "SF"),
        ("KCC", "KC"),
        ("NOS", "NO"),
        ("GBP", "GB"),
        ("NEP", "NE"),
        ("LVR", "LV"),
        ("TBB", "TB"),
    ],
)
def test_projection_team_code_variants_match_ffc(source, canonical):
    matcher = PlayerMatcher(
        [{"Player": "Example Player", "Position": "WR", "Team": canonical}]
    )

    assert (
        matcher.match("Example Player", position="WR", team=source).canonical
        == "Example Player"
    )


def test_conflicts_and_unmatched_names_are_never_silent_matches():
    matcher = PlayerMatcher(
        [{"Player": "Example Player", "Position": "WR", "Team": "AAA"}]
    )

    position = matcher.match("Example Player", position="RB", team="AAA")
    team = matcher.match("Example Player", position="WR", team="BBB")
    missing = matcher.match("Different Player", position="WR", team="AAA")

    assert position.canonical is None
    assert "position conflict" in position.reason
    assert team.canonical is None
    assert "team conflict" in team.reason
    assert missing.canonical is None
    assert missing.reason == "unmatched name"


def test_duplicate_canonical_names_and_alias_collisions_are_rejected():
    with pytest.raises(PlayerMatchError, match="normalize to the same"):
        PlayerMatcher(
            [
                {"Player": "John Doe", "Position": "WR"},
                {"Player": "John Doe Jr.", "Position": "WR"},
            ]
        )

    with pytest.raises(PlayerMatchError, match="conflicts with canonical"):
        PlayerMatcher(
            [
                {"Player": "John Doe", "Position": "WR"},
                {"Player": "Another Player", "Position": "RB"},
            ],
            {"John Doe Jr.": "Another Player"},
        )

    with pytest.raises(PlayerMatchError, match="appears more than once"):
        PlayerMatcher(
            [
                {"Player": "Duplicate Player", "Position": "WR"},
                {"Player": "Duplicate Player", "Position": "WR"},
            ]
        )
