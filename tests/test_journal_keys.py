"""What a write-up is filed under, through the real handlers.

A new row is filed under `t:` and its card's first fill, a key no other card
can hold and that stays with the fill whatever the cards do (`campaigns`, "What
a write-up is filed under"); rows the released code wrote under an order id are
read by account and by the day they record. Each case here is one a review
found a write-up on the wrong card in, or two cards sharing one. Driven through
`serve_ephemeral`, so the page's posts meet the handlers that file them.
"""

from __future__ import annotations

import json
import random
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import add_statement, connect_migrated

from optjournal import journal, web
from optjournal.db import connect
from optjournal.journal import FIELDS


def _insert(db: Path, fills: list[tuple], *, category: str = "OPT") -> None:
    """Fills `(trade, account, order, at, qty, open_close, realised, conid[,
    notes])`, all on QQQ, options unless `category` says otherwise."""
    option = category == "OPT"
    conn = connect(db)
    for tid, account, oid, at, qty, open_close, pnl, conid, *notes in fills:
        conn.execute(
            "INSERT INTO trades (broker, trade_id, ib_exec_id, transaction_id,"
            " ib_order_id, account_id, trade_date, date_time, asset_category,"
            " symbol, conid, underlying_symbol, put_call, strike, expiry,"
            " multiplier, buy_sell, open_close, notes, quantity, trade_price,"
            " currency, fx_rate_to_base, proceeds, proceeds_base, ib_commission,"
            " ib_commission_base, fifo_pnl_realized, fifo_pnl_realized_base,"
            " raw, source_file, first_seen_at)"
            " VALUES ('ibkr',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1.0,'USD',1.0,?,?,"
            "-1.0,-1.0,?,?,'{}','t.xml','now')",
            (tid, tid, tid, oid, account, at[:10], at, category,
             f"QQQ {conid}" if option else "QQQ", conid, "QQQ" if option else None,
             "C" if option else None, 500 if option else None,
             "2026-12-18" if option else None, 100 if option else 1,
             "SELL" if qty < 0 else "BUY", open_close, notes[0] if notes else None,
             qty, -qty * 100.0, -qty * 100.0, pnl, pnl))
    conn.commit()
    conn.close()


def _journal(tmp_path: Path, fills: list[tuple]) -> Path:
    db = tmp_path / "journal.db"
    conn = connect_migrated(db)
    add_statement(conn)
    conn.commit()
    conn.close()
    _insert(db, fills)
    (tmp_path / "raw").mkdir(exist_ok=True)
    return db


