import json
from datetime import UTC, datetime, timedelta

import pytest

from fantasyfootball import adp_model
from fantasyfootball.adp_model import (
    ADPModelError,
    PlayerDraftDistribution,
    legacy_probability,
    load_adp_model,
    probability_available,
)


def ffc_payload():
    return {
        "meta": {
            "type": "PPR",
            "teams": 12,
            "rounds": 15,
            "total_drafts": 7500,
            "start_date": "2026-08-27",
            "end_date": "2026-09-03",
        },
        "players": [
            {
                "name": "Alpha Runner",
                "adp": 20.0,
                "stdev": 4.0,
                "times_drafted": 800,
                "high": 8,
                "low": 34,
                "team": "AAA",
                "position": "RB",
                "bye": 12,
            },
            {
                "name": "No Variance",
                "adp": 40.0,
                "stdev": 0,
                "times_drafted": 100,
            },
        ],
    }


def test_loads_ffc_model_and_reuses_fresh_private_cache(tmp_path, monkeypatch):
    calls = []

    def request(scoring_format, teams, year):
        calls.append((scoring_format, teams, year))
        return ffc_payload()

    monkeypatch.setattr(adp_model, "_request_ffc", request)
    config = {
        "teams": 12,
        "adp_model": {"source": "ffc", "format": "PPR"},
    }

    first = load_adp_model(config, 2026, tmp_path)
    second = load_adp_model(config, 2026, tmp_path)

    assert calls == [("ppr", 12, 2026)]
    assert first.status == "live"
    assert second.status == "cache"
    assert first.total_drafts == 7500
    assert first.players[0].samples == 800
    assert [player.name for player in first.players] == [
        "Alpha Runner",
        "No Variance",
    ]
    assert first.players[0].team == "AAA"
    assert first.players[0].position == "RB"
    assert first.players[0].bye == 12
    assert first.players[1].stdev == 0
    assert first.players[1].bye is None
    wrapper = json.loads((tmp_path / ".ffc_adp.json").read_text())
    assert datetime.fromisoformat(wrapper["fetched_at"]).tzinfo == UTC
    assert wrapper["format"] == "ppr"
    assert wrapper["teams"] == 12
    assert wrapper["year"] == 2026


def test_ffc_request_receives_no_platform_identifiers(tmp_path, monkeypatch):
    calls = []

    def request(scoring_format, teams, year):
        calls.append((scoring_format, teams, year))
        return ffc_payload()

    monkeypatch.setattr(adp_model, "_request_ffc", request)
    config = {
        "teams": 12,
        "site": "ESPN",
        "league_id": "private-league",
        "draft_id": "private-draft",
        "user_id": "private-user",
        "swid": "private-swid",
        "espn_s2": "private-cookie",
        "adp_model": {"source": "ffc", "format": "ppr"},
    }

    load_adp_model(config, 2026, tmp_path)

    assert calls == [("ppr", 12, 2026)]


def test_ffc_outage_is_nonfatal_without_a_cache(tmp_path, monkeypatch):
    def unavailable(*args):
        raise RuntimeError("offline")

    monkeypatch.setattr(adp_model, "_request_ffc", unavailable)
    model = load_adp_model(
        {"teams": 12, "adp_model": {"format": "half-ppr"}},
        2026,
        tmp_path,
    )

    assert model.enabled is True
    assert model.available is False
    assert model.status == "unavailable"
    assert "offline" in model.message.lower()


def test_ffc_outage_uses_matching_stale_cache(tmp_path, monkeypatch):
    wrapper = {
        "format": "ppr",
        "teams": 12,
        "year": 2026,
        "fetched_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "payload": ffc_payload(),
    }
    (tmp_path / ".ffc_adp.json").write_text(json.dumps(wrapper))

    def unavailable(*args):
        raise RuntimeError("offline")

    monkeypatch.setattr(adp_model, "_request_ffc", unavailable)
    model = load_adp_model(
        {"teams": 12, "adp_model": {"format": "ppr"}},
        2026,
        tmp_path,
    )

    assert model.available is True
    assert model.status == "stale-cache"
    assert model.players[0].name == "Alpha Runner"


