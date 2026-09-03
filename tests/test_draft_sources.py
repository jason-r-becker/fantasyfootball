from collections import Counter

import pytest

from fantasyfootball import draft_sources
from fantasyfootball.draft_state import DraftError


def test_espn_fetch_uses_lightweight_client_and_caches_player_map(monkeypatch):
    calls = Counter()

    class FakeRequests:
        def get_league_draft(self):
            calls["draft"] += 1
            return {
                "draftDetail": {
                    "picks": [
                        {
                            "overallPickNumber": 1,
                            "roundId": 1,
                            "roundPickNumber": 1,
                            "playerId": 99,
                            "teamId": 2,
                        }
                    ]
                }
            }

        def get_pro_players(self):
            calls["players"] += 1
            return [{"id": 99, "fullName": "Example Player"}]

    class FakeLeague:
        def __init__(self, **kwargs):
            calls["league"] += 1
            assert kwargs["fetch_league"] is False
            self.espn_request = FakeRequests()

    monkeypatch.setattr(draft_sources, "League", FakeLeague)
    draft_sources._ESPN_PLAYER_MAPS.pop(2097, None)
    config = {"league_id": "123", "teams": 12, "team_id": 2}

    first = draft_sources.fetch_espn_picks(config, 2097)
    second = draft_sources.fetch_espn_picks(config, 2097)

    assert first == second
    assert first[0]["player"] == "Example Player"
    assert first[0]["mine"] is True
    assert calls == {"league": 2, "draft": 2, "players": 1}


def test_espn_fetch_skips_player_request_before_draft(monkeypatch):
    calls = Counter()

    class FakeRequests:
        def get_league_draft(self):
            calls["draft"] += 1
            return {"draftDetail": {"picks": []}}

        def get_pro_players(self):
            calls["players"] += 1
            return []

    class FakeLeague:
        def __init__(self, **kwargs):
            assert kwargs["fetch_league"] is False
            self.espn_request = FakeRequests()

    monkeypatch.setattr(draft_sources, "League", FakeLeague)

    assert (
        draft_sources.fetch_espn_picks(
            {"league_id": "123", "teams": 12, "team_id": None}, 2098
        )
        == []
    )
    assert calls == {"draft": 1}


def test_espn_pick_without_team_id_is_not_marked_mine():
    picks = draft_sources.parse_espn_picks(
        {
            "draftDetail": {
                "picks": [
                    {
                        "overallPickNumber": 1,
                        "playerId": 99,
                        "teamId": 2,
                    }
                ]
            }
        },
        {99: "Example Player"},
        {"teams": 12, "team_id": None},
    )

    assert picks[0]["mine"] is False


def test_espn_fetch_does_not_expose_upstream_error_text(monkeypatch):
    cookie_value = "private-cookie-value"

    class FakeRequests:
        def get_league_draft(self):
            raise RuntimeError(f"access denied with espn_s2={cookie_value}")

    class FakeLeague:
        def __init__(self, **kwargs):
            self.espn_request = FakeRequests()

    monkeypatch.setattr(draft_sources, "League", FakeLeague)

    with pytest.raises(DraftError) as captured:
        draft_sources.fetch_espn_picks(
            {
                "league_id": "123",
                "teams": 12,
                "swid": "private-swid",
                "espn_s2": cookie_value,
            },
            2099,
        )

    assert cookie_value not in str(captured.value)
    assert "private-swid" not in str(captured.value)
    assert "Check the league ID" in str(captured.value)