def _post(base: str, path: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{base}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _state(base: str) -> dict:
    with urllib.request.urlopen(f"{base}/api/state", timeout=120) as response:  # noqa: S310
        return json.loads(response.read())


def _cards(state: dict) -> list[dict]:
    """Each card's anchor, opening date, status, links and the note it shows."""
    entries = state["journal"]["entries"]
    return sorted(({"anchor": lc["anchor"], "opened": str(lc["opened_at"])[:10],
                    "status": lc["status"], "links": lc["links"],
                    "note": (entries.get(lc["anchor"] or "") or {}).get("entry_note"),
                    "lessons": (entries.get(lc["anchor"] or "") or {}).get("lessons")}
                   for lc in state["lifecycles"]), key=lambda c: c["opened"])


def _full_rows(db: Path) -> dict[tuple, tuple]:
    """Every stored row, key to everything written in it."""
    conn = connect(db)
    rows = {tuple(r)[:3]: tuple(r)[3:] for r in conn.execute(
        "SELECT broker, account_id, anchor_order_id, * FROM journal_entries")}
    conn.close()
    return rows


def _rows(db: Path) -> list[tuple]:
    conn = connect(db)
    rows = [tuple(r) for r in conn.execute(
        "SELECT broker, account_id, anchor_order_id, entry_note FROM journal_entries"
        " ORDER BY 1, 2, 3")]
    conn.close()
    return rows


def _form(state: dict, anchor: str, **changes) -> dict:
    """What the page's Save posts: every field, as the form was filled from the
    entry the card shows, and which row that is."""
    je = state["journal"]["entries"].get(anchor)
    body = {"anchor": anchor, **{f: (je or {}).get(f) or "" for f in FIELDS},
            "shows": [je["broker"], je["account_id"], je["anchor"]] if je else None}
    body.update(changes)
    return body


def _released(db: Path, anchor: str, account: str, opened_on: str, note: str) -> None:
    """A row as the released version filed it: under the card's lowest order id,
    with the order's first fill date on record when it was written."""
    conn = connect(db)
    journal.save(conn, anchor, account_id=account, opened_on=opened_on,
                 underlying_symbol="QQQ", values={"entry_note": note})
    conn.close()


#: GTC order 5040 opens A, which expires; the same order fills again a week
#: later and opens B.
_GTC = [("t1", "U1", "5040", "2026-09-01 10:34:11", 1, "O", None, "21"),
        ("t2", "U1", "5082", "2026-09-03 16:20:00", -1, "C", -200.0, "21", "Ep")]
_REFILL = [("t3", "U1", "5040", "2026-09-08 09:50:02", 1, "O", None, "21")]


def test_a_gtc_order_that_fills_again_files_the_new_card_under_its_own_fill(tmp_path):
    """Both cards answered to 5040, so B showed A's write-up, and writing B's
    plan posted 5040 and overwrote A's row in the database."""
    db = _journal(tmp_path, _GTC)
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        (a,) = _cards(_state(base))
        assert _post(base, "/api/journal", _form(_state(base), a["anchor"],
                                                 entry_note="plan A"))[0] == 200
        _insert(db, _REFILL)
        first, second = _cards(_state(base))
        assert (first["anchor"], second["anchor"]) == ("5040", "t:t3")
        assert (first["note"], second["note"]) == ("plan A", None)
        status, wrote = _post(base, "/api/journal", _form(_state(base), second["anchor"],
                                                          entry_note="plan B"))
        assert (status, wrote["entry"]["entry_note"]) == (200, "plan B")
        assert [c["note"] for c in _cards(_state(base))] == ["plan A", "plan B"]
    assert _rows(db) == [("ibkr", "U1", "t:t1", "plan A"), ("ibkr", "U1", "t:t3", "plan B")]


def test_a_history_import_of_an_orders_first_fill_moves_no_write_up(tmp_path):
    """The archive holds only GTC 5040's refill (B), and the reader writes B up.
    A history import brings 5040's first fill (A): A answered to 5040 and showed
    B's text, its editor came prefilled with it, and typing A's note overwrote
    B's sentence; a page loaded before the import, saving from B, edited the row
    now drawn on A."""
    db = _journal(tmp_path, _REFILL)
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        (b,) = _cards(_state(base))
        _post(base, "/api/journal", _form(_state(base), b["anchor"], entry_note="B note"))
        before = _state(base)
        _insert(db, _GTC)
        a, b = _cards(_state(base))
        assert (a["anchor"], a["note"], b["anchor"], b["note"]) == (
            "5040", None, "t:t3", "B note")
        status, reply = _post(base, "/api/journal", _form(before, "5040", lessons="stale"))
        assert (status, reply["kind"]) == (409, "stale")
        assert _post(base, "/api/journal", _form(_state(base), "5040",
                                                 entry_note="A note"))[0] == 200
        assert [c["note"] for c in _cards(_state(base))] == ["A note", "B note"]
    assert _rows(db) == [("ibkr", "U1", "t:t1", "A note"), ("ibkr", "U1", "t:t3", "B note")]


def test_a_row_the_released_version_filed_finds_the_card_by_the_day_it_records(tmp_path):
    """The released version filed B's write-up under 5040, with B's day, before
    the history import brought A, 5040's first fill. It stays on B."""
    db = _journal(tmp_path, _REFILL)
    _released(db, "5040", "U1", "2026-09-08", "B, by the released version")
    _insert(db, _GTC)
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        assert [c["note"] for c in _cards(_state(base))] == [
            None, "B, by the released version"]


def test_a_released_row_finds_the_card_in_its_own_account(tmp_path):
    """Order 100 sold one put in each of two accounts; the released version drew
    one card and filed its row under U2, the account the earliest fill it read
    was in. U2's card shows it, and U1's, the order's first fill, does not."""
    db = _journal(tmp_path, [
        ("t1", "U1", "100", "2026-09-01 10:00:00", -1, "O", None, "21"),
        ("t2", "U2", "100", "2026-09-01 10:00:00", -1, "O", None, "21")])
    _released(db, "100", "U2", "2026-09-01", "written on main")
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        notes = {c["anchor"]: c["note"] for c in _cards(_state(base))}
    assert notes == {"100": None, "t:t2": "written on main"}


def test_a_released_row_on_an_evening_fill_finds_its_card_by_trade_date(tmp_path):
    """IBKR dates a fill after its evening cutoff to the next trade date, which
    is what the released version recorded: B's refill filled at 20:03 on Sep 8,
    trade date Sep 9. Matched by the fill's clock day, the row fell through to
    A, the order's first fill, once the history import brought it."""
    db = _journal(tmp_path, [("t3", "U1", "5040", "2026-09-08 20:03:00", 1, "O", None, "21")])
    conn = connect(db)
    conn.execute("UPDATE trades SET trade_date = '2026-09-09' WHERE trade_id = 't3'")
    conn.commit()
    conn.close()
    _released(db, "5040", "U1", "2026-09-09", "B, by the released version")
    _insert(db, _GTC)
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        assert [c["note"] for c in _cards(_state(base))] == [
            None, "B, by the released version"]


def test_an_earlier_fill_joining_a_card_does_not_change_the_write_up_it_shows(tmp_path):
    """B is written up, then A. A roll joins the two cards, which shows A, filed
    under the earlier fill, and lists B; the reader revises A on the joined card.
    A history import then brings an older fill of A's contract: the card's first
    fill moved, neither row was its own any more, and the older row, B's stale
    one, took the card while the revised A was listed as unclaimed."""
    db = _journal(tmp_path, [("a1", "U1", "200", "2026-09-10 10:00:00", 1, "O", None, "21"),
                             ("b1", "U1", "300", "2026-09-12 10:00:00", 1, "O", None, "22")])
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        for anchor, note in (("300", "B"), ("200", "A")):
            assert _post(base, "/api/journal", _form(_state(base), anchor,
                                                     entry_note=note))[0] == 200
        conn = connect(db)
        conn.execute("UPDATE journal_entries SET created_at = '2026-09-01T00:00:00+00:00'"
                     " WHERE anchor_order_id = 't:b1'")
        conn.commit()
        conn.close()
        _insert(db, [("r1", "U1", "400", "2026-09-15 10:00:00", -1, "C", 10.0, "21"),
                     ("r2", "U1", "401", "2026-09-15 10:00:30", -1, "C", 10.0, "22")])
        (card,) = _cards(_state(base))
        assert _post(base, "/api/journal", _form(_state(base), card["anchor"],
                                                 entry_note="A, revised"))[0] == 200
        _insert(db, [("h1", "U1", "150", "2026-09-05 10:00:00", 1, "O", None, "21")])
        state = _state(base)
    assert [c["note"] for c in _cards(state)] == ["A, revised"]
    assert [o["entry_note"] for o in state["journal"]["orphans"]] == ["B"]


@pytest.mark.parametrize("then", ["link", "import"])
def test_a_released_write_up_keeps_its_card_when_the_cards_anchor_moves(tmp_path, then):
    """A carries a row the released version filed under 200; B is written up
    now. A roll joins them, the card shows A's row and the reader revises it.
    Then the card's anchor moves: a link to an older unwritten card (order
    100), or a history import of 200's first fill into a card of its own.
    Picked by the card's anchor, the card switched to B's row and listed the
    revised A as unclaimed."""
    db = _journal(tmp_path, [
        ("c1", "U1", "100", "2026-09-01 10:00:00", 1, "O", None, "23"),
        ("c2", "U1", "110", "2026-09-03 10:00:00", -1, "C", 5.0, "23"),
        ("b1", "U1", "300", "2026-09-05 10:00:00", 1, "O", None, "22"),
        ("a1", "U1", "200", "2026-09-10 10:00:00", 1, "O", None, "21")])
    _released(db, "200", "U1", "2026-09-10", "A, by the released version")
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        assert _post(base, "/api/journal", _form(_state(base), "300",
                                                 entry_note="B"))[0] == 200
        _insert(db, [("r1", "U1", "400", "2026-09-15 10:00:00", -1, "C", 10.0, "21"),
                     ("r2", "U1", "401", "2026-09-15 10:00:30", -1, "C", 10.0, "22")])
        assert _post(base, "/api/journal", _form(_state(base), "200",
                                                 entry_note="A, revised"))[0] == 200
        if then == "link":
            assert _post(base, "/api/links", {"anchor": "200", "joins": "100"})[0] == 200
        else:
            _insert(db, [("h1", "U1", "200", "2026-08-20 10:00:00", 1, "O", None, "21"),
                         ("h2", "U1", "150", "2026-08-22 10:00:00", -1, "C", 3.0, "21")])
        state = _state(base)
    assert "A, revised" in [c["note"] for c in _cards(state)]
    assert [o["entry_note"] for o in state["journal"]["orphans"]] == ["B"]


def test_a_link_from_the_second_card_of_an_order_joins_that_card(tmp_path):
    """B (the GTC order's second fill, closed by 5090) linked to C (5100) from
    B's card posted 5040, which landed on A. Stored under the two cards' first
    fills, it joins B and C."""
    db = _journal(tmp_path, [
        *_GTC[:1], ("t2", "U1", "5082", "2026-09-04 16:20:00", -1, "C", -100.0, "21", "Ep"),
        *_REFILL, ("t4", "U1", "5090", "2026-09-09 10:00:00", -1, "C", 40.0, "21"),
        ("t5", "U1", "5100", "2026-09-10 10:00:00", 1, "O", None, "22")])
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        b = next(c for c in _cards(_state(base)) if c["opened"] == "2026-09-08")
        status, wrote = _post(base, "/api/links", {"anchor": b["anchor"], "joins": "5100"})
        assert (status, wrote["pair"]) == (200, ["t:t3", "t:t5"])
        state = _state(base)
    joined = [lc for lc in state["lifecycles"] if lc["links"]]
    assert [lc["opened_at"][:10] for lc in joined] == ["2026-09-08"]
    assert len(state["lifecycles"]) == 2


def test_one_order_allocated_to_two_accounts_is_two_rows(tmp_path):
    """Order 100 sold one put in each of two accounts. The two cards answered to
    100 together, so they shared one row and one editor."""
    db = _journal(tmp_path, [
        ("t1", "U1", "100", "2026-09-01 10:00:00", -1, "O", None, "21"),
        ("t2", "U2", "100", "2026-09-01 10:00:00", -1, "O", None, "21")])
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        cards = _cards(_state(base))
        assert sorted(c["anchor"] for c in cards) == ["100", "t:t2"]
        for card in cards:
            _post(base, "/api/journal", _form(_state(base), card["anchor"],
                                              entry_note=f"on {card['anchor']}"))
        assert sorted(c["note"] for c in _cards(_state(base))) == ["on 100", "on t:t2"]
    assert _rows(db) == [("ibkr", "U1", "t:t1", "on 100"), ("ibkr", "U2", "t:t2", "on t:t2")]


def test_a_reversal_by_an_older_order_keeps_both_rows_through_a_history_import(tmp_path):
    """Long one from order 200; then GTC order 100, placed earlier, sells three
    through zero: one card for the long it closed and one for the short it
    opened, both answering to 100. Then a history import brings an older fill of
    the long (order 40), and each card keeps its own write-up."""
    db = _journal(tmp_path, [
        ("t1", "U1", "200", "2026-09-01 10:00:00", 1, "O", None, "21"),
        ("t2", "U1", "100", "2026-09-02 10:00:00", -3, "C;O", 30.0, "21")])
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        cards = {c["status"]: c for c in _cards(_state(base))}
        assert (cards["closed"]["anchor"], cards["open"]["anchor"]) == ("t:t1", "100")
        for card in cards.values():
            assert _post(base, "/api/journal", _form(
                _state(base), card["anchor"], entry_note=f"{card['status']} side"))[0] == 200
        _insert(db, [("h1", "U1", "40", "2026-08-15 10:00:00", 1, "O", None, "21")])
        after = {c["status"]: c for c in _cards(_state(base))}
    assert (after["closed"]["anchor"], after["open"]["anchor"]) == ("40", "100")
    assert (after["closed"]["note"], after["open"]["note"]) == ("closed side", "open side")
    assert _rows(db) == [("ibkr", "U1", "t:t1", "closed side"),
                         ("ibkr", "U1", "t:t2", "open side")]


def test_the_review_reads_each_write_up_once(tmp_path):
    """A and B, both closed, answered to 5040, so the review counted A's one
    write-up as two written cards."""
    db = _journal(tmp_path, [*_GTC, *_REFILL,
                             ("t4", "U1", "5090", "2026-09-09 10:00:00", -1, "C", 40.0, "21")])
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        _post(base, "/api/journal", _form(_state(base), "5040", entry_note="A"))
        review = _state(base)["journal"]["review"]
    assert (review["closed"], review["written"]) == (2, 1)


def test_a_new_write_up_is_dated_by_its_cards_first_fill(tmp_path):
    """The open date a row records is what makes it legible once no card shows
    it, and what a released row is found again by: the card's first fill day."""
    db = _journal(tmp_path, _GTC)
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        _, wrote = _post(base, "/api/journal", _form(_state(base), "5040", entry_note="A"))
    assert (wrote["entry"]["opened_on"], wrote["entry"]["underlying"]) == ("2026-09-01", "QQQ")


def test_a_write_naming_no_current_card_is_refused_and_keeps_the_text(tmp_path):
    """A page loaded before a statement regrouped its card posts an anchor no
    card answers to now: refused with 409 and nothing written. The reply asks the
    reader to copy their text before reloading, since reloading closes the
    editor; it told them to reload and save again, which lost it."""
    db = _journal(tmp_path, _GTC)
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        state = _state(base)
        for anchor in ("5082", "t:t2"):
            status, reply = _post(base, "/api/journal", _form(state, anchor, entry_note="x"))
            assert (status, reply["kind"]) == (409, "stale"), anchor
            assert "Copy what you typed" in reply["message"]
            assert "Nothing was saved" in reply["message"]
            assert "save again" not in reply["message"]
        assert _post(base, "/api/journal", dict(_form(state, "5040"), account="U2"))[0] == 409
        assert _post(base, "/api/journal", {"anchor": "t:nope", "lessons": "x"})[0] == 404
        status, reply = _post(base, "/api/links", {"anchor": "t:t2", "joins": "5040"})
        assert (status, reply["kind"]) == (409, "stale")
    assert _rows(db) == []


@pytest.mark.parametrize("shows", [5, True, "ibkr", {"a": 1}, ["ibkr", "U1"],
                                   ["ibkr", "U1", "5040", "x"], [["ibkr"], "U1", "5040"]],
                         ids=["int", "true", "text", "object", "two", "four", "nested"])
def test_a_shows_that_is_not_a_row_key_is_refused_with_400(tmp_path, shows):
    """`shows: 5` raised a TypeError, a 500."""
    db = _journal(tmp_path, _GTC)
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        status, reply = _post(base, "/api/journal", _form(_state(base), "5040",
                                                          entry_note="x", shows=shows))
    assert (status, reply["kind"]) == (400, "journal")
    assert _rows(db) == []


def test_one_order_in_two_categories_files_neither(tmp_path):
    """IBKR does not give one order id to an option and a stock, but if it did,
    both cards would answer to it: neither shows a row filed under it, which is
    listed as unclaimed, and a write naming it is refused rather than filed on
    whichever came first."""
    db = _journal(tmp_path, [("t1", "U1", "700", "2026-09-01 10:00:00", 1, "O", None, "21")])
    _insert(db, [("s1", "U1", "700", "2026-09-01 10:00:00", 100, "O", None, "S1")],
            category="STK")
    _released(db, "700", "U1", "2026-09-01", "on main")
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        state = _state(base)
        assert "700" not in state["journal"]["entries"]
        assert [o["entry_note"] for o in state["journal"]["orphans"]] == ["on main"]
        status, reply = _post(base, "/api/journal", {"anchor": "700", "lessons": "x"})
    assert status == 409 and "names 2 current positions" in reply["message"]


def test_one_click_opens_one_editor(tmp_path):
    """The two GTC cards shared an anchor, so opening one card's editor opened
    both, with two `#j-entry_note` boxes, and Save read the first card's."""
    from test_web import _node_run, _page_const, _page_fns, _static

    db = _journal(tmp_path, [*_GTC, *_REFILL])
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        state = _state(base)
    second = next(lc for lc in state["lifecycles"] if lc["status"] == "open")
    drawn = _node_run([
        f"import {{esc}} from '{_static('format.js')}';",
        f"const S={{state:{json.dumps(state)},jrnl:{json.dumps(second['anchor'])},lnk:null}};",
        "const infoTip=()=>'';",
        _page_const("JENTRY"), _page_const("JTRIGGERS"), _page_const("JADHERE"),
        _page_const("JFIELDS"),
        *_page_fns("jcount", "journalRow", "rollCandidates", "linkForm", "jfield",
                   "journalForm"),
        "console.log(JSON.stringify(S.state.lifecycles.map(lc=>",
        "  (journalRow(lc).match(/id=\"j-entry_note\"/g)||[]).length)));",
    ])
    assert sorted(drawn) == [0, 1]


def test_the_page_says_which_row_the_card_shows_when_it_saves():
    """Save posts the card's anchor and `shows`, the key of the entry drawn on
    the card, which the server needs to edit that row rather than add one."""
    from test_web import _node_run, _page_const, _page_fns, _static

    entry = {"broker": "ibkr", "account_id": "U1", "anchor": "100", "lessons": "x"}
    posted = _node_run([
        f"import {{esc}} from '{_static('format.js')}';",
        "function note(){} async function load(){}",
        "const b={dataset:{jsave:'50'},disabled:false,classList:{add(){},remove(){}}};",
        "const document={querySelectorAll:sel=>sel==='[data-jsave]'?[b]:[]};",
        "const $=()=>({value:''});",
        f"const S={{state:{{journal:{{entries:{{'50':{json.dumps(entry)}}}}}}}}};",
        "let body=null;",
        "const fetch=async(_url,init)=>{body=JSON.parse(init.body);",
        "  return {json:async()=>({ok:true,entry:null})};};",
        _page_const("JFIELDS"), _page_const("JENTRY"),
        *_page_fns("bindJournal"),
        "bindJournal(); await b.onclick();",
        "console.log(JSON.stringify([body.anchor, body.shows]));",
    ])
    assert posted == ["50", ["ibkr", "U1", "100"]]


#: A holding from before the archive, closed by order 100; a history import
#: later brings its opening fill, order 50, so the card answers to 50.
_CLOSED = [("t1", "U1", "100", "2026-09-02 10:00:00", -1, "C", 50.0, "21")]
_OPENED = [("h1", "U1", "50", "2026-08-01 10:00:00", 1, "O", None, "21")]


@pytest.mark.parametrize("released", [False, True], ids=["filed under t:", "released"])
def test_a_write_edits_the_row_its_card_shows_in_place(tmp_path, released):
    """Clearing a sentence on a card whose anchor moved wrote a second row and
    left the first, with the sentence, in the unclaimed notes; then it re-keyed
    the row, which moved it to whichever card the new key named. It edits the
    row under its own key, and its recorded open date stays."""
    db = _journal(tmp_path, _CLOSED)
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        if released:
            _released(db, "100", "U1", "2026-09-02", "took profits")
        else:
            _post(base, "/api/journal", _form(_state(base), "100", entry_note="took profits"))
        key = ("ibkr", "U1", "100" if released else "t:t1")
        dated = _full_rows(db)[key]
        _insert(db, _OPENED)
        state = _state(base)
        assert state["journal"]["entries"]["50"]["anchor"] == key[2]
        status, reply = _post(base, "/api/journal", _form(state, "50", lessons="later"))
        assert status == 200, reply
        state = _state(base)
    assert state["journal"]["orphans"] == []
    assert state["journal"]["entries"]["50"]["lessons"] == "later"
    assert list(_full_rows(db)) == [key]
    assert _full_rows(db)[key][4] == dated[4], "the open date it records was rewritten"


def test_emptying_a_write_up_deletes_the_row_its_card_shows(tmp_path):
    """Emptying every field answered "Entry deleted" and left the row under 100,
    which came back on the next load."""
    db = _journal(tmp_path, _CLOSED)
    _released(db, "100", "U1", "2026-09-02", "took profits")
    _insert(db, _OPENED)
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        status, reply = _post(base, "/api/journal", _form(
            _state(base), "50", **dict.fromkeys(FIELDS, "")))
        assert (status, reply["entry"]) == (200, None)
        state = _state(base)
    assert (state["journal"]["entries"], state["journal"]["orphans"]) == ({}, [])
    assert _rows(db) == []


def test_a_write_that_names_another_row_than_the_card_shows_is_refused(tmp_path):
    """A page that names a row the card does not show, or names none while the
    card shows one filed elsewhere, could only edit the wrong text."""
    db = _journal(tmp_path, _CLOSED)
    _released(db, "100", "U1", "2026-09-02", "took profits")
    _insert(db, _OPENED)
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        state = _state(base)
        for body in (_form(state, "50", shows=None),
                     _form(state, "50", shows=["ibkr", "U1", "999"]),
                     {"anchor": "50", "lessons": "x"}):
            status, reply = _post(base, "/api/journal", body)
            assert (status, reply["kind"]) == (409, "stale"), body
    assert _rows(db) == [("ibkr", "U1", "100", "took profits")]


#: Z, an older position on another contract (30, closed by 35), and Y, opened
#: by 100 and closed by 110.
_Z = [("z1", "U1", "30", "2026-07-01 10:00:00", 1, "O", None, "22"),
      ("z2", "U1", "35", "2026-07-20 10:00:00", -1, "C", 20.0, "22")]
_Y = [("y1", "U1", "100", "2026-09-02 10:00:00", 1, "O", None, "21"),
      ("y2", "U1", "110", "2026-09-09 10:00:00", -1, "C", 50.0, "21")]


def test_link_edit_unlink_leaves_each_write_up_on_its_own_card(tmp_path):
    """Z, with no write-up, linked to Y, with one; the joined card's write-up is
    edited; the link is undone. The edit re-keyed Y's row to the joined card's
    anchor, 30, so the undo left Y's text on Z and Y not written up."""
    db = _journal(tmp_path, _Z + _Y)
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        _post(base, "/api/journal", _form(_state(base), "100", entry_note="Y note",
                                          lessons="Y lesson"))
        status, wrote = _post(base, "/api/links", {"anchor": "30", "joins": "100"})
        assert status == 200, wrote
        (joined,) = _cards(_state(base))
        assert (joined["anchor"], joined["note"]) == ("30", "Y note")
        assert _post(base, "/api/journal", _form(_state(base), "30",
                                                 lessons="rewritten on the joined card"))[0] == 200
        pair = _cards(_state(base))[0]["links"][0]
        assert _post(base, "/api/links", {"anchor": pair[0], "joins": pair[1],
                                          "unlink": True})[0] == 200
        z, y = _cards(_state(base))
    assert (z["anchor"], z["note"]) == ("30", None)
    assert (y["anchor"], y["note"], y["lessons"]) == ("100", "Y note",
                                                      "rewritten on the joined card")


def test_a_link_between_two_written_cards_is_refused_whatever_ids_they_show(tmp_path):
    """Y shows the write-up the released version filed under 100, since its
    anchor moved to 50, and Z has its own. The guard only looked for a row under
    the later anchor, 50, found none, and linked them, and Y's write-up stopped
    showing anywhere."""
    db = _journal(tmp_path, [*_Z, *_CLOSED])
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        _post(base, "/api/journal", _form(_state(base), "30", entry_note="Z: earnings play"))
        _released(db, "100", "U1", "2026-09-02", "Y: took profits")
        _insert(db, _OPENED)
        status, reply = _post(base, "/api/links", {"anchor": "50", "joins": "30"})
        state = _state(base)
    assert (status, reply["ok"]) == (409, False)
    assert sorted(c["note"] for c in _cards(state)) == ["Y: took profits", "Z: earnings play"]
    assert state["journal"]["orphans"] == []


def test_a_link_where_one_card_has_a_write_up_keeps_it_showing(tmp_path):
    """The joined card shows the one write-up, whichever card it was on."""
    db = _journal(tmp_path, [*_Z, *_CLOSED])
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        _post(base, "/api/journal", _form(_state(base), "100", entry_note="Y: took profits"))
        status, _reply = _post(base, "/api/links", {"anchor": "100", "joins": "30"})
        state = _state(base)
    assert status == 200
    assert [c["note"] for c in _cards(state)] == ["Y: took profits"]
    assert state["journal"]["orphans"] == []


# ------------------------------------------------------------------- fuzz
#
# Random option journals arriving as statements do, then a history import of
# older fills, written up through the handlers after each, against the
# properties the cases above break.


def _random_fills(rnd: random.Random) -> tuple[list[tuple], list[tuple]]:
    """A journal of re-entries, GTC refills, allocations and reversals, and a
    history import: older fills, some of them earlier fills of its orders."""
    fills: list[tuple] = []
    held: dict[tuple[str, str], int] = {}
    order = 1000
    when = 86400 * 10
    for _ in range(rnd.randint(3, 9)):
        order += rnd.choice([1, 1, 2, 30])
        when += rnd.choice([20, 40, 3600, 86400])
        account = rnd.choice(["U1", "U1", "U2"])
        conid = rnd.choice(["21", "22"])
        kind = rnd.choice(["open", "close", "flip", "gtc", "alloc", "pre"])
        oid = str(order)
        if kind == "gtc" and fills:
            oid = rnd.choice(fills)[2]
        accounts = ["U1", "U2"] if kind == "alloc" else [account]
        for acct in accounts:
            have = held.get((acct, conid), 0)
            qty = (-(have or 1) if kind == "close" else -2 * (have or 1) if kind == "flip"
                   else -1 if kind == "pre" else 1)
            after = have + qty
            open_close = ("C;O" if have and after and (have > 0) != (after > 0)
                          else "C" if kind == "pre" or (have and abs(after) < abs(have))
                          else "O")
            fills.append((f"t{len(fills) + 1}", acct, oid, _at(when), qty, open_close,
                          5.0 if "C" in open_close else None, conid))
            held[(acct, conid)] = after
    older = []
    for k in range(rnd.randint(0, 3)):
        oid = rnd.choice(fills)[2] if rnd.random() < 0.3 else str(500 + k)
        older.append((f"h{k + 1}", rnd.choice(["U1", "U2"]), oid,
                      _at(86400 * rnd.randint(1, 8) + 3600 * k), 1, "O", None,
                      rnd.choice(["21", "22"])))
    return fills, older


def _at(seconds: int) -> str:
    return (f"2026-09-{1 + seconds // 86400:02d} {10 + seconds % 86400 // 3600:02d}:"
            f"{seconds % 3600 // 60:02d}:{seconds % 60:02d}")


def _check(db: Path, state: dict) -> None:
    """Anchors and keys unique; each row on one card at most, and a `t:` row on
    the card holding its fill."""
    from optjournal.serialize import journal_cards

    conn = connect(db)
    cards = {c.anchor: c for c in journal_cards(conn)}
    conn.close()
    anchors = [lc["anchor"] for lc in state["lifecycles"] if lc["anchor"]]
    assert len(anchors) == len(set(anchors)), anchors
    keys = [c.key for c in cards.values() if c.key]
    assert len(keys) == len(set(keys)), keys
    shown = {anchor: (e["broker"], e["account_id"], e["anchor"])
             for anchor, e in state["journal"]["entries"].items()}
    assert len(shown) == len(set(shown.values()))
    for anchor, (broker, _account, filed) in shown.items():
        if filed.startswith("t:"):
            assert (broker, filed[2:]) in cards[anchor].parts, (anchor, filed)


@pytest.mark.parametrize("seed", range(12))
def test_no_write_through_the_handlers_touches_a_row_another_card_shows(tmp_path, seed):
    """Keys are unique across the current cards, every stored row is shown on at
    most one card and a `t:` row on the card holding its fill, and a write from
    one card changes no row another card shows, through a statement, a later
    one and a history import, with a write on every card after each."""
    rnd = random.Random(seed)
    fills, older = _random_fills(rnd)
    cut = rnd.randint(1, len(fills))
    db = _journal(tmp_path, fills[:cut])
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        for step, batch in enumerate(([], fills[cut:], older)):
            _insert(db, batch)
            state = _state(base)
            _check(db, state)
            for anchor in [lc["anchor"] for lc in state["lifecycles"] if lc["anchor"]]:
                before = {a: (e["broker"], e["account_id"], e["anchor"])
                          for a, e in state["journal"]["entries"].items()}
                rows = _full_rows(db)
                status, reply = _post(base, "/api/journal", _form(
                    state, anchor, lessons=f"{step} {anchor}"))
                assert status == 200, (anchor, reply)
                state = _state(base)
                after = _full_rows(db)
                for other, row in before.items():
                    if other != anchor:
                        assert after.get(row) == rows.get(row), (anchor, other)
                _check(db, state)
