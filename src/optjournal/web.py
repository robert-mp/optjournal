"""Local web UI for the options journal.

Deliberately a stdlib HTTP server and one self-contained HTML page. The whole
project has no runtime dependency beyond py_ibkr and keyring, and a journal
that reads a 220KB SQLite file for a two-position book does not justify a web
framework, a build step or a node_modules.

SECURITY. This binds to 127.0.0.1 only and has no authentication. That is a
deliberate pair: the page exposes an entire brokerage account -- positions,
realised P&L, account costs -- and one endpoint spends real IBKR requests, so
it must never be reachable off-host. The bind address is passed explicitly
rather than defaulted, and `serve()` refuses anything that is not a loopback
address. Do not put this behind a reverse proxy without adding auth first.

The page reads a single /api/state payload rather than one endpoint per panel.
At this data volume the whole journal is a few KB of JSON, so one round trip is
simpler than five and the panels can never disagree with each other.
"""

from __future__ import annotations

import dataclasses
import http.server
import ipaddress
import json
import logging
import socket
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from optjournal import __version__
from optjournal.analysis import analyse
from optjournal.db import connect, migrate
from optjournal.flex import (
    FETCH_COOLDOWN_S,
    FetchCooldown,
    TokenMissing,
    cooldown_remaining,
    fetch,
    last_fetch,
    load,
)
from optjournal.history import build_history
from optjournal.ingest import ASSET_FILTER_OPTIONS, ingest_file
from optjournal.render import (
    costs_data,
    history_data,
    newest_statement,
    orders_data,
    positions_data,
    statements_data,
)

__all__ = ["build_state", "serve"]

log = logging.getLogger(__name__)

#: Only loopback. Checked rather than documented, because the cost of getting
#: this wrong is publishing an unauthenticated brokerage dashboard onto a
#: network.
def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_state(
    *,
    db_path: Path,
    archive_dir: Path,
    query_id: str | None,
    asset_category: str = "OPT",
) -> dict[str, Any]:
    """Everything the page renders, in one JSON-safe payload.

    Opens its own connection: sqlite3 objects cannot cross threads and the
    server is threaded, so a shared handle would fail intermittently under the
    one condition nobody tests for.
    """
    conn = connect(db_path)
    try:
        migrate(conn)
        state: dict[str, Any] = {
            "version": __version__,
            "generated_at": _now(),
            "asset_category": asset_category,
            "db": str(db_path),
            "archive": str(archive_dir),
            "positions": positions_data(conn),
            "orders": orders_data(conn),
            "history": history_data(build_history(conn, asset_category=asset_category)),
            "statements": statements_data(archive_dir, conn),
        }
    finally:
        conn.close()

    newest = newest_statement(archive_dir)
    if newest is not None:
        try:
            reports = [analyse(s) for s in load(newest).FlexStatements]
            state["costs"] = [costs_data(r) for r in reports]
            state["costs_source"] = newest.name
        except Exception as exc:  # pragma: no cover - defensive
            # A malformed archive must not blank the whole page; the rest of
            # the state is read from SQLite and is still valid.
            log.warning("cost report unavailable: %s", exc)
            state["costs"] = []
            state["costs_error"] = str(exc)
    else:
        state["costs"] = []

    state["sync"] = {
        "query_id": query_id,
        "configured": bool(query_id),
        "last_fetch": last_fetch(archive_dir, query_id) if query_id else None,
        "cooldown_s": FETCH_COOLDOWN_S,
        "cooldown_remaining_s": (
            cooldown_remaining(archive_dir, query_id) if query_id else 0
        ),
    }
    return state


