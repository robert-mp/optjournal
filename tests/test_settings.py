"""Tests for stored preferences and the query id's precedence.

Every case runs against a `tmp_path` root, so nothing here can read or write the
developer's own `.optjournal.json` -- which is the whole risk in testing a module
whose entire job is a file beside the code.
"""

from __future__ import annotations

import json

import pytest

from optjournal import settings


def test_an_absent_file_reads_as_no_preferences(tmp_path):
    """A fresh clone has no settings file, which is the ordinary case.

    Answering `{}` rather than raising is what lets every caller write
    `read().get(key)` with a default and never branch on existence.
    """
    assert settings.read(tmp_path) == {}
    assert settings.query_id(root=tmp_path) is None
    assert settings.scoring(root=tmp_path) is None


@pytest.mark.parametrize("damage", [
    "",                     # truncated to nothing
    "{",                    # interrupted mid-write
    "null",                 # valid JSON, not an object
    "[1, 2, 3]",            # valid JSON, wrong shape
    "not json at all",
])
def test_damage_reads_as_absence_rather_than_raising(tmp_path, damage):
    """A preference file must never be able to stop the journal.

    The data is in SQLite and `raw/`; this file holds two choices. So every
    kind of damage answers the way an absent file does, and the journal comes up
    with defaults rather than a traceback. Same rule as `flex._read_state`.
    """
    settings.path_for(tmp_path).write_text(damage, encoding="utf-8")
    assert settings.read(tmp_path) == {}
    assert settings.query_id(root=tmp_path) is None


def test_writing_one_preference_leaves_the_others_alone(tmp_path):
    """`update` merges, and the settings page depends on it.

    Saving the query id must not reset a scoreboard unit the form never asked
    about. A replace-the-file implementation passes every single-key test and
    fails exactly here.
    """
    settings.update(tmp_path, query_id="1591754")
    settings.update(tmp_path, scoring="contract")
    assert settings.read(tmp_path) == {"query_id": "1591754", "scoring": "contract"}


def test_none_deletes_a_preference_rather_than_storing_a_null(tmp_path):
    """"Back to the default" is spelled `None`, and leaves no trace.

    Storing null would make every reader distinguish "chosen as empty" from
    "never chosen", a distinction no caller wants: `scoring=None` means the page
    is back on the default, which is exactly the state a fresh install is in.
    """
    settings.update(tmp_path, query_id="1591754", scoring="contract")
    settings.update(tmp_path, scoring=None)
    assert settings.read(tmp_path) == {"query_id": "1591754"}
    assert "scoring" not in settings.path_for(tmp_path).read_text(encoding="utf-8")


def test_an_unknown_key_is_refused_at_the_call_site(tmp_path):
    """A typo must fail loudly, not become a preference that never applies.

    The failure a free-form settings dict invites is silent: `update(scorring=…)`
    writes a key nothing reads, the page keeps showing the default, and there is
    nothing to notice.
    """
    with pytest.raises(ValueError, match="not settings this journal stores"):
        settings.update(tmp_path, scorring="contract")
    assert settings.read(tmp_path) == {}, "a refused write must store nothing"


def test_a_write_leaves_no_temporary_file_behind(tmp_path):
    """The atomic write renames its staging file, it does not leave it.

    A stray `.optjournal.tmp` is gitignored, but a leftover would also mean the
    rename never happened -- which is the case where a crash could have left
    truncated JSON where valid settings were, and `read` would fail OPEN on the
    wreckage and silently reset every preference.
    """
    settings.update(tmp_path, query_id="1591754")
    assert json.loads(settings.path_for(tmp_path).read_text(encoding="utf-8"))
    assert not list(tmp_path.glob("*.tmp"))


def test_the_query_id_precedence_is_argument_then_environment_then_stored(
    tmp_path, monkeypatch
):
    """Three channels, and the order each earns.

    The ARGUMENT is what someone typed just now. The ENVIRONMENT is what a cron
    or a shell set deliberately. The STORED value is the one that makes a closed
    terminal survivable -- and, being a file, the only one a launchd agent can
    see, which is why it exists at all.
    """
    settings.update(tmp_path, query_id="stored")
    monkeypatch.delenv("OPTJOURNAL_QUERY_ID", raising=False)
    assert settings.query_id(root=tmp_path) == "stored"

    monkeypatch.setenv("OPTJOURNAL_QUERY_ID", "from-env")
    assert settings.query_id(root=tmp_path) == "from-env", "env beats the file"
    assert settings.query_id("typed", root=tmp_path) == "typed", "argument wins"


def test_a_blank_value_is_absence_at_every_level(tmp_path, monkeypatch):
    """`--query-id ''` is not a query id, and neither is an exported empty var.

    Treating either as real would send IBKR a request for a query that cannot
    exist, and the reply to that is indistinguishable from a genuine failure.
    """
    settings.update(tmp_path, query_id="stored")
    monkeypatch.setenv("OPTJOURNAL_QUERY_ID", "   ")
    assert settings.query_id("  ", root=tmp_path) == "stored", (
        "a blank argument and a blank environment both fall through"
    )


def test_stored_values_are_stripped_so_a_pasted_id_still_works(tmp_path):
    """An id pasted with a trailing newline is the same id.

    Whitespace surviving into the URL is a 400 from IBKR describing a query that
    looks correct in every log line that reports it.
    """
    settings.update(tmp_path, query_id=" 1591754\n")
    assert settings.query_id(root=tmp_path) == "1591754"


def test_the_home_override_redirects_the_file(tmp_path, monkeypatch):
    """`OPTJOURNAL_HOME` moves the settings file, and the suite depends on it.

    Two callers need this and neither is a preference: the test suite, which must
    never read the developer's own `.optjournal.json` (see the autouse fixture in
    conftest), and a packaged build, whose code directory sits inside a signed
    bundle that is neither writable nor preserved across an update.

    An explicit `root` still wins, because a caller passing one has said exactly
    where it wants to look.
    """
    monkeypatch.setenv(settings.HOME_ENV, str(tmp_path / "elsewhere"))
    (tmp_path / "elsewhere").mkdir()
    settings.update(query_id="7654321")
    assert settings.path_for() == tmp_path / "elsewhere" / settings.FILENAME
    assert settings.query_id() == "7654321"

    other = tmp_path / "explicit"
    other.mkdir()
    assert settings.query_id(root=other) is None, (
        "an explicit root outranks the environment"
    )
