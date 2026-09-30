"""CLI ownership of the paths and credentials passed to the web server."""

from __future__ import annotations

import pytest

from optjournal import web
from optjournal.cli import main
from optjournal.config import (
    DEFAULT_ARCHIVE,
    DEFAULT_DB,
    DEFAULT_DEMO_DB,
    DEFAULT_DEMO_DIR,
)


def _serve_kwargs(monkeypatch, argv: list[str]) -> dict:
    captured: dict = {}
    monkeypatch.setattr(web, "serve", lambda **kw: captured.update(kw))
    assert main(argv) == 0
    return captured


def test_demo_flag_redirects_both_paths(monkeypatch):
    kw = _serve_kwargs(monkeypatch, ["serve", "--demo"])
    assert kw["db_path"] == DEFAULT_DEMO_DB
    assert kw["archive_dir"] == DEFAULT_DEMO_DIR


def test_without_demo_the_real_paths_are_served(monkeypatch):
    kw = _serve_kwargs(monkeypatch, ["serve"])
    assert kw["db_path"] == DEFAULT_DB
    assert kw["archive_dir"] == DEFAULT_ARCHIVE


def test_an_explicit_path_wins_over_demo(monkeypatch, tmp_path):
    kw = _serve_kwargs(
        monkeypatch, ["serve", "--demo", "--db", str(tmp_path / "mine.db")]
    )
    assert kw["db_path"] == tmp_path / "mine.db"
    assert kw["archive_dir"] == DEFAULT_DEMO_DIR


def test_serve_falls_back_to_the_query_id_in_the_environment(monkeypatch):
    monkeypatch.setenv("OPTJOURNAL_QUERY_ID", "1591754")
    assert _serve_kwargs(monkeypatch, ["serve"])["query_id"] == "1591754"


def test_an_explicit_query_id_beats_the_environment(monkeypatch):
    monkeypatch.setenv("OPTJOURNAL_QUERY_ID", "from-env")
    kw = _serve_kwargs(monkeypatch, ["serve", "--query-id", "explicit"])
    assert kw["query_id"] == "explicit"


def test_no_query_id_anywhere_stays_none_rather_than_empty(monkeypatch):
    monkeypatch.setenv("OPTJOURNAL_QUERY_ID", "")
    assert _serve_kwargs(monkeypatch, ["serve"])["query_id"] is None


def test_the_demo_ignores_a_query_id_in_the_environment(monkeypatch):
    monkeypatch.setenv("OPTJOURNAL_QUERY_ID", "1591754")
    kw = _serve_kwargs(monkeypatch, ["serve", "--demo"])
    assert kw["query_id"] is None
    assert kw["db_path"] == DEFAULT_DEMO_DB


def test_demo_refuses_a_query_id(monkeypatch, capsys):
    monkeypatch.setattr(web, "serve", lambda **kw: pytest.fail("must not serve"))
    assert main(["serve", "--demo", "--query-id", "1591754"]) == 2
    assert "Refused" in capsys.readouterr().err


def test_the_server_is_told_when_it_serves_the_demo(monkeypatch):
    """H6: `query_id=None` alone did not keep the demo off IBKR, because the
    server resolves the stored id per request. The flag is what it checks."""
    assert _serve_kwargs(monkeypatch, ["serve", "--demo"])["demo"] is True
    assert _serve_kwargs(monkeypatch, ["serve"])["demo"] is False


def test_the_stored_query_id_is_not_frozen_into_the_server(monkeypatch, tmp_path):
    """M10: `serve` resolved the stored id once at startup and handed it to the
    server and the scheduler as if it had been typed, so an id saved in Settings
    later never reached the Run button or the scheduled sync, and the page called
    the startup id an "override". Only an explicit flag or the environment is
    handed over; the stored step is read per request and per run."""
    from optjournal import settings  # noqa: PLC0415 - local to this test

    monkeypatch.delenv("OPTJOURNAL_QUERY_ID", raising=False)
    settings.update(tmp_path, query_id="111111")
    monkeypatch.setenv(settings.HOME_ENV, str(tmp_path))
    assert _serve_kwargs(monkeypatch, ["serve"])["query_id"] is None
