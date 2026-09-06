import json
import socket
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pandas as pd
import pytest

from fantasyfootball.draft_app import DraftHTTPServer, bind_server, parse_args
from fantasyfootball.draft_state import DraftSession


def make_session(tmp_path, *, simulate_api_down=False):
    league = tmp_path / "data" / "2026" / "test"
    league.mkdir(parents=True)
    config = {
        "teams": 2,
        "positions": {"QB": 1, "RB": 1, "WR": 1, "TE": 1, "FLEX": 1},
        "site": "Sleeper",
        "adp_model": {"source": "disabled"},
        "draft_id": "draft",
    }
    (league / "config.json").write_text(json.dumps(config), encoding="utf-8")
    frame = pd.DataFrame(
        [
            {
                "Player": "Alpha Runner",
                "Team": "AAA",
                "Position": "RB",
                "Bye": 5,
                "ADP": 1.0,
                "VOR_Floor": 8.0,
                "VOR_Points": 10.0,
                "VOR_Ceiling": 12.0,
                "Floor": 80.0,
                "Points": 100.0,
                "Ceiling": 120.0,
                "FLEX_VOR_Points": 7.0,
                "Std Dev": 3.0,
            }
        ]
    )
    frame.to_csv(league / "clean.csv")
    return DraftSession(
        tmp_path,
        2026,
        "test",
        1,
        rounds=2,
        simulate_api_down=simulate_api_down,
    )


def request_json(url, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = Request(
        url + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=2) as response:
        return response.status, json.load(response)


def test_http_fallback_flow_and_simulated_api_outage(tmp_path):
    session = make_session(tmp_path, simulate_api_down=True)
    server = DraftHTTPServer(("127.0.0.1", 0), session)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(url + "/", timeout=2) as response:
            html = response.read().decode()
        assert 'id="manual-player"' in html
        assert 'id="picks-away-number"' in html
        assert 'data-untracked="K"' in html
        assert 'data-untracked="DST"' in html
        assert 'data-position="FLEX"' in html
        assert 'id="league-strength"' in html

        status, payload = request_json(url, "/api/state")
        assert status == 200
        assert payload["simulate_api_down"] is True

        _, optimized = request_json(url, "/api/optimize", {})
        assert optimized["profile"]["runs"] == 1
        assert optimized["profile"]["last_ms"] >= 0
        assert optimized["profile"]["threshold_ms"] == 500

        _, tracker = request_json(url, "/api/league-strength")
        assert tracker["active"] is False
        assert tracker["starts_round"] == 4

        _, excluded = request_json(
            url,
            "/api/exclusions",
            {"player": "Alpha Runner", "excluded": True},
        )
        assert excluded["state"]["excluded_players"] == ["Alpha Runner"]
        _, restored = request_json(
            url,
            "/api/exclusions",
            {"player": "Alpha Runner", "excluded": False},
        )
        assert restored["state"]["excluded_players"] == []

        with pytest.raises(HTTPError) as captured:
            request_json(url, "/api/sync", {})
        assert captured.value.code == 503
        error = json.load(captured.value)
        assert "Simulated platform API outage" in error["error"]

        _, payload = request_json(
            url, "/api/picks", {"player": "Alpha Runner"}
        )
        assert payload["state"]["current_pick"] == 2
        _, payload = request_json(
            url, "/api/picks/untracked", {"position": "DST"}
        )
        assert payload["state"]["current_pick"] == 3
        assert payload["pick"]["position"] == "DST"
        with urlopen(url + "/api/drafted/export", timeout=2) as response:
            drafted_csv = response.read().decode()
        assert "Alpha Runner" in drafted_csv
        assert "D/ST selected (not tracked)" in drafted_csv
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_server_uses_a_free_port_when_requested_port_is_busy(tmp_path):
    session = make_session(tmp_path)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    occupied_port = blocker.getsockname()[1]
    server = None
    try:
        server, used_fallback = bind_server(
            "127.0.0.1", occupied_port, session
        )
        assert used_fallback is True
        assert server.server_port != occupied_port
    finally:
        if server is not None:
            server.server_close()
        blocker.close()


def test_network_binding_requires_explicit_opt_in():
    with pytest.raises(SystemExit):
        parse_args(["--league", "test", "--host", "0.0.0.0"])

    args = parse_args(
        [
            "--league",
            "test",
            "--host",
            "0.0.0.0",
            "--allow-network",
        ]
    )
    assert args.host == "0.0.0.0"
    assert args.allow_network is True
