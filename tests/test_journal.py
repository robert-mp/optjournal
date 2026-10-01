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


# ------------------------------------------------------------------- anchors
#
# What an entry or a hand link is filed under. A card answers to the lowest of its
# own orders, which nothing but its own fills decides. Entries and links already
# stored were filed by the released code (0.2.0), whose rule is frozen below as
# the oracle the cases are checked against.


def _released_cards(db, links=()) -> list[tuple[str | None, frozenset[str]]]:
    """The cards the released code drew over this journal, as `(anchor, trade
    ids)`: its `stats.campaigns_for` and `campaigns.link`, frozen from `main`.

    Every order of a 90-second window joined every episode it touched, IBKR's own
    orders included; a card's anchor was the lowest order of its fills' window
    groups; and a link found the first episode, in list order, holding the id.
    Read over today's episodes, which are the released ones for the plain
    opening and closing fills these cases use.
    """
    from datetime import datetime

    from optjournal.history import build_history

    episodes = build_history(db, asset_category="OPT").episodes
    order_of_trade: dict[str, str] = {}
    first: dict[str, tuple[str, str | None]] = {}
    for row in db.execute(
        "SELECT trade_id, ib_order_id, date_time, trade_date, underlying_symbol,"
        " symbol FROM trades WHERE asset_category = 'OPT' AND ib_order_id IS NOT NULL"
    ):
        oid = str(row["ib_order_id"])
        order_of_trade[str(row["trade_id"])] = oid
        at = str(row["date_time"] or row["trade_date"] or "")
        under = row["underlying_symbol"] or row["symbol"]
        if oid not in first or at < first[oid][0]:
            first[oid] = (at, under)

    def lowest(ids):
        def key(oid):
            try:
                return (0, float(oid)), oid
            except ValueError:
                return (1, float("inf")), oid
        return min(ids, key=key, default=None)

    rows = sorted(((oid, datetime.fromisoformat(at) if at else None, under)
                   for oid, (at, under) in first.items()),
                  key=lambda r: (r[2] or f"￿{r[0]}", str(r[1] or ""), r[0]))
    groups: list[list[tuple]] = []
    for row in rows:
        last = groups[-1][-1] if groups else None
        if (last is not None and row[2] is not None and row[2] == last[2]
                and row[1] is not None and last[1] is not None
                and abs((row[1] - last[1]).total_seconds()) <= 90):
            groups[-1].append(row)
        else:
            groups.append([row])
    group_of = {r[0]: g for g, members in enumerate(groups) for r in members}
    parent = list(range(len(episodes)))

    def find(i):
        while parent[i] != i:
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    first_in_group: dict[int, int] = {}
    orders_of: dict[int, set[str]] = {}
    for i, episode in enumerate(episodes):
        for tid in episode.trade_ids:
            oid = order_of_trade.get(str(tid))
            if oid is None:
                continue
            orders_of.setdefault(i, set()).add(oid)
            g = group_of.get(oid)
            if g is None:
                continue
            orders_of[i].update(o for o, gg in group_of.items() if gg == g)
            union(first_in_group.setdefault(g, i), i)
    episode_of: dict[str, int] = {}
    for i, oids in orders_of.items():
        for oid in oids:
            episode_of.setdefault(oid, i)
    for a, b in links:
        ia, ib = episode_of.get(a), episode_of.get(b)
        if ia is not None and ib is not None:
            union(ia, ib)
    cards: dict[int, list[int]] = {}
    for i in range(len(episodes)):
        cards.setdefault(find(i), []).append(i)
    return sorted(
        (lowest({o for i in idxs for o in orders_of.get(i, ())}),
         frozenset(t for i in idxs for t in episodes[i].trade_ids))
        for idxs in cards.values())


def _journal_of(db, fills, *, statement=True) -> None:
    """Option fills, `(trade, account, order, at, qty, open_close, realised[,
    conid])`, in a journal with its statement."""
    if statement:
        db.execute(
            "INSERT INTO statements (source_file, sha256, account_id, from_date,"
            " to_date, base_currency, asset_filter, ingested_at)"
            " VALUES ('t.xml','x','U1','2025-01-01','2026-12-31','EUR','OPT','now')")
    for tid, account, oid, at, qty, open_close, pnl, *conid in fills:
        db.execute(
            "INSERT INTO trades (broker, trade_id, ib_exec_id, transaction_id,"
            " ib_order_id, account_id, trade_date, date_time, asset_category,"
            " symbol, conid, underlying_symbol, put_call, strike, expiry,"
            " multiplier, buy_sell, open_close, quantity, trade_price, currency,"
            " fx_rate_to_base, proceeds, proceeds_base, ib_commission,"
            " ib_commission_base, fifo_pnl_realized, fifo_pnl_realized_base,"
            " raw, source_file, first_seen_at)"
            " VALUES ('ibkr',?,?,?,?,?,?,?,'OPT',?,?,'SPY','P',500,"
            "'2026-12-18',100,?,?,?,1.0,'USD',1.0,?,?,-1.0,-1.0,?,?,'{}','t.xml','now')",
            (tid, tid, tid, oid, account, at[:10], at, f"SPY {conid[0] if conid else '1'}",
             conid[0] if conid else "1", "SELL" if qty < 0 else "BUY", open_close, qty,
             -qty * 100.0, -qty * 100.0, pnl, pnl))
    db.commit()