def _do_sync(
    *, db_path: Path, archive_dir: Path, query_id: str, assets: tuple[str, ...]
) -> dict[str, Any]:
    """Fetch, archive and ingest. Spends an IBKR request unless refused."""
    started = _now()
    try:
        result = fetch(query_id, archive_dir=archive_dir)
    except FetchCooldown as exc:
        return {
            "ok": False,
            "kind": "cooldown",
            "retry_after_s": exc.retry_after_s,
            "message": str(exc),
        }
    except TokenMissing as exc:
        return {"ok": False, "kind": "config", "message": str(exc)}

    conn = connect(db_path)
    try:
        migrate(conn)
        ingested = ingest_file(conn, result.raw_path, assets=assets)
        new_trades = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE first_seen_at >= ?", (started,)
        ).fetchone()["n"]
        new_cash = conn.execute(
            "SELECT COUNT(*) AS n FROM cash_transactions WHERE first_seen_at >= ?",
            (started,),
        ).fetchone()["n"]
    finally:
        conn.close()

    return {
        "ok": True,
        "kind": "synced",
        "archive": result.raw_path.name,
        "reused_archive": result.duplicate_of is not None,
        "already_ingested": ingested.already_ingested,
        "duplicate_of": ingested.duplicate_of,
        "new_trades": new_trades,
        "new_cash": new_cash,
        "warnings": ingested.warnings,
    }


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"optjournal/{__version__}"
    # Config injected by serve(); class attributes keep the handler picklable
    # and avoid a closure-over-mutable-state bug.
    db_path: Path
    archive_dir: Path
    query_id: str | None
    assets: tuple[str, ...]
    _sync_lock = threading.Lock()

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # No external resources are loaded, so lock that down rather than
        # relying on the page never gaining a <script src>.
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, json.dumps(payload, default=str).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            try:
                self._json(200, build_state(
                    db_path=self.db_path,
                    archive_dir=self.archive_dir,
                    query_id=self.query_id,
                ))
            except sqlite3.OperationalError as exc:
                self._json(500, {"error": f"database not readable: {exc}"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path != "/api/sync":
            self._json(404, {"error": "not found"})
            return
        if not self.query_id:
            self._json(400, {
                "ok": False, "kind": "config",
                "message": "No Flex query ID configured. Start with "
                           "`optjournal serve --query-id <id>`.",
            })
            return
        # Serialised: two concurrent syncs would each spend a request and race
        # on the same archive directory.
        if not self._sync_lock.acquire(blocking=False):
            self._json(409, {"ok": False, "kind": "busy",
                             "message": "A sync is already running."})
            return
        try:
            self._json(200, _do_sync(
                db_path=self.db_path,
                archive_dir=self.archive_dir,
                query_id=self.query_id,
                assets=self.assets,
            ))
        finally:
            self._sync_lock.release()


def serve(
    *,
    db_path: Path,
    archive_dir: Path,
    query_id: str | None = None,
    assets: tuple[str, ...] = ASSET_FILTER_OPTIONS,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Serve the UI until interrupted. Loopback only, by construction."""
    if not _is_loopback(host):
        raise ValueError(
            f"refusing to bind {host!r}: this UI has no authentication and "
            f"exposes an entire brokerage account. Loopback only."
        )

    _Handler.db_path = db_path
    _Handler.archive_dir = archive_dir
    _Handler.query_id = query_id
    _Handler.assets = tuple(assets)

    class _Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET

    with _Server((host, port), _Handler) as httpd:
        actual = httpd.socket.getsockname()[1]
        print(f"optjournal UI on http://{host}:{actual}")
        print("  loopback only, no authentication -- do not expose this port")
        if not query_id:
            print("  no --query-id given, so Sync now is disabled")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


PAGE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>optjournal</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#252a34;--fg:#e6e8ee;--dim:#8b93a7;
--ok:#4ec9a0;--bad:#e56a6a;--warn:#e0b341;--accent:#5b9dd9}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}
header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;
padding:16px 20px;border-bottom:1px solid var(--line)}
h1{font-size:16px;margin:0;font-weight:600}
.dim{color:var(--dim)}.mono{font-variant-numeric:tabular-nums;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
main{padding:20px;display:grid;gap:16px;
grid-template-columns:repeat(auto-fit,minmax(420px,1fr));max-width:1600px}
section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
section.wide{grid-column:1/-1}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;
color:var(--dim);margin:0 0 10px;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:500;padding:4px 8px 6px;
border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:5px 8px;border-bottom:1px solid rgba(255,255,255,.04)}
tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right}
.pos{color:var(--ok)}.neg{color:var(--bad)}
button{background:var(--accent);color:#fff;border:0;border-radius:6px;
padding:7px 14px;font:inherit;font-weight:500;cursor:pointer}
button:disabled{background:#2b3240;color:var(--dim);cursor:not-allowed}
#msg{padding:8px 12px;border-radius:6px;font-size:13px;display:none;margin-top:10px}
#msg.show{display:block}
.tot{font-weight:600}
.tag{font-size:11px;padding:1px 6px;border-radius:4px;background:#252a34;color:var(--dim)}
.empty{color:var(--dim);font-style:italic;padding:6px 8px}
</style></head><body>
<header>
  <h1>optjournal</h1>
  <span class="dim" id="sub">loading…</span>
  <span style="flex:1"></span>
  <span class="dim mono" id="synced"></span>
  <button id="sync" disabled>Sync now</button>
</header>
<div style="padding:0 20px"><div id="msg"></div></div>
<main id="main"></main>
<script>
const $=s=>document.querySelector(s);
const money=(v,d=2)=>v==null?'-':Number(v).toLocaleString(undefined,
  {minimumFractionDigits:d,maximumFractionDigits:d});
const sign=v=>v==null?'':Number(v)>0?'pos':Number(v)<0?'neg':'';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function tbl(cols,rows,mk){
  if(!rows||!rows.length) return '<div class="empty">none</div>';
  return `<table><thead><tr>${cols.map(c=>
    `<th class="${c[1]||''}">${esc(c[0])}</th>`).join('')}</tr></thead><tbody>${
    rows.map(mk).join('')}</tbody></table>`;
}

function render(s){
  const c=(s.costs&&s.costs[0])||null;
  $('#sub').textContent=`${s.asset_category} · base ${c?c.base_currency:'EUR'}`
    +(c?` · ${c.from_date} → ${c.to_date}`:'');
  $('#synced').textContent=s.sync.last_fetch?`last fetch ${s.sync.last_fetch}`:'never fetched';

  const b=$('#sync'), rem=s.sync.cooldown_remaining_s;
  if(!s.sync.configured){b.disabled=true;b.textContent='Sync (no query id)';}
  else if(rem>0){b.disabled=true;b.textContent=`Sync in ${Math.ceil(rem/60)}m`;
    b.title=`Cooldown: ${rem}s left. Prevents spending an IBKR request for data that cannot have changed.`;}
  else {b.disabled=false;b.textContent='Sync now';b.title='Fetches from IBKR — spends one request';}

  const P=[];
  P.push(`<section><h2>Option book${s.positions.length&&s.positions[0].report_date
    ?` <span class="tag">${esc(s.positions[0].report_date)}</span>`:''}</h2>`+
    tbl([['symbol'],['qty','n'],['mark','n'],['value','n'],['unrealised','n'],['ccy']],
      s.positions,p=>`<tr><td class="mono">${esc(p.symbol)}</td>
      <td class="n mono ${sign(p.position)}">${p.position}</td>
      <td class="n mono">${money(p.mark_price)}</td>
      <td class="n mono">${money(p.position_value)}</td>
      <td class="n mono ${sign(p.fifo_pnl_unrealized)}">${money(p.fifo_pnl_unrealized)}</td>
      <td class="dim">${esc(p.currency)}</td></tr>`)+`</section>`);

  if(c){
    const T=c.totals;
    const rows=[['commission',T.commission_base,'stated'],
                ['fees',T.fees_base,'stated'],
                ['AutoFX markup',T.autofx_spread_base,'estimated @ 3 bps']];
    if(T.taxes_base) rows.splice(2,0,['taxes',T.taxes_base,'stated']);
    P.push(`<section><h2>Cost &amp; friction <span class="tag">${esc(s.costs_source||'')}</span></h2>
      <table><tbody>${rows.map(r=>`<tr><td>${r[0]}</td>
        <td class="n mono">${money(r[1])}</td><td class="dim">${r[2]}</td></tr>`).join('')}
      <tr class="tot"><td>total</td><td class="n mono">${money(T.friction_base)}</td>
        <td class="dim">${money(T.stated_friction_base)} stated +
        ${money(T.autofx_spread_base)} est.</td></tr></tbody></table>
      <h2 style="margin-top:14px">FX conversions</h2>`+
      tbl([['pair'],['n','n'],['notional','n'],['AFx','n'],['@3bps','n']],c.fx,
        f=>`<tr><td class="mono">${esc(f.symbol)}</td><td class="n mono">${f.conversions}</td>
        <td class="n mono">${money(f.notional_base)}</td>
        <td class="n mono">${f.autofx_conversions}</td>
        <td class="n mono">${money(f.autofx_spread_base)}</td></tr>`)+
      `<div class="dim" style="font-size:12px;margin-top:8px">${esc(c.fx_caveat||'')}</div>
      </section>`);
  }

  const h=s.history, ht=(h&&h.totals)||{};
  P.push(`<section><h2>Closed P&amp;L <span class="tag">${ht.closed_episodes||0} closed
    · ${ht.open_episodes||0} open${ht.win_rate!=null
      ? ` · ${ht.wins}W/${ht.losses}L`:''}</span></h2>`+
    tbl([['symbol'],['opened'],['closed'],['qty','n'],['realised','n'],['status']],
      (h&&h.closed)||[],
      e=>`<tr><td class="mono">${esc(e.symbol)}</td><td class="dim">${esc(e.opened_at||'-')}</td>
      <td class="dim">${esc(e.closed_at||'-')}</td><td class="n mono">${e.contracts}</td>
      <td class="n mono ${sign(e.realized_pnl_base)}">${money(e.realized_pnl_base)}</td>
      <td class="dim">${esc(e.status)}</td></tr>`)+
    (ht.realized_base!=null&&ht.closed_episodes
      ? `<div style="margin-top:8px" class="dim">realised total
         <span class="mono ${sign(ht.realized_base)}">${money(ht.realized_base)}</span>
         · commission <span class="mono">${money(ht.commission_base)}</span>
         <span class="tag">already net</span></div>`:'')+
    `</section>`);

  P.push(`<section><h2>Open episodes <span class="tag">${ht.open_episodes||0}</span></h2>`+
    tbl([['symbol'],['opened'],['qty','n'],['cost basis','n'],['record']],(h&&h.open)||[],
      e=>`<tr><td class="mono">${esc(e.symbol)}</td>
      <td class="dim">${esc(e.opened_at||'pre-archive')}</td>
      <td class="n mono ${sign(e.net_qty)}">${e.net_qty}</td>
      <td class="n mono">${e.cost_basis!=null?money(e.cost_basis):'-'}</td>
      <td class="dim">${e.snapshot_only?'snapshot only':
        e.entry_outside_window?'partial':'complete'}</td></tr>`)+`</section>`);

  // One row per order, with its legs beneath. leg_count > 1 on a single order
  // IS a spread, so the nesting is the strategy view rather than decoration.
  P.push(`<section><h2>Orders <span class="tag">${s.orders.length} order(s)</span></h2>`+
    (s.orders.length?`<table><thead><tr><th>order / leg</th><th class="n">qty</th>
      <th class="n">price</th><th class="n">fills</th><th class="n">proceeds</th>
      <th class="n">comm</th></tr></thead><tbody>`+
      s.orders.map(o=>`<tr><td class="mono">${esc(o.underlyings||'?')}
        <span class="tag">${esc(o.ib_order_id)}</span>${o.leg_count>1
          ?` <span class="tag" style="color:var(--warn)">${o.leg_count} legs</span>`:''}</td>
        <td class="n dim">—</td><td class="n dim">—</td>
        <td class="n mono">${o.fills}</td>
        <td class="n mono">${money(o.proceeds)}</td>
        <td class="n mono ${sign(o.commission)}">${money(o.commission,4)}</td></tr>`+
        (o.legs||[]).map(l=>`<tr><td class="mono dim" style="padding-left:22px">
          ${esc(l.symbol)} <span class="tag">${esc(l.open_close||'')}${
          l.buy_sell?' '+esc(l.buy_sell):''}</span></td>
          <td class="n mono ${sign(l.quantity)}">${l.quantity}</td>
          <td class="n mono">${money(l.avg_price,4)}</td>
          <td class="n mono dim">${l.fills!=null?l.fills:'-'}</td>
          <td class="n dim">—</td><td class="n dim">—</td></tr>`).join('')
      ).join('')+`</tbody></table>`:'<div class="empty">none</div>')+`</section>`);

  P.push(`<section class="wide"><h2>Archive</h2>`+
    tbl([['file'],['period'],['bytes','n'],['ingested'],['assets']],s.statements,
      t=>`<tr><td class="mono">${esc(t.file)}</td>
      <td class="dim">${esc(t.from_date||'?')} → ${esc(t.to_date||'?')}</td>
      <td class="n mono">${(t.bytes||0).toLocaleString()}</td>
      <td class="${t.ingested?'pos':'dim'}">${t.ingested?'yes':'no'}</td>
      <td class="dim">${esc(t.asset_filter||'-')}</td></tr>`)+`</section>`);

  $('#main').innerHTML=P.join('');
}

function note(text,kind){
  const m=$('#msg');m.textContent=text;m.className='show';
  m.style.background=kind==='bad'?'#3a2226':kind==='warn'?'#3a3122':'#1e3329';
  m.style.color=kind==='bad'?'#f0a0a0':kind==='warn'?'#e8cd85':'#a8e6cd';
}

async function load(){
  const r=await fetch('/api/state');
  if(!r.ok){note('Could not read state: '+r.status,'bad');return;}
  render(await r.json());
}

$('#sync').addEventListener('click',async()=>{
  if(!confirm('Fetch from IBKR now?\\n\\nThis spends one request against your '
    +'Flex request budget. IBKR locks out clients that ask too often.')) return;
  const b=$('#sync');b.disabled=true;b.textContent='Syncing…';
  try{
    const r=await fetch('/api/sync',{method:'POST'});
    const d=await r.json();
    if(d.kind==='cooldown') note(d.message,'warn');
    else if(!d.ok) note(d.message||'Sync failed','bad');
    else if(d.new_trades||d.new_cash)
      note(`Synced: ${d.new_trades} new trade(s), ${d.new_cash} new cash row(s).`,'ok');
    else note('Synced. Nothing new'+(d.reused_archive
      ?' — payload identical to the last statement, no duplicate archived.':'.'),'ok');
    if(d.warnings&&d.warnings.length) note(d.warnings.join(' · '),'warn');
  }catch(e){note('Sync failed: '+e,'bad');}
  await load();
});

load();
</script></body></html>
"""
