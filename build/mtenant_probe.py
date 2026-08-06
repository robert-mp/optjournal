"""Throwaway probe: what happens when two accounts land in one journal."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from optjournal.db import open_journal  # noqa: E402
from optjournal.ingest import ASSET_FILTER_ALL, ingest_file  # noqa: E402

db = ROOT / "build" / "mixed-probe.db"
db.unlink(missing_ok=True)
Path(str(db) + "-wal").unlink(missing_ok=True)
real = sorted((ROOT / "raw").glob("activity-*.xml"))[-1]
demo = sorted((ROOT / "demo").glob("activity-demo-*.xml"))[-1]
with open_journal(db) as conn:
    r1 = ingest_file(conn, real, assets=ASSET_FILTER_ALL)
    r2 = ingest_file(conn, demo, assets=ASSET_FILTER_ALL)
    print("real trades:", r1.trades_inserted, "demo trades:", r2.trades_inserted)
    print("real snaps:", r1.positions_written, "demo snaps:", r2.positions_written)
    print("real NAV:", r1.equity_summaries_written, "demo NAV:", r2.equity_summaries_written)
    for q in (
        "SELECT account_id, COUNT(*) FROM trades GROUP BY account_id",
        "SELECT COUNT(*) FROM trade_orders",
        "SELECT report_date, COUNT(*), COUNT(DISTINCT account_id)"
        " FROM position_snapshots GROUP BY report_date ORDER BY report_date DESC LIMIT 4",
        "SELECT COUNT(*), COUNT(DISTINCT account_id) FROM current_option_positions",
        "SELECT account_id, COUNT(*) FROM equity_summaries GROUP BY account_id",
        "SELECT COUNT(*) FROM equity_summaries",
        "SELECT base_currency, account_id, source_file FROM statements",
    ):
        print(q, "->", [tuple(r) for r in conn.execute(q)])

    # What the whole-account layers would report
    from optjournal.history import build_history  # noqa: E402
    from optjournal.stats import month_stats  # noqa: E402

    rep = build_history(conn, asset_category="OPT")
    print("episodes closed/open:", len(rep.closed), len(rep.open))
    st = month_stats(conn, None, asset_category="OPT", report=rep)
    print("net_liq_base:", st.net_liq_base, "net_liq_date:", st.net_liq_date)
    print("stats fields:", {k: v for k, v in vars(st).items() if "liq" in k or "real" in k})
