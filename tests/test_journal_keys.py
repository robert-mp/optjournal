"""Which journal row a card is filed under, through the real handlers.

A card's row is `(broker, account_id, anchor)` and no two current cards share
one (`campaigns`, "What a card is filed under"). Each case here is one a review
found two cards sharing a row in: writing one card's plan overwrote the other's,
a link posted from one landed on the other, and the review counted one write-up
twice. Driven through `serve_ephemeral`, so the page's posts meet the handlers
that file them.
"""

from __future__ import annotations

import json
import random
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import add_statement, connect_migrated

from optjournal import web
from optjournal.db import connect


def _insert(db: Path, fills: list[tuple]) -> None:
    """Option fills `(trade, account, order, at, qty, open_close, realised,
    conid[, notes])`, all on QQQ."""
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
            " VALUES ('ibkr',?,?,?,?,?,?,?,'OPT',?,?,'QQQ','C',500,'2026-12-18',"
            "100,?,?,?,?,1.0,'USD',1.0,?,?,-1.0,-1.0,?,?,'{}','t.xml','now')",
            (tid, tid, tid, oid, account, at[:10], at, f"QQQ {conid}", conid,
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
    """Each card's anchor, opening date, status and the note it shows."""
    entries = state["journal"]["entries"]
    return sorted(({"anchor": lc["anchor"], "opened": str(lc["opened_at"])[:10],
                    "status": lc["status"],
                    "note": (entries.get(lc["anchor"] or "") or {}).get("entry_note")}
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


#: GTC order 5040 opens A, which expires; the same order fills again a week
#: later and opens B.
_GTC = [("t1", "U1", "5040", "2026-09-01 10:34:11", 1, "O", None, "21"),
        ("t2", "U1", "5082", "2026-09-03 16:20:00", -1, "C", -200.0, "21", "Ep")]
_REFILL = [("t3", "U1", "5040", "2026-09-08 09:50:02", 1, "O", None, "21")]


def test_a_gtc_order_that_fills_again_files_the_new_card_under_its_own_row(tmp_path):
    """Both cards answered to 5040, so B showed A's write-up, and writing B's
    plan posted 5040 and overwrote A's row in the database."""
    db = _journal(tmp_path, _GTC)
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        (a,) = _cards(_state(base))
        assert _post(base, "/api/journal", {"anchor": a["anchor"],
                                            "entry_note": "plan A"})[0] == 200
        _insert(db, _REFILL)
        first, second = _cards(_state(base))
        assert first["anchor"] == a["anchor"] == "5040"
        assert second["anchor"] == "5040~t3"
        assert (first["note"], second["note"]) == ("plan A", None)
        status, wrote = _post(base, "/api/journal", {"anchor": second["anchor"],
                                                     "entry_note": "plan B"})
        assert (status, wrote["entry"]["entry_note"]) == (200, "plan B")
        assert [c["note"] for c in _cards(_state(base))] == ["plan A", "plan B"]
    assert _rows(db) == [("ibkr", "U1", "5040", "plan A"),
                         ("ibkr", "U1", "5040~t3", "plan B")]


def test_a_link_from_the_second_card_of_an_order_joins_that_card(tmp_path):
    """B (the GTC order's second fill, closed by 5090) linked to C (5100) from
    B's card posted 5040, which landed on A."""
    db = _journal(tmp_path, [
        *_GTC[:1], ("t2", "U1", "5082", "2026-09-04 16:20:00", -1, "C", -100.0, "21", "Ep"),
        *_REFILL, ("t4", "U1", "5090", "2026-09-09 10:00:00", -1, "C", 40.0, "21"),
        ("t5", "U1", "5100", "2026-09-10 10:00:00", 1, "O", None, "22")])
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        b = next(c for c in _cards(_state(base)) if c["opened"] == "2026-09-08")
        assert _post(base, "/api/links", {"anchor": b["anchor"], "joins": "5100"})[0] == 200
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
        assert sorted(c["anchor"] for c in cards) == ["100", "100~t2"]
        for card in cards:
            _post(base, "/api/journal", {"anchor": card["anchor"],
                                         "entry_note": f"on {card['anchor']}"})
        assert sorted(c["note"] for c in _cards(_state(base))) == ["on 100", "on 100~t2"]
    assert _rows(db) == [("ibkr", "U1", "100", "on 100"),
                         ("ibkr", "U2", "100~t2", "on 100~t2")]


def test_a_reversal_by_an_older_order_keeps_both_rows_through_a_history_import(tmp_path):
    """Long one from order 200; then GTC order 100, placed earlier, sells three
    through zero: one card for the long it closed and one for the short it
    opened, both answering to 100. Then a history import brings an older fill of
    the long (order 40), so the long answers to 40, and each card keeps its own
    write-up: neither takes the other's row as its anchor moves."""
    db = _journal(tmp_path, [
        ("t1", "U1", "200", "2026-09-01 10:00:00", 1, "O", None, "21"),
        ("t2", "U1", "100", "2026-09-02 10:00:00", -3, "C;O", 30.0, "21")])
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        cards = {c["status"]: c for c in _cards(_state(base))}
        assert (cards["closed"]["anchor"], cards["open"]["anchor"]) == ("100~t2~C", "100")
        for card in cards.values():
            assert _post(base, "/api/journal", {
                "anchor": card["anchor"], "entry_note": f"{card['status']} side"})[0] == 200
        _insert(db, [("h1", "U1", "40", "2026-08-15 10:00:00", 1, "O", None, "21")])
        after = {c["status"]: c for c in _cards(_state(base))}
    assert (after["closed"]["anchor"], after["open"]["anchor"]) == ("40", "100")
    assert (after["closed"]["note"], after["open"]["note"]) == ("closed side", "open side")
    assert _rows(db) == [("ibkr", "U1", "100", "open side"),
                         ("ibkr", "U1", "100~t2~C", "closed side")]


def test_the_review_reads_each_write_up_once(tmp_path):
    """A and B, both closed, answered to 5040, so the review counted A's one
    write-up as two written cards."""
    db = _journal(tmp_path, [*_GTC, *_REFILL,
                             ("t4", "U1", "5090", "2026-09-09 10:00:00", -1, "C", 40.0, "21")])
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        _post(base, "/api/journal", {"anchor": "5040", "entry_note": "A"})
        review = _state(base)["journal"]["review"]
    assert (review["closed"], review["written"]) == (2, 1)


def test_a_write_naming_no_current_card_is_refused(tmp_path):
    """A page loaded before a statement regrouped its card posts an anchor no
    card answers to now: refused with 409 and nothing written, where it would
    otherwise file a row no card shows."""
    db = _journal(tmp_path, _GTC)
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        for anchor in ("5040~t9", "5082~t1"):
            status, reply = _post(base, "/api/journal", {"anchor": anchor,
                                                         "entry_note": "x"})
            assert (status, reply["kind"]) == (409, "stale"), anchor
            assert "Nothing was changed" in reply["message"]
        status, reply = _post(base, "/api/journal", {"anchor": "5040", "account": "U2",
                                                     "entry_note": "x"})
        assert status == 409
        status, reply = _post(base, "/api/links", {"anchor": "5040~t9", "joins": "5082"})
        assert (status, reply["kind"]) == (409, "stale")
    assert _rows(db) == []


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


# ------------------------------------------------------------------- fuzz
#
# Random option journals arriving as statements do, written up through the
# handlers after each, against the properties the cases above break.


def _random_fills(rnd: random.Random) -> list[tuple]:
    """A journal of re-entries, GTC refills, allocations and reversals."""
    fills: list[tuple] = []
    held: dict[tuple[str, str], int] = {}
    order = 1000
    when = 0
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
            at = f"2026-09-{1 + when // 86400:02d} {10 + when % 86400 // 3600:02d}:" \
                 f"{when % 3600 // 60:02d}:{when % 60:02d}"
            fills.append((f"t{len(fills) + 1}", acct, oid, at, qty, open_close,
                          5.0 if "C" in open_close else None, conid))
            held[(acct, conid)] = after
    return fills


def _shown_rows(state: dict) -> dict[str, tuple]:
    """The row each card shows, by anchor: `(broker, account, filed anchor)`."""
    return {anchor: (e.get("broker", "ibkr"), e["account_id"], e["anchor"])
            for anchor, e in state["journal"]["entries"].items()}


@pytest.mark.parametrize("seed", range(12))
def test_no_write_through_the_handlers_touches_a_row_another_card_shows(tmp_path, seed):
    """Keys are unique across the current cards, every stored row is shown on at
    most one card, and a write from one card changes no row another card shows,
    through two statements and a write on every card after each."""
    rnd = random.Random(seed)
    fills = _random_fills(rnd)
    cut = rnd.randint(1, len(fills))
    db = _journal(tmp_path, fills[:cut])
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        for step in range(2):
            if step:
                _insert(db, fills[cut:])
            state = _state(base)
            anchors = [lc["anchor"] for lc in state["lifecycles"] if lc["anchor"]]
            assert len(anchors) == len(set(anchors)), anchors
            for anchor in anchors:
                before = _shown_rows(state)
                rows = _full_rows(db)
                status, _reply = _post(base, "/api/journal", {
                    "anchor": anchor, "lessons": f"{step} {anchor}"})
                assert status == 200, (anchor, _reply)
                state = _state(base)
                after = _full_rows(db)
                for other, row in before.items():
                    if other != anchor:
                        assert after.get(row) == rows.get(row), (anchor, other)
                shown = list(_shown_rows(state).values())
                assert len(shown) == len(set(shown))
