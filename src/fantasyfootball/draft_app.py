"""Local web server for a resilient live fantasy-football draft board."""

from __future__ import annotations

import argparse
import errno
import json
import mimetypes
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import parse_qs, urlparse

from fantasyfootball.draft_analysis import (
    build_dropoff_chart,
    build_league_tracker,
    optimize_draft,
)
from fantasyfootball.draft_sources import (
    fetch_platform_picks,
    fetch_platform_team_names,
)
from fantasyfootball.draft_state import DraftError, DraftSession
from fantasyfootball.utils import root

TEAM_NAMES_FORMAT = "platform_display_name_v1"


class DraftRequestHandler(BaseHTTPRequestHandler):
    server: "DraftHTTPServer"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, error: Exception, status: HTTPStatus) -> None:
        self._json({"error": str(error)}, status)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as error:
            raise DraftError("Request body must be valid JSON.") from error
        if not isinstance(payload, dict):
            raise DraftError("Request body must be a JSON object.")
        return payload

    def _asset(self, name: str) -> None:
        allowed = {
            "draft.html",
            "draft.css",
            "draft.js",
        }
        if name not in allowed:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        asset = files("fantasyfootball.web").joinpath(name)
        body = asset.read_bytes()
        content_type, _ = mimetypes.guess_type(name)
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type", content_type or "application/octet-stream"
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                self._asset("draft.html")
            elif path == "/draft.css":
                self._asset("draft.css")
            elif path == "/draft.js":
                self._asset("draft.js")
            elif path == "/api/state":
                self._json(self.server.session.public_state())
            elif path == "/api/analysis":
                query = parse_qs(parsed.query)
                metric = query.get("metric", [None])[0]
                try:
                    limit = int(query.get("limit", [18])[0])
                except ValueError as error:
                    raise DraftError(
                        "Chart rank limit must be a whole number."
                    ) from error
                self._json(
                    build_dropoff_chart(
                        self.server.session, limit=limit, metric=metric
                    )
                )
            elif path == "/api/league-strength":
                self._json(build_league_tracker(self.server.session))
            elif path == "/api/export":
                workbook = self.server.session.paths.workbook
                body = workbook.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{workbook.name}"',
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/drafted/export":
                drafted = self.server.session.paths.drafted
                body = drafted.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{drafted.name}"',
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except DraftError as error:
            self._error(error, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self._error(error, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/picks":
                pick = self.server.session.add_pick(
                    str(body.get("player", "")),
                    number=body.get("number"),
                    mine=body.get("mine"),
                    locked=True,
                )
                self._json(
                    {"pick": pick, "state": self.server.session.public_state()}
                )
            elif path == "/api/picks/untracked":
                pick = self.server.session.add_untracked_pick(
                    str(body.get("position", "")), mine=body.get("mine")
                )
                self._json(
                    {"pick": pick, "state": self.server.session.public_state()}
                )
            elif path == "/api/sync":
                session = self.server.session
                if session.simulate_api_down:
                    self._error(
                        DraftError(
                            "Simulated platform API outage. Manual fallback "
                            "is ready."
                        ),
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                    return
                remote = fetch_platform_picks(session.config, session.year)
                result = session.reconcile_platform_picks(remote)
                if session.state.get("team_names_format") != TEAM_NAMES_FORMAT:
                    try:
                        session.set_team_names(
                            fetch_platform_team_names(session.config),
                            format_name=TEAM_NAMES_FORMAT,
                        )
                    except DraftError:
                        # Pick sync is the critical path. Generic slot labels
                        # remain usable if optional owner metadata is offline.
                        pass
                self._json({**result, "state": session.public_state()})
            elif path == "/api/optimize":
                started = perf_counter()
                result = optimize_draft(self.server.session)
                elapsed = perf_counter() - started
                profile = self.server.session.record_optimizer_run(elapsed)
                self._json({**result, "profile": profile})
            elif path == "/api/exclusions":
                player = self.server.session.set_player_excluded(
                    str(body.get("player", "")),
                    bool(body.get("excluded", True)),
                )
                self._json(
                    {
                        "player": player,
                        "state": self.server.session.public_state(),
                    }
                )
            elif path == "/api/spreadsheet/refresh":
                result = self.server.session.refresh_from_spreadsheet()
                self._json(
                    {**result, "state": self.server.session.public_state()}
                )
            elif path == "/api/reset":
                self.server.session.reset_practice()
                self._json({"state": self.server.session.public_state()})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except DraftError as error:
            self._error(error, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self._error(error, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path.startswith("/api/picks/"):
                number = int(path.rsplit("/", 1)[-1])
                pick = self.server.session.replace_pick(
                    number, str(body.get("player", "")), mine=body.get("mine")
                )
                self._json(
                    {"pick": pick, "state": self.server.session.public_state()}
                )
            elif path == "/api/settings":
                self.server.session.set_metric(str(body.get("metric", "")))
                self._json({"state": self.server.session.public_state()})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (DraftError, ValueError) as error:
            self._error(error, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self._error(error, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if not path.startswith("/api/picks/"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            number = int(path.rsplit("/", 1)[-1])
            pick = self.server.session.undo_pick(number)
            self._json(
                {"pick": pick, "state": self.server.session.public_state()}
            )
        except (DraftError, ValueError) as error:
            self._error(error, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self._error(error, HTTPStatus.INTERNAL_SERVER_ERROR)


class DraftHTTPServer(ThreadingHTTPServer):
    def __init__(
        self, address: tuple[str, int], session: DraftSession
    ) -> None:
        super().__init__(address, DraftRequestHandler)
        self.session = session


def bind_server(
    host: str, port: int, session: DraftSession
) -> tuple[DraftHTTPServer, bool]:
    """Bind the requested port, falling back safely when it is occupied."""
    try:
        return DraftHTTPServer((host, port), session), False
    except OSError as error:
        if error.errno != errno.EADDRINUSE or port == 0:
            raise
        return DraftHTTPServer((host, 0), session), True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the local fantasy-football draft web app."
    )
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--league", required=True)
    parser.add_argument(
        "--pick",
        type=int,
        help="Your draft slot (defaults to config.json draft_slot)",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        help="Draft rounds (defaults to config.json draft_rounds or 15)",
    )
    parser.add_argument(
        "--metric",
        choices=(
            "VOR_Floor",
            "VOR_Points",
            "VOR_Ceiling",
            "Points",
            "Ceiling",
        ),
        default="VOR_Points",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--practice",
        action="store_true",
        help="Use a separate practice workbook",
    )
    mode.add_argument(
        "--live", action="store_true", help="Mirror picks to live_draft.csv"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Allow a non-loopback host (unsafe on untrusted networks)",
    )
    parser.add_argument(
        "--simulate-api-down",
        action="store_true",
        help=(
            "Practice the manual fallback while platform sync returns an error"
        ),
    )
    args = parser.parse_args(argv)
    if args.live and args.simulate_api_down:
        parser.error("--simulate-api-down is only available in practice mode")
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if args.host not in loopback_hosts and not args.allow_network:
        parser.error(
            "a non-loopback --host requires the explicit --allow-network flag"
        )
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    mode = "live" if args.live else "practice"
    session = DraftSession(
        Path(root()),
        args.year,
        args.league,
        args.pick,
        rounds=args.rounds,
        mode=mode,
        metric=args.metric,
        simulate_api_down=args.simulate_api_down,
    )
    server, used_fallback_port = bind_server(args.host, args.port, session)
    actual_port = int(server.server_address[1])
    url = f"http://{args.host}:{actual_port}"
    if used_fallback_port:
        print(
            f"Port {args.port} is already in use; selected free port "
            f"{actual_port}."
        )
    print(f"Draft board: {url}")
    print(f"Mode: {mode}; spreadsheet: {session.paths.workbook}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDraft board stopped. Your session has been saved.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
