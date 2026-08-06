"""The CLI's own argument decoding, which nothing tested.

`cli.py` is wiring, so its logic is thin -- but the thin parts sit between the
user's intent and what gets stored, and a defect there is silent by construction:
argparse succeeds, the command runs, the exit code is 0, and the database is
empty.

That is not hypothetical. Mutation-testing found that dropping the `"ALL"`
sentinel from `_asset_filter` -- so `"ALL"` is decoded as a literal asset category
rather than as "every category" -- makes `optjournal ingest` report
`trades +0 (filtered 9)` per statement, store **zero** trades, positions and
securities, and exit **0**. All 597 tests passed. They passed because every one of
them calls `ingest_file(assets=ASSET_FILTER_ALL)` directly, handing over the
already-decoded constant and never exercising the decoder that turns a
command-line string into it.

So these tests are deliberately at the boundary the suite was blind to: the
string the user types, and the tuple the ingest receives.
"""

from __future__ import annotations

import pytest
from conftest import STATEMENTS, connect_migrated

from optjournal.cli import _asset_filter, main
from optjournal.ingest import ASSET_FILTER_ALL


def test_all_decodes_to_the_every_category_sentinel():
    """`ASSET_FILTER_ALL` is the empty tuple, and that is the whole trap.

    `assets=()` reads as "store nothing" but means "store everything" -- the
    ingest treats an empty filter as no filter. So decoding `"ALL"` to a literal
    `("ALL",)` does not fail loudly; it matches no category and silently filters
    every row out. Asserted by identity to the constant, not just by emptiness,
    so the intent is unmistakable.
    """
    assert _asset_filter("ALL") == ASSET_FILTER_ALL
    assert _asset_filter("ALL") == ()
    # Case and whitespace are the user's, not the decoder's.
    assert _asset_filter("all") == ASSET_FILTER_ALL
    assert _asset_filter("  All  ") == ASSET_FILTER_ALL
    # An omitted value means the same as ALL rather than "nothing".
    assert _asset_filter("") == ASSET_FILTER_ALL


def test_a_narrow_filter_is_upper_cased_and_split():
    """IBKR's categories are upper-case, so a lower-case `opt` must not silently
    match nothing -- the same failure mode as the sentinel, one layer down."""
    assert _asset_filter("OPT") == ("OPT",)
    assert _asset_filter("opt") == ("OPT",)
    assert _asset_filter("OPT,STK") == ("OPT", "STK")
    assert _asset_filter(" opt , stk ") == ("OPT", "STK")
    # A trailing or doubled comma is a typo, not an empty category.
    assert _asset_filter("OPT,,STK") == ("OPT", "STK")
    assert _asset_filter("OPT,") == ("OPT",)


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_the_default_ingest_stores_trades_rather_than_filtering_them_all_out(tmp_path):
    """End to end through `main`, because that is where the blindness was.

    The unit assertions above would have caught the sentinel defect, but only
    because they exist now. This one closes the more general hole: it drives the
    real command with its real default and asserts rows LANDED, so any future
    argument-decoding change that quietly stores nothing fails here rather than at
    a terminal a week later.

    Asserts a positive count, not merely exit 0 -- exit 0 with an empty database
    is precisely what the defect produced.
    """
    db = tmp_path / "cli.db"
    code = main(["ingest", "--archive", str(STATEMENTS[0].parent), "--db", str(db)])
    assert code == 0, "the default ingest should succeed"

    conn = connect_migrated(db)
    trades = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
    assert trades > 0, (
        "the default --assets stored no trades at all; the ALL sentinel is being "
        "decoded as a literal category and filtering every row out"
    )
    # Categories beyond options prove nothing was narrowed: the archive holds
    # stock and FX too, and storing only OPT would also read as "it worked".
    categories = {
        r["asset_category"]
        for r in conn.execute("SELECT DISTINCT asset_category FROM trades")
    }
    assert len(categories) > 1, (
        f"only {categories} stored -- the default must not narrow, since a row "
        "dropped at ingest costs a re-ingest to recover"
    )


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_an_explicit_narrow_filter_really_narrows(tmp_path):
    """The other direction: `--assets OPT` must filter, or the flag is a no-op.

    Without this, a decoder that returned `ASSET_FILTER_ALL` for everything would
    pass the test above and silently ignore the user's narrowing.
    """
    db = tmp_path / "narrow.db"
    assert main(["ingest", "--archive", str(STATEMENTS[0].parent),
                 "--db", str(db), "--assets", "OPT"]) == 0
    conn = connect_migrated(db)
    categories = {
        r["asset_category"]
        for r in conn.execute("SELECT DISTINCT asset_category FROM trades")
    }
    assert categories == {"OPT"}, f"--assets OPT stored {categories}"
