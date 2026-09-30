"""Tests for the only table in this database that cannot be re-derived.

Everything else here can be rebuilt by re-ingesting `raw/`, so the tests for it
mostly ask whether a number is right. These ask whether writing SURVIVES -- an
update that touches one field, a schema migration, a campaign regrouped by a
later fill -- because the failure mode of a journal is not a wrong figure, it is
a reader's own sentence quietly gone.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from optjournal import journal
from optjournal.db import connect, migrate

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "j.db")
    migrate(conn)
    yield conn
    conn.close()


def _save(db, **values):
    return journal.save(
        db, "1241544513", account_id="U1", underlying_symbol="META",
        opened_on="2026-08-03", values=values,
    )


def test_an_entry_round_trips(db):
    saved = _save(db, plan_target="take at 50%", plan_invalidation="short strike breached")

    assert saved is not None
    assert saved.anchor_order_id == "1241544513"
    assert saved.values["plan_target"] == "take at 50%"
    read = journal.entry_for(db, "1241544513", account_id="U1")
    assert read == saved, "what was read back is not what was written"


def test_the_close_review_does_not_blank_the_entry_plan(db):
    """Two surfaces write one row, at two different moments.

    The plan is written before the trade; the review weeks later, from a
    different form. A whole-row write would mean the close review posts every
    field it does not show as empty and erases the plan it is reviewing -- which
    is the failure a reader discovers exactly once, and never trusts the app
    again.
    """
    _save(db, plan_target="take at 50%", plan_invalidation="strike breached")

    reviewed = _save(db, followed_target="yes", exit_trigger="target",
                     lessons="sized right, exited early")

    assert reviewed is not None
    assert reviewed.values["plan_target"] == "take at 50%"
    assert reviewed.values["plan_invalidation"] == "strike breached"
    assert reviewed.values["followed_target"] == "yes"


def test_clearing_a_field_is_distinguishable_from_not_touching_it(db):
    """A reader deleting a sentence must be able to delete it.

    So an empty value in a write CLEARS, while a key absent from the write is
    left alone. Both mean "no text" when read back, which is why the test writes
    one of each in the same call.
    """
    _save(db, plan_target="take at 50%", lessons="held too long")

    updated = _save(db, plan_target="")

    assert updated is not None
    assert updated.values["plan_target"] is None, "the cleared field survived"
    assert updated.values["lessons"] == "held too long", "an untouched field went"


def test_whitespace_only_text_is_stored_as_absent(db):
    """One rule for absence, so `is_empty` cannot depend on which surface wrote.

    A textarea a reader tabbed through posts a newline, and a row holding "\\n"
    would count as journalled while saying nothing.
    """
    saved = _save(db, plan_target="   \n  ")
    assert saved is None, "a whitespace-only entry was stored as written"
    assert journal.entry_for(db, "1241544513", account_id="U1") is None


def test_emptying_every_field_deletes_the_row(db):
    """An entry with nothing in it is not an entry.

    Stored, it would count against any completeness tally -- "12 of 14 decisions
    journalled" -- while telling a reader nothing, which makes the tally worse
    than no tally.
    """
    _save(db, plan_target="take at 50%")

    assert _save(db, plan_target="") is None
    assert journal.entry_for(db, "1241544513", account_id="U1") is None


def test_created_at_survives_an_update(db):
    """When the journal gained this entry is a fact about the journal.

    Same rule as `trades.first_seen_at`, and for the same reason: it is what lets
    "written since" be answerable at all. `updated_at` is the one that moves.
    """
    first = _save(db, plan_target="take at 50%")
    assert first is not None

    db.execute("UPDATE journal_entries SET created_at = '2026-08-03T14:00:00+00:00',"
               " updated_at = '2026-08-03T14:00:00+00:00'")
    db.commit()

    later = _save(db, lessons="exited early")
    assert later is not None
    assert later.created_at == "2026-08-03T14:00:00+00:00"
    assert later.updated_at != later.created_at, "the update stamp did not move"


@pytest.mark.parametrize("field", ["followed_target", "followed_invalidation"])
def test_an_adherence_answer_outside_the_three_is_refused(db, field):
    """Three answers, and a caller sending a boolean has a bug.

    Coerced to None instead, the adherence count would read "not answered" for a
    trade that was answered -- wrong in the reassuring direction, which is the
    direction nobody audits. So it raises.
    """
    with pytest.raises(journal.JournalError, match="not one of"):
        _save(db, **{field: "true"})
    assert journal.entry_for(db, "1241544513", account_id="U1") is None, (
        "the refused write left a partial row"
    )


def test_na_is_a_real_adherence_answer(db):
    """The reason the field is not a boolean.

    A trade taken off at target never reached its invalidation, so "there was no
    loss exit to follow" has to be storable -- as False it would count a winner
    as discipline broken.
    """
    saved = _save(db, followed_target="yes", followed_invalidation="na")
    assert saved is not None
    assert saved.values["followed_invalidation"] == "na"


def test_an_unknown_exit_trigger_is_refused(db):
    with pytest.raises(journal.JournalError, match="exit_trigger"):
        _save(db, exit_trigger="felt_wrong")


def test_every_trigger_in_the_list_is_accepted(db):
    """The enum and its validator cannot drift apart: one is built from the other.

    Worth asserting anyway, because the page renders `TRIGGERS` as its options,
    so a value the form can produce and the writer rejects is a button that
    silently fails.
    """
    for key in journal.TRIGGERS:
        assert _save(db, exit_trigger=key) is not None


def test_a_field_the_table_does_not_have_is_refused(db):
    """Names come from `FIELDS`, so an endpoint cannot invent a column.

    The alternative -- ignoring unknown keys -- means a typo in the page's form
    posts successfully and the text never appears again.
    """
    with pytest.raises(journal.JournalError, match="not journal fields"):
        _save(db, plan="take at 50%")


def test_the_key_columns_are_not_writable_through_values(db):
    """A request may not re-point an entry at another decision.

    `anchor_order_id`, `account_id` and `broker` come from the campaign the
    reader had open, never from the form, so they are absent from `FIELDS` and a
    write naming them is refused like any other unknown field.
    """
    for key in ("anchor_order_id", "account_id", "broker"):
        with pytest.raises(journal.JournalError, match="not journal fields"):
            _save(db, **{key: "elsewhere"})


def test_a_position_with_no_fills_cannot_be_journalled(db):
    """A snapshot-only campaign has no order id to key on.

    Refused with a reason rather than filed under a placeholder: the next such
    campaign would collide with it, and two decisions sharing one journal entry
    is worse than a decision having none.
    """
    with pytest.raises(journal.JournalError, match="no fills"):
        journal.save(db, None, account_id="U1", values={"plan_target": "hold"})


def test_two_accounts_journal_the_same_order_id_independently(db):
    """Order ids are unique per broker, not across brokers.

    The same reason `trades` is keyed `(broker, trade_id)`. Included because the
    journal's key repeats that choice, and a two-broker collision here would
    show one account's private notes on another's trade.
    """
    journal.save(db, "1", account_id="U1", values={"plan_target": "mine"})
    journal.save(db, "1", account_id="U2", values={"plan_target": "theirs"})
    journal.save(db, "1", account_id="U1", broker="other",
                 values={"plan_target": "elsewhere"})

    assert journal.entry_for(db, "1", account_id="U1").values["plan_target"] == "mine"
    assert journal.entry_for(db, "1", account_id="U2").values["plan_target"] == "theirs"
    assert journal.entry_for(
        db, "1", account_id="U1", broker="other"
    ).values["plan_target"] == "elsewhere"
    assert len(journal.entries(db)) == 3
    assert len(journal.entries(db, broker="other")) == 1


def test_entries_is_keyed_for_lookup_by_a_page_full_of_decisions(db):
    """The Trades tab asks "does THIS decision have an entry" per card.

    One query and a dict lookup, rather than a query per card.
    """
    journal.save(db, "1", account_id="U1", values={"plan_target": "a"})
    journal.save(db, "2", account_id="U1", values={"plan_target": "b"})

    got = journal.entries(db)
    assert set(got) == {("ibkr", "U1", "1"), ("ibkr", "U1", "2")}


def test_an_entry_whose_campaign_regrouped_is_reported_not_lost(db):
    """The one way this key can fail, and what happens when it does.

    Membership is decided by a 90-second window, so a fill arriving late inside
    it can join a cluster and LOWER the campaign's anchor. The note written
    against the old anchor then points at no campaign. It still records its
    underlying and open date, so it is something a reader can act on -- the row
    reads "the META decision opened 2026-08-03" -- rather than a loss they never
    hear about.
    """
    _save(db, plan_target="take at 50%")
    journal.save(db, "1299999999", account_id="U1", underlying_symbol="GOOG",
                 opened_on="2026-08-04", values={"plan_target": "still live"})

    orphaned = journal.orphans(db, live_anchors={"1299999999"})

    assert [e.anchor_order_id for e in orphaned] == ["1241544513"]
    assert (orphaned[0].underlying_symbol, orphaned[0].opened_on) == (
        "META", "2026-08-03"
    ), "an orphan that cannot say which decision it belonged to is a loss"
    assert orphaned[0].values["plan_target"] == "take at 50%", "the text went"


def test_deleting_an_entry_reports_whether_a_row_went(db):
    _save(db, plan_target="take at 50%")

    assert journal.delete(db, "1241544513", account_id="U1") is True
    assert journal.delete(db, "1241544513", account_id="U1") is False


def test_the_identity_it_records_is_refreshed_but_the_key_is_not(db):
    """`underlying_symbol` describes which decision the anchor belongs to today.

    So a corrected symbol reaches the row that quotes it back, while the key it
    is filed under does not move -- otherwise correcting a label would orphan the
    entry it labels.
    """
    _save(db, plan_target="take at 50%")

    journal.save(db, "1241544513", account_id="U1", underlying_symbol="META1",
                 opened_on="2026-08-03", values={"lessons": "x"})

    read = journal.entry_for(db, "1241544513", account_id="U1")
    assert read.underlying_symbol == "META1"
    assert read.values["plan_target"] == "take at 50%"


def test_the_payload_names_every_writable_field(db):
    """The page binds to these keys, so a field the payload omits is a field the
    reader can write and never see again."""
    saved = _save(db, plan_target="take at 50%")
    payload = saved.payload()
    for name in journal.FIELDS:
        assert name in payload, f"{name} is writable but absent from the payload"
    assert payload["anchor"] == "1241544513"


def test_a_link_has_one_spelling_and_round_trips(db):
    """Lower id first, by number: '999' before '1000', which string order gets
    wrong. Linking twice is one row."""
    assert journal.link(db, "1000", "999") == ("999", "1000")
    journal.link(db, "999", "1000")
    assert journal.links(db) == [("999", "1000")]
    assert journal.unlink(db, "1000", "999") is True
    assert journal.links(db) == []
    assert journal.unlink(db, "1000", "999") is False


def test_an_order_cannot_be_linked_to_itself(db):
    with pytest.raises(journal.JournalError):
        journal.link(db, "42", "42")


# ------------------------------------------------ handles made under an older rule
#
# Every entry and hand link a reader already has was made against the anchors the
# page offered when it was written. These cases are read twice: by the rule that
# stored them, `campaigns.py` as of 51ff771 loaded from git, and by the code here,
# which must resolve each stored handle to the same card.


def _stored_under_51ff771(tmp_path, monkeypatch):
    """`campaigns.py` as of 51ff771, loaded from git as a module of its own."""
    shown = subprocess.run(
        ["git", "show", "51ff771:src/optjournal/campaigns.py"], cwd=ROOT,
        capture_output=True, check=False)
    if shown.returncode:
        pytest.skip("git cannot show 51ff771, so there is no older rule to compare")
    path = tmp_path / "campaigns_51ff771.py"
    path.write_bytes(shown.stdout)
    spec = importlib.util.spec_from_file_location("campaigns_51ff771", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "campaigns_51ff771", module)
    spec.loader.exec_module(module)
    return module


def _journal_of(db, fills) -> None:
    """Option fills on one SPY put, `(trade, account, order, at, qty, open_close,
    realised)`, in a journal with its statement."""
    db.execute(
        "INSERT INTO statements (source_file, sha256, account_id, from_date,"
        " to_date, base_currency, asset_filter, ingested_at)"
        " VALUES ('t.xml','x','U1','2025-01-01','2026-12-31','EUR','OPT','now')")
    for tid, account, oid, at, qty, open_close, pnl in fills:
        db.execute(
            "INSERT INTO trades (broker, trade_id, ib_exec_id, transaction_id,"
            " ib_order_id, account_id, trade_date, date_time, asset_category,"
            " symbol, conid, underlying_symbol, put_call, strike, expiry,"
            " multiplier, buy_sell, open_close, quantity, trade_price, currency,"
            " fx_rate_to_base, proceeds, proceeds_base, ib_commission,"
            " ib_commission_base, fifo_pnl_realized, fifo_pnl_realized_base,"
            " raw, source_file, first_seen_at)"
            " VALUES ('ibkr',?,?,?,?,?,?,?,'OPT','SPY P','1','SPY','P',500,"
            "'2026-12-18',100,?,?,?,1.0,'USD',1.0,?,?,-1.0,-1.0,?,?,'{}','t.xml','now')",
            (tid, tid, tid, oid, account, at[:10], at, "SELL" if qty < 0 else "BUY",
             open_close, qty, -qty * 100.0, -qty * 100.0, pnl, pnl))
    db.commit()


def _cards(db, old=None, monkeypatch=None) -> list[tuple[str | None, frozenset[int]]]:
    """Every campaign as `(anchor, episode indices)`, by today's rule or `old`'s."""
    from optjournal import stats
    from optjournal.history import build_history

    episodes = build_history(db, asset_category="OPT").episodes
    if old is None:
        camps = stats.campaigns_for(db, "OPT", episodes)
    else:
        with monkeypatch.context() as patch:
            patch.setattr(stats, "campaigns", old)
            camps = stats.campaigns_for(db, "OPT", episodes)
    return sorted(((c.anchor, frozenset(c.episode_indices)) for c in camps),
                  key=lambda card: sorted(card[1]))


def _episode_of(db, trade_id: str) -> int:
    from optjournal.history import build_history

    episodes = build_history(db, asset_category="OPT").episodes
    return next(i for i, e in enumerate(episodes) if e.trade_ids[:1] == [trade_id])


#: A opened by 50 and closed by 100; B re-opened by 101 thirty seconds later and
#: closed by 150; C a later position, opened by 200 and closed by 250.
_REENTRY = [
    ("t1", "U1", "50", "2026-09-01 10:00:00", 1, "O", None),
    ("t2", "U1", "100", "2026-09-02 10:00:00", -1, "C", 10.0),
    ("t3", "U1", "101", "2026-09-02 10:00:30", 1, "O", None),
    ("t4", "U1", "150", "2026-09-03 10:00:00", -1, "C", 20.0),
    ("t5", "U1", "200", "2026-09-04 10:00:00", 1, "O", None),
    ("t6", "U1", "250", "2026-09-05 10:00:00", -1, "C", 30.0),
]


@pytest.mark.parametrize("fills", [
    _REENTRY,
    [(tid, "U2" if oid in ("101", "150") else account, oid, at, qty, oc, pnl)
     for tid, account, oid, at, qty, oc, pnl in _REENTRY],
], ids=["same account", "re-entry in another account"])
def test_a_re_entry_inside_the_window_keeps_the_anchor_its_writing_was_filed_under(
        db, tmp_path, monkeypatch, fills):
    """A closed by order 100, B re-opened by 101 thirty seconds later (in the same
    account or another: the window group spans both). The anchor the page offered
    for B was 100, and narrowing a campaign's orders to its own fills moved it to
    101: an entry written on B's card showed on no card, and a link made from it,
    (100, 200), joined A with C instead of B."""
    old = _stored_under_51ff771(tmp_path, monkeypatch)
    _journal_of(db, fills)
    journal.link(db, "100", "200")
    assert _cards(db) == _cards(db, old, monkeypatch)
    journal.unlink(db, "100", "200")
    assert _cards(db) == _cards(db, old, monkeypatch)
    assert ("100", frozenset({_episode_of(db, "t3")})) in _cards(db), (
        "B, the re-entry, answers to 100")


def test_an_entry_on_the_re_entry_card_is_still_on_it(db):
    """The same case read through the payload the page looks entries up in."""
    from optjournal.serialize import journal_data

    _journal_of(db, _REENTRY[:4])
    journal.save(db, "100", account_id="U1", values={"entry_note": "why I re-entered"})
    assert dict(_cards(db))["100"] == frozenset({_episode_of(db, "t3")})
    data = journal_data(db)
    assert data["entries"]["100"]["entry_note"] == "why I re-entered"
    assert data["orphans"] == []


#: A: a holding from before the archive closed by 100, then B re-opened by 101
#: thirty seconds later and closed by 150. Both cards answered to 100.
_PRE = [
    ("t2", "U1", "100", "2026-09-02 10:00:00", -1, "C", 10.0),
    ("t3", "U1", "101", "2026-09-02 10:00:30", 1, "O", None),
    ("t4", "U1", "150", "2026-09-03 10:00:00", -1, "C", 20.0),
]
#: A short from before the archive bought back and a long opened by one order, in
#: two fills, the long then sold by 1003. Both cards answered to 1002.
_DIVIDED = [
    ("t1", "U1", "1002", "2026-09-15 10:00:00", 2, "C", 48.0),
    ("t2", "U1", "1002", "2026-09-15 10:00:01", 1, "O", None),
    ("t3", "U1", "1003", "2026-09-20 10:00:00", -1, "C", 25.0),
]


@pytest.mark.parametrize("fills, shared, first, other", [
    (_PRE, "100", "t2", "101"),
    (_DIVIDED, "1002", "t1", "1003"),
], ids=["closed then re-entered", "one order dividing two positions"])
def test_an_anchor_two_cards_answered_to_has_one_owner_and_is_reported(
        db, tmp_path, monkeypatch, fills, shared, first, other):
    """The older rule gave two cards one anchor, so one entry showed on both and a
    save from either rewrote the other's note. The card that took the anchor
    order's first execution keeps it; the other answers to the lowest order of its
    own that nothing else claims, and an entry filed under the shared anchor is
    listed with the notes no single card claims, since it may be about either."""
    from optjournal.serialize import journal_data

    old = _stored_under_51ff771(tmp_path, monkeypatch)
    _journal_of(db, fills)
    journal.save(db, shared, account_id="U1", values={"entry_note": "which one?"})
    before = _cards(db, old, monkeypatch)
    assert [anchor for anchor, _ in before] == [shared, shared], "the premise"
    after = _cards(db)
    assert {episodes for _, episodes in after} == {episodes for _, episodes in before}
    assert dict(after)[shared] == frozenset({_episode_of(db, first)})
    assert sorted(anchor for anchor, _ in after) == sorted([shared, other])
    data = journal_data(db)
    assert data["entries"][shared]["entry_note"] == "which one?"
    assert [e["anchor"] for e in data["orphans"]] == [shared]
