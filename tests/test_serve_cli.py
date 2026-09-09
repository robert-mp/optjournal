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