def _cards(db, *, reverse=False) -> list[tuple[str | None, frozenset[str]]]:
    """Today's cards as `(anchor, trade ids)`, over the episode list as built or
    reversed."""
    from optjournal.history import build_history
    from optjournal.stats import campaigns_for

    episodes = build_history(db, asset_category="OPT").episodes
    if reverse:
        episodes = episodes[::-1]
    return sorted((c.anchor, frozenset(t for i in c.episode_indices
                                       for t in episodes[i].trade_ids))
                  for c in campaigns_for(db, "OPT", episodes))


def _note(db, anchor: str) -> None:
    """An entry under `anchor`, filed the way `web._journal_write` files one."""
    row = db.execute("SELECT account_id FROM trades WHERE ib_order_id = ?",
                     (anchor,)).fetchone()
    journal.save(db, anchor, account_id=row["account_id"],
                 values={"entry_note": f"note {anchor}"})


def _shown(db) -> tuple[dict[frozenset[str], str | None], list[str]]:
    """The note each card shows, by its trade ids, and the orphans' anchors."""
    from optjournal.serialize import journal_data

    data = journal_data(db)
    return ({tids: (data["entries"].get(str(anchor)) or {}).get("entry_note")
             for anchor, tids in _cards(db)},
            sorted(e["anchor"] for e in data["orphans"]))


def _scalps(n: int, *, gap: int = 40, base: int = 7000) -> list[tuple]:
    """`n` round trips in one contract, each order `gap` seconds after the last."""
    out = []
    for k in range(2 * n):
        at = 10 * 3600 + k * gap
        out.append((f"t{k}", "U1", str(base + k),
                    f"2026-09-10 {at // 3600:02d}:{at % 3600 // 60:02d}:{at % 60:02d}",
                    1 if k % 2 == 0 else -1, "O" if k % 2 == 0 else "C",
                    None if k % 2 == 0 else 5.0))
    return out


#: A opened by 50 and closed by 100; B re-opened by 101 thirty seconds later and
#: closed by 150; C a later position opened by 200 and closed by 250.
_REENTRY = [
    ("t1", "U1", "50", "2026-09-01 10:00:00", 1, "O", None),
    ("t2", "U1", "100", "2026-09-02 10:00:00", -1, "C", 10.0),
    ("t3", "U1", "101", "2026-09-02 10:00:30", 1, "O", None),
    ("t4", "U1", "150", "2026-09-03 10:00:00", -1, "C", 20.0),
    ("t5", "U1", "200", "2026-09-04 10:00:00", 1, "O", None),
    ("t6", "U1", "250", "2026-09-05 10:00:00", -1, "C", 30.0),
]
#: A holding from before the archive closed by 100, re-entered by 101 thirty
#: seconds later, and still open.
_PRE_OPEN = [
    ("t2", "U1", "100", "2026-09-02 10:00:00", -1, "C", 10.0),
    ("t3", "U1", "101", "2026-09-02 10:00:30", 1, "O", None),
]
#: One order allocated to two accounts, each closed on its own day.
_ALLOCATED = [
    ("t1", "U1", "100", "2026-09-01 10:00:00", -1, "O", None),
    ("t2", "U2", "100", "2026-09-01 10:00:00", -1, "O", None),
    ("t3", "U2", "200", "2026-09-03 10:00:00", 1, "C", 20.0),
    ("t4", "U1", "300", "2026-09-05 10:00:00", 1, "C", 30.0),
]
#: The same put sold in two accounts 30 seconds apart, U2 closing first.
_TWO_ACCOUNTS = [
    ("t1", "U1", "100", "2026-09-01 10:00:00", -1, "O", None),
    ("t2", "U2", "101", "2026-09-01 10:00:30", -1, "O", None),
    ("t3", "U2", "150", "2026-09-02 10:00:00", 1, "C", 20.0),
    ("t4", "U1", "200", "2026-09-03 10:00:00", 1, "C", 30.0),
]


