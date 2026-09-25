from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest
from typer.testing import CliRunner

from mbet.cli import app
from mbet.config import load_config
from mbet.errors import NOT_CONFIGURED
from tests.conftest import combined

runner = CliRunner()


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MBET_HOME", str(tmp_path))
    monkeypatch.setenv("MBET_DISABLE_KEYRING", "1")
    monkeypatch.delenv("MBET_CONFIG", raising=False)
    monkeypatch.delenv("MBET_BETTING_ENABLED", raising=False)
    monkeypatch.delenv("MBET_MAX_STAKE", raising=False)
    monkeypatch.delenv("MBET_DEBUG", raising=False)
    return tmp_path


def test_version(isolated):
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "mbet 0.1.0" in combined(result)


def test_balance_without_endpoint(isolated):
    result = runner.invoke(app, ["balance"])
    assert result.exit_code == 1
    assert NOT_CONFIGURED in combined(result)


def test_login_does_not_prompt_when_unconfigured(isolated):
    result = runner.invoke(app, ["login"], input="alice\nsecret-should-not-be-used\n")
    assert result.exit_code == 1
    text = combined(result)
    assert NOT_CONFIGURED in text
    assert "secret-should-not-be-used" not in text


def test_max_stake_and_betting_disabled_and_dry_run(isolated):
    over = runner.invoke(
        app,
        ["bet", "--event", "381923", "--selection", "home", "--stake", "100", "--odds", "1.85", "--yes"],
    )
    assert over.exit_code == 1
    assert "exceeds configured maximum of $10.00" in combined(over)

    disabled = runner.invoke(
        app,
        ["bet", "--event", "381923", "--selection", "home", "--stake", "1", "--odds", "1.85", "--yes"],
    )
    assert disabled.exit_code == 1
    assert "Bet submission is disabled" in combined(disabled)

    dry = runner.invoke(
        app,
        [
            "bet",
            "--event",
            "381923",
            "--selection",
            "home",
            "--stake",
            "1",
            "--odds",
            "1.85",
            "--event-name",
            "Team A vs Team B",
            "--selection-name",
            "Team A",
            "--dry-run",
        ],
    )
    text = combined(dry)
    assert dry.exit_code == 0, text
    assert "Team A vs Team B" in text
    assert "1.85" in text
    assert "Dry run" in text
    assert "No bet was submitted" in text


def test_config_env_overrides(isolated, monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "base_url: https://www.marathonbet.com\nmax_stake: '10.00'\nbetting_enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MBET_CONFIG", str(path))
    monkeypatch.setenv("MBET_MAX_STAKE", "2.50")
    monkeypatch.setenv("MBET_BETTING_ENABLED", "false")
    config = load_config()
    assert str(config.max_stake) == "2.50"
    assert config.betting_enabled is False


def test_cli_login_hides_password(isolated, monkeypatch, tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            body = json.loads(raw.decode())
            assert body["password"] == "p@ss-NOT-IN-LOGS-99"
            data = json.dumps({"csrfToken": "csrf-from-server"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "MBSESSION=cookie-from-server; Path=/; HttpOnly")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                f"base_url: http://{host}:{port}",
                "currency: USD",
                "max_stake: '10.00'",
                "betting_enabled: false",
                "endpoints:",
                "  login: /session/login",
                "request_templates:",
                "  login:",
                "    method: POST",
                "    content_type: json",
                "    body:",
                "      username: '{username}'",
                "      password: '{password}'",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MBET_CONFIG", str(config))
    try:
        result = runner.invoke(app, ["login"], input="alice\np@ss-NOT-IN-LOGS-99\n")
        text = combined(result)
        assert result.exit_code == 0, text
        assert "Login successful" in text
        assert "p@ss-NOT-IN-LOGS-99" not in text
        raw = (tmp_path / "session.bin").read_bytes()
        assert b"p@ss-NOT-IN-LOGS-99" not in raw
    finally:
        server.shutdown()
        server.server_close()
