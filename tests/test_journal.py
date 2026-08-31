"""Tests for the only table in this database that cannot be re-derived.

Everything else here can be rebuilt by re-ingesting `raw/`, so the tests for it
mostly ask whether a number is right. These ask whether writing SURVIVES -- an
update that touches one field, a schema migration, a campaign regrouped by a
later fill -- because the failure mode of a journal is not a wrong figure, it is
a reader's own sentence quietly gone.
"""

from __future__ import annotations

import pytest

from optjournal import journal
from optjournal.db import connect, migrate


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