def test_offline_mode_uses_cache_without_request(tmp_path, monkeypatch):
    wrapper = {
        "format": "ppr",
        "teams": 12,
        "year": 2026,
        "fetched_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "payload": ffc_payload(),
    }
    (tmp_path / ".ffc_adp.json").write_text(json.dumps(wrapper))

    def should_not_request(*args):
        raise AssertionError("offline mode contacted FFC")

    monkeypatch.setattr(adp_model, "_request_ffc", should_not_request)
    model = load_adp_model(
        {
            "teams": 12,
            "adp_model": {"format": "ppr", "offline": True},
        },
        2026,
        tmp_path,
    )

    assert model.status == "offline-cache"
    assert model.available is True


def test_cache_scope_mismatch_is_not_reused(tmp_path, monkeypatch):
    wrapper = {
        "format": "half-ppr",
        "teams": 10,
        "year": 2025,
        "fetched_at": datetime.now(UTC).isoformat(),
        "payload": ffc_payload(),
    }
    (tmp_path / ".ffc_adp.json").write_text(json.dumps(wrapper))
    calls = []

    def request(scoring_format, teams, year):
        calls.append((scoring_format, teams, year))
        return ffc_payload()

    monkeypatch.setattr(adp_model, "_request_ffc", request)
    model = load_adp_model(
        {"teams": 12, "adp_model": {"format": "ppr"}},
        2026,
        tmp_path,
    )

    assert model.status == "live"
    assert calls == [("ppr", 12, 2026)]


def test_future_dated_cache_is_not_trusted(tmp_path, monkeypatch):
    wrapper = {
        "format": "ppr",
        "teams": 12,
        "year": 2026,
        "fetched_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "payload": ffc_payload(),
    }
    (tmp_path / ".ffc_adp.json").write_text(json.dumps(wrapper))
    calls = []

    def request(scoring_format, teams, year):
        calls.append((scoring_format, teams, year))
        return ffc_payload()

    monkeypatch.setattr(adp_model, "_request_ffc", request)

    model = load_adp_model(
        {"teams": 12, "adp_model": {"format": "ppr"}},
        2026,
        tmp_path,
    )

    assert model.status == "live"
    assert calls == [("ppr", 12, 2026)]