def test_every_card_with_a_fill_answers_to_its_own_lowest_order(db):
    """Six round trips in one 0DTE contract, each order 40 seconds after the
    last, are one window chain and six cards. Anchors read across the chain's
    groups and then made unique left four of the six with no anchor, so they
    could not be journalled or linked and the page said they had no fills."""
    _journal_of(db, _scalps(6))
    assert [anchor for anchor, _ in _cards(db)] == [
        "7000", "7002", "7004", "7006", "7008", "7010"]


def test_an_open_re_entry_has_an_anchor_while_it_is_open(db):
    """A holding from before the archive closed by 100 and re-entered by 101
    thirty seconds later: the re-entry had no anchor until it closed."""
    _journal_of(db, _PRE_OPEN)
    assert [anchor for anchor, _ in _cards(db)] == ["100", "101"]


@pytest.mark.parametrize("fills", [_REENTRY, _PRE_OPEN, _ALLOCATED, _TWO_ACCOUNTS,
                                   _scalps(6)],
                         ids=["re-entry", "open re-entry", "allocated", "two accounts",
                              "scalps"])
def test_no_anchor_reads_the_order_of_the_episode_list(db, fills):
    """`build_history` sorts episodes by when they closed, open ones last, so a
    later statement reorders them. An anchor, and the card a link finds, chosen
    by position in that list moved with it."""
    _journal_of(db, fills)
    journal.link(db, fills[0][2], fills[-1][2])
    assert _cards(db) == _cards(db, reverse=True)


@pytest.mark.parametrize("fills, cut", [
    (_TWO_ACCOUNTS, 3), (_ALLOCATED, 2), (_ALLOCATED, 3), (_PRE_OPEN + [
        ("t4", "U1", "415", "2026-09-03 10:41:18", 1, "O", None),
        ("t5", "U1", "417", "2026-09-04 10:25:43", -2, "C", 30.0)], 2),
    (_scalps(3) + [("t9", "U1", "7100", "2026-09-11 10:00:00", 1, "O", None)], 5),
], ids=["two accounts", "allocated, both open", "allocated, one closed",
        "open re-entry scaled in and closed", "scalps, then the last one closes"])
def test_a_later_statement_moves_no_anchor_and_no_entry(db, fills, cut):
    """Entries written on every card, then the rest of the fills ingested: each
    card keeps its anchor and its note, and no note lands on another card."""
    _journal_of(db, fills[:cut])
    before = _cards(db)
    for anchor, _ in before:
        _note(db, anchor)
    _journal_of(db, fills[cut:], statement=False)
    after = _cards(db)
    shown, orphans = _shown(db)
    for anchor, tids in before:
        (grown,) = [(a, t) for a, t in after if tids <= t]
        assert grown[0] == anchor, (anchor, grown)
        assert shown[grown[1]] == f"note {anchor}"
    assert orphans == []


@pytest.mark.parametrize("fills", [_REENTRY, _PRE_OPEN, _ALLOCATED, _TWO_ACCOUNTS,
                                   _scalps(6)],
                         ids=["re-entry", "open re-entry", "allocated", "two accounts",
                              "scalps"])
def test_an_entry_the_released_code_filed_shows_on_a_card_it_was_shown_on(db, fills):
    """Written under every anchor the released code drew, each entry shows on a
    card holding fills of the card it was written on, and on no other."""
    _journal_of(db, fills)
    released = _released_cards(db)
    for anchor, _ in released:
        _note(db, anchor)
    shown, orphans = _shown(db)
    assert orphans == []
    for anchor, tids in released:
        on = [t for t, note in shown.items() if note == f"note {anchor}"]
        assert on, f"note {anchor} is on no card"
        assert all(t <= tids for t in on), f"note {anchor} is on a card it was not on"


def test_a_link_the_released_code_stored_joins_what_it_joined(db):
    """A link between two cards' anchors as the released code drew them: A and B
    were one card there (anchor 50), C another (200). It joins A, the part of
    that card holding 50, with C, and leaves B, a decision of its own now."""
    _journal_of(db, _REENTRY)
    released = dict(_released_cards(db))
    journal.link(db, "50", "200")
    (joined,) = [tids for anchor, tids in _cards(db) if anchor == "50"]
    assert joined <= released["50"] | released["200"]
    assert joined & released["50"] and joined & released["200"]
    assert joined == {"t1", "t2", "t5", "t6"}


def test_a_link_finds_the_card_whose_anchor_it_names(db):
    """A after B (order 101 inside A's window): a link from B's card posts 101,
    which B answers to, so it joins B with C whatever order the list is in."""
    _journal_of(db, _REENTRY)
    journal.link(db, "101", "200")
    assert ("101", frozenset({"t3", "t4", "t5", "t6"})) in _cards(db)
    assert ("101", frozenset({"t3", "t4", "t5", "t6"})) in _cards(db, reverse=True)