@pytest.mark.parametrize(
    "wrapper",
    [
        "not-json",
        json.dumps({"format": "ppr"}),
        json.dumps(
            {
                "format": "ppr",
                "teams": 12,
                "year": 2026,
                "fetched_at": datetime.now(UTC).isoformat(),
                "payload": {"meta": {}, "players": "invalid"},
            }
        ),
    ],
)
def test_corrupted_cache_is_ignored_safely(tmp_path, monkeypatch, wrapper):
    (tmp_path / ".ffc_adp.json").write_text(wrapper)
    monkeypatch.setattr(
        adp_model,
        "_request_ffc",
        lambda *args: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    model = load_adp_model(
        {"teams": 12, "adp_model": {"format": "ppr"}},
        2026,
        tmp_path,
    )

    assert model.status == "unavailable"
    assert model.available is False


def test_failed_cache_write_does_not_discard_live_model(tmp_path, monkeypatch):
    monkeypatch.setattr(adp_model, "_request_ffc", lambda *args: ffc_payload())
    monkeypatch.setattr(
        adp_model,
        "_write_cache",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read only")),
    )

    model = load_adp_model(
        {"teams": 12, "adp_model": {"format": "ppr"}},
        2026,
        tmp_path,
    )

    assert model.status == "live"
    assert model.available is True
    assert not (tmp_path / ".ffc_adp.json").exists()


def test_atomic_cache_write_failure_preserves_old_file_and_removes_temp(
    tmp_path, monkeypatch
):
    cache = tmp_path / ".ffc_adp.json"
    cache.write_text("previous cache", encoding="utf-8")

    def fail_dump(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(adp_model.json, "dump", fail_dump)

    with pytest.raises(OSError, match="disk full"):
        adp_model._write_cache(
            cache,
            ffc_payload(),
            datetime.now(UTC),
            scoring_format="ppr",
            teams=12,
            year=2026,
        )

    assert cache.read_text(encoding="utf-8") == "previous cache"
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.parametrize(
    ("configured", "normalized", "response_type"),
    [
        ("PPR", "ppr", "PPR"),
        ("half", "half-ppr", "Half PPR"),
        ("halfppr", "half-ppr", "Half-PPR"),
        ("half_ppr", "half-ppr", "Half-PPR"),
        ("non-ppr", "standard", "Standard"),
        ("nonppr", "standard", "Standard"),
        ("2qb", "2-qb", "2QB"),
    ],
)
def test_scoring_format_aliases_are_normalized(
    tmp_path, monkeypatch, configured, normalized, response_type
):
    payload = ffc_payload()
    payload["meta"]["type"] = response_type
    calls = []

    def request(scoring_format, teams, year):
        calls.append((scoring_format, teams, year))
        return payload

    monkeypatch.setattr(adp_model, "_request_ffc", request)

    model = load_adp_model(
        {"teams": 12, "adp_model": {"format": configured}},
        2026,
        tmp_path,
    )

    assert model.scoring_format == normalized
    assert calls == [(normalized, 12, 2026)]


def test_adp_model_requires_explicit_format_or_disabled_source(tmp_path):
    with pytest.raises(ADPModelError, match="adp_model is required"):
        load_adp_model({"teams": 12}, 2026, tmp_path)

    disabled = load_adp_model(
        {"teams": 12, "adp_model": {"source": "disabled"}},
        2026,
        tmp_path,
    )
    assert disabled.enabled is False


@pytest.mark.parametrize(
    "payload",
    [
        {"meta": {}, "players": "not-a-list"},
        {
            "meta": {"teams": 12, "type": "Standard"},
            "players": ffc_payload()["players"],
        },
        {
            "meta": {"teams": 10, "type": "PPR"},
            "players": ffc_payload()["players"],
        },
        {
            "meta": {"teams": 12, "type": "PPR"},
            "players": [{"name": "Missing Statistics"}],
        },
        {
            "meta": {"type": "PPR"},
            "players": ffc_payload()["players"],
        },
        {
            "meta": {"teams": 12},
            "players": ffc_payload()["players"],
        },
        {
            "meta": {"teams": 12, "type": "PPR"},
            "players": [
                ffc_payload()["players"][0],
                {
                    **ffc_payload()["players"][0],
                    "name": "Alpha Runner Jr.",
                },
            ],
        },
    ],
)
def test_invalid_source_or_player_schema_falls_back_safely(
    tmp_path, monkeypatch, payload
):
    monkeypatch.setattr(adp_model, "_request_ffc", lambda *args: payload)

    model = load_adp_model(
        {"teams": 12, "adp_model": {"format": "ppr"}},
        2026,
        tmp_path,
    )

    assert model.status == "unavailable"
    assert model.available is False


def test_invalid_partial_player_rows_are_skipped_when_valid_rows_remain(
    tmp_path, monkeypatch
):
    payload = ffc_payload()
    payload["players"].extend(
        [
            "not-an-object",
            {"name": "No ADP", "times_drafted": 10},
            {"name": "Zero ADP", "adp": 0, "times_drafted": 10},
            {
                "name": "Negative Spread",
                "adp": 50,
                "stdev": -1,
                "times_drafted": 10,
            },
            {"name": "No Samples", "adp": 50, "stdev": 2},
            {
                "name": "Example Kicker",
                "adp": 150,
                "stdev": 20,
                "times_drafted": 100,
                "position": "PK",
            },
            {
                "name": "Example Defense",
                "adp": 151,
                "stdev": 20,
                "times_drafted": 100,
                "position": "DEF",
            },
        ]
    )
    monkeypatch.setattr(adp_model, "_request_ffc", lambda *args: payload)

    model = load_adp_model(
        {"teams": 12, "adp_model": {"format": "ppr"}},
        2026,
        tmp_path,
    )

    assert [player.name for player in model.players] == [
        "Alpha Runner",
        "No Variance",
    ]


@pytest.mark.parametrize("scoring_format", ["", "points-per-first-down"])
def test_invalid_ffc_format_is_rejected(tmp_path, scoring_format):
    with pytest.raises(ADPModelError, match="adp_model.format"):
        load_adp_model(
            {
                "teams": 12,
                "adp_model": {"source": "ffc", "format": scoring_format},
            },
            2026,
            tmp_path,
        )


def test_distribution_probability_is_monotonic_and_sample_aware():
    reliable = PlayerDraftDistribution(
        name="Alpha Runner", mean=20.0, stdev=4.0, samples=1000
    )
    sparse = PlayerDraftDistribution(
        name="Alpha Runner", mean=20.0, stdev=4.0, samples=1
    )
    threshold = PlayerDraftDistribution(
        name="Alpha Runner", mean=20.0, stdev=4.0, samples=25
    )

    early = probability_available(reliable, 15, fallback_adp=20.0, teams=12)
    middle = probability_available(reliable, 20, fallback_adp=20.0, teams=12)
    late = probability_available(reliable, 25, fallback_adp=20.0, teams=12)
    assert 1 > early > middle > late > 0
    assert probability_available(
        threshold, 25, fallback_adp=20.0, teams=12
    ) == pytest.approx(late)

    fallback = legacy_probability(20.0, 25, 12)
    sparse_result = probability_available(
        sparse, 25, fallback_adp=20.0, teams=12
    )
    assert abs(sparse_result - fallback) < abs(late - fallback)


def test_probability_boundaries_continuity_and_observed_range_are_sound():
    distribution = PlayerDraftDistribution(
        name="Alpha Runner",
        mean=20.0,
        stdev=4.0,
        samples=25,
        earliest=12,
        latest=24,
    )

    probabilities = [
        probability_available(distribution, pick, fallback_adp=20.0, teams=12)
        for pick in range(1, 61)
    ]

    assert probabilities[0] == pytest.approx(1.0)
    assert all(0 <= probability <= 1 for probability in probabilities)
    assert probabilities == sorted(probabilities, reverse=True)
    assert probabilities[24] > 0
    assert probabilities[24] < probabilities[23]

    root_two = 2**0.5

    def cdf(value):
        z_score = (value - distribution.mean) / distribution.stdev
        return 0.5 * (1 + adp_model.math.erf(z_score / root_two))

    expected = (1 - cdf(19.5)) / (1 - cdf(0.5))
    assert probability_available(
        distribution, 20, fallback_adp=20.0, teams=12
    ) == pytest.approx(expected)


@pytest.mark.parametrize("stdev", [None, 0, -1])
def test_missing_or_nonpositive_stdev_uses_saved_adp_fallback(stdev):
    distribution = PlayerDraftDistribution(
        name="Alpha Runner",
        mean=20.0,
        stdev=stdev,
        samples=500,
    )

    assert probability_available(
        distribution, 25, fallback_adp=30.0, teams=12
    ) == legacy_probability(30.0, 25, 12)


def test_sample_cutoff_blends_24_and_uses_full_model_at_25():
    sparse = PlayerDraftDistribution(
        name="Alpha Runner", mean=20.0, stdev=4.0, samples=24
    )
    threshold = PlayerDraftDistribution(
        name="Alpha Runner", mean=20.0, stdev=4.0, samples=25
    )
    modeled = probability_available(threshold, 25, fallback_adp=30.0, teams=12)
    fallback = legacy_probability(30.0, 25, 12)
    blended = probability_available(sparse, 25, fallback_adp=30.0, teams=12)

    assert blended == pytest.approx((24 / 25) * modeled + (1 / 25) * fallback)
