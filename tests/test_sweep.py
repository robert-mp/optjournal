"""The sweep's checks, each shown to fail on a page that violates it.

A check that has never failed is indistinguishable from `return PASS`. The
scratch version of the sweep reported "133/133 passed", which said only that
the script ran -- a regex with a typo'd class name, an anchor that no longer
matches the markup, a predicate whose precondition is never true all report
success forever. Two of these checks were written against markup I read once;
the only evidence they still bind is that a broken page makes them complain.

So every check gets a pair: a fragment that satisfies it, and one that
violates it in the specific way the real bug did. The violation must FAIL --
not error, not skip. Because the checks are pure functions of a rendered
page, both cost microseconds and need no browser, which is why this can live
in the suite while the sweep itself does not.

The fragments are deliberately minimal rather than realistic. A check that
only fires on a full page render is a check whose trigger condition nobody
understands.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from optjournal import browser, sweep
from optjournal.sweep import FAIL, PASS, SKIP, Page

#: The page's own JS, which ``--dump-dom`` embeds in every real render. It is
#: in the fixture because leaving it out made the first sweep run's failures
#: invisible here: every structural check was matching THIS source rather than
#: rendered output -- reading the Annual year-row template as a Positions
#: subtotal, and `${String(pos.side)...}` as a CSS class -- while the suite
#: stayed green because no fixture had a <script> tag to confuse it.
#:
#: Same unfaithful-fixture class as a test double that carries only the fields
#: the code under test happens to read. The literals below are the exact ones
#: that produced false positives.
_SCRIPT = (
    "<script>\n"
    "  const row=pos=>`<td class=\"side ${String(pos.side).toLowerCase()"
    "==='short'?'sell':'buy'}\">${esc(pos.side)}</td>`;\n"
    "  out.push(`<tr class=\"grp\"><td class=\"mono dim\" colspan=\"10\">${esc(y)}</td></tr>`);\n"
    "  const sel=S.calday===key?' selected':'';\n"
    "  cells.push(`<div class=\"day ${k}${dy?' clk':''}${sel}\">`);\n"
    "  if(v==null) return 'undefined';  /* NaN guard */\n"
    "</script>"
)

#: The header, which legitimately shows EVERY currency's glyph on its toggle.
#: A costs check that counted glyphs page-wide failed on a correct page because
#: of this, so it is in the fixture rather than left out.
_HEADER = (
    '<button class="tab on" data-tab="{tab}"></button>'
    '<button class="tab" data-tab="calendar"></button>'
    '<button class="tab" data-tab="trades"></button>'
    '<button class="tab" data-tab="positions"></button>'
    '<button class="tab" data-tab="costs"></button>'
    '<div class="hdr-icons">'
    '<div class="ccytog"><button>€EUR</button><button class="on">$USD</button></div>'
    "</div>"
)


def page(tab="dashboard", body="", ccy=None, kind=None, calday=None, replay=None,
         payload=None, console=()):
    """A page as the browser really delivers it: markup, header and script.

    Built through the same three views `sweep_journal` uses, so a check that
    reads the wrong one fails here exactly as it would on a live render.
    """
    dom = ("<html><body>" + _HEADER.format(tab=tab) + body
           + _SCRIPT + "</body></html>")
    return Page(
        tab=tab, ccy=ccy, kind=kind, calday=calday, replay=replay,
        console=tuple(console), dom=dom,
        markup=browser.markup(dom), text=browser.rendered_text(dom),
        payload=payload or {"stats": {}},
    )


def _costs(pill, commission, per_unit):
    """The journal cost block, plus an FX table that must be out of scope.

    The FX section is included on purpose: it lists conversion pairs and so
    legitimately shows other currencies. A check that read the whole page
    counted those as a contradiction and failed a correct render.
    """
    return (
        f'<div class="pills"><span class="pill">OPT <b>{pill}</b></span></div>'
        '<h2>OPT — attributable to this journal</h2>'
        f'<div class="stats"><div class="card">OPT commission {commission}'
        f' as charged</div><div class="card">Per contract {per_unit}</div></div>'
        '<div class="card"><h2>FX conversions</h2><table><tbody>'
        '<tr><td>EUR.SEK</td><td>kr1,200</td><td>€1.73</td></tr>'
        '</tbody></table></div>'
    )


def _bare(dom, tab, payload=None):
    """A page from an explicit DOM, for checks about the frame itself."""
    return Page(
        tab=tab, ccy=None, kind=None, calday=None, replay=None, console=(), dom=dom,
        markup=browser.markup(dom), text=browser.rendered_text(dom),
        payload=payload or {"stats": {}},
    )


# ---------------------------------------------------------------------------
# Each pair: (check, a page that satisfies it, a page that violates it)
# ---------------------------------------------------------------------------

_CALENDAR_OK = (
    '<div class="day g"></div><div class="day r"></div><div class="day"></div>'
    '<div class="pills"><span class="pill">Green days <b>1</b></span>'
    '<span class="pill">Red days <b>1</b></span></div>'
)
#: The bug this reproduces: pills read the payload's period-wide totals while
#: the grid showed one month, so under "All time" they claimed days the grid
#: could not account for.
_CALENDAR_LIES = (
    '<div class="day g"></div><div class="day"></div>'
    '<div class="pills"><span class="pill">Green days <b>7</b></span>'
    '<span class="pill">Red days <b>2</b></span></div>'
)

#: The check derives the expected spans from the header row, so the fixture has
#: to carry one. Nine columns with `value` fifth means 4 before and 4 after.
_POS_HEAD = (
    "<tr><th>contract</th><th>side</th><th>qty</th><th>mark</th>"
    "<th>value</th><th>cost basis</th><th>unrealised</th><th>price ccy</th>"
    "<th>record</th></tr>"
)
_POS_OK = (
    _POS_HEAD
    + '<tr class="grp"><td colspan="4" class="dim">x</td><td></td>'
      '<td colspan="4"></td></tr>'
)
#: The original: a colspan=8 label put the subtotal in the last column, under
#: `record` -- four columns left of the value it totalled.
_POS_WRONG_COLUMN = (
    _POS_HEAD + '<tr class="grp"><td colspan="8" class="dim">x</td><td></td></tr>'
)

_REPLAY_PAYLOAD = {
    "stats": {},
    "replays": {
        "lc:C1@2026-07-24": {
            "key": "lc:C1@2026-07-24", "underlying": "TSLA", "label": "Short put",
            "bar_size": "1h", "points": [[100, 370.0], [200, 340.0], [300, 323.0]],
            "strikes": [{"strike": 270.0, "put_call": "P", "side": "short",
                         "frm": 150, "to": 280}],
            "opened_at": "2026-07-24", "closed_at": "2026-08-03",
            "opened_ts": 150, "closed_ts": 280, "fills": [150, 280],
            "band": [[100, 320.0, 420.0], [200, 290.0, 390.0], [300, 275.0, 371.0]],
            "marks": [[100, 100.0, 0.4], [200, 250.0, 0.3], [300, 792.0, 0.0]],
            "events": [
                {"ts": 150, "at": "2026-07-24 10:35", "label": "Short put",
                 "kind": "open", "legs": [], "cash": None, "commission": None,
                 "realized": None, "delta_before": None, "delta_after": 0.4},
                {"ts": 280, "at": "2026-08-03 09:55", "label": "Short put close",
                 "kind": "close", "legs": [], "cash": None, "commission": None,
                 "realized": None, "delta_before": 0.3, "delta_after": 0.0},
            ],
        },
    },
}
_REPLAY_CTL = (
    '<input id="rscrub" type="range" min="0" max="2" value="2">'
    '<select id="rspeed"><option value="240">1x</option></select>'
)
#: A whole panel as the page really emits one. Fuller than it looks necessary:
#: each element here is one the check asserts on, and every one of them was added
#: after a defect that shipped because nothing looked for it.
_REPLAY_OK = (
    '<div class="rkey">put strike</div>'
    '<div class="replay"><svg>'
    '<defs><clipPath id="rclip"><rect id="rclipr"/></clipPath></defs>'
    '<polygon class="emband" points="1,2 3,4"/>'
    '<line class="edge in" x1="3" y1="0" x2="3" y2="9"/>'
    '<line class="ax" x1="52" y1="12" x2="52" y2="240"/>'
    '<line class="ax" x1="52" y1="240" x2="874" y2="240"/>'
    '<line class="sk put" x1="100" y1="5" x2="300" y2="5"/>'
    '<text class="sklab put">270P</text>'
    '<g clip-path="url(#rclip)">'
    '<polyline class="dline" points="1,2 3,4"/>'
    '<polyline class="pxline" points="1,2 3,4 5,6"/></g>'
    '</svg>' + _REPLAY_CTL
    + '<div class="evs">'
      '<div class="evc on" data-rts="150" data-rseek="0">opened</div>'
      '<div class="evc on now" data-rts="280" data-rseek="1">closed</div>'
      '</div></div>'
)
#: The failure this excludes: an axis frame with no line reads as "this trade
#: did nothing", which is a claim about the trade rather than about the data.
_REPLAY_NO_LINE = _REPLAY_OK.replace(
    '<polyline class="pxline" points="1,2 3,4 5,6"/>', '')
#: The cards render but none is revealed -- the open sync did not run, so the
#: strip sits blank until the reader happens to touch the scrubber.
_REPLAY_CARDS_HIDDEN = _REPLAY_OK.replace('class="evc on now"', 'class="evc"').replace(
    'class="evc on"', 'class="evc"')
#: A card seeking PAST its own event: clicking it would jump the chart to a frame
#: where the card it was clicked from is not yet revealed.
_REPLAY_CARD_MISSEEKS = _REPLAY_OK.replace(
    '<div class="evc on" data-rts="150" data-rseek="0">',
    '<div class="evc on" data-rts="150" data-rseek="2">')


_SIDE_OK = '<td class="side buy">Long</td><td class="side sell">Short</td>'
#: `side` alone has no hue; the column renders as unstyled text.
_SIDE_UNSTYLABLE = '<td class="side">Long</td>'

_CLOSED_OK = (
    '<div class="card">CLOSED <span>STC</span> <span>Closed</span></div>'
)
#: A closed lifecycle whose closing event repeats the full strategy name reads
#: as two unrelated trades on one card.
_CLOSED_UNCAPTIONED = (
    '<div class="card">CLOSED <span>STC</span> <span>Short put close</span></div>'
)

_DRILL_PAYLOAD = {"stats": {"days": [{"day": "2026-08-04", "trades": 2}]}}
_DRILL_OK = (
    '<div class="day g clk selected" data-calday="2026-08-04"></div>'
    '<h3>2026-08-04 — 2 fill(s)</h3><div class="leg ctx"></div><div class="leg ctx"></div>'
)
#: The panel rendered but no cell shows as chosen, so the grid gives no clue
#: which day is open below it.
_DRILL_NO_SELECTION = '<h3>2026-08-04 — 2 fill(s)</h3><div class="leg ctx"></div>'
#: The bug fixed that morning: drill-down legs rendered without `.ctx`, losing
#: the contract and strategy columns that are the panel's entire purpose.
_DRILL_PLAIN_LEGS = (
    '<div class="day g clk selected" data-calday="2026-08-04"></div>'
    '<h3>2026-08-04 — 2 fill(s)</h3><div class="leg"></div><div class="leg ctx"></div>'
)

_MONEY_OK = {"stats": {"commissions": {"base": 1.0, "native": 1.1, "ccy": "USD"},
                       "friction_base": 2.0}}
#: The state `Money.__post_init__` refuses in Python, asserted here because the
#: payload is where a hand-built dict could reintroduce it.
_MONEY_HALF = {"stats": {"commissions": {"base": 1.0, "native": 1.1, "ccy": None}}}
#: friction contains the AutoFX estimate, which was never billed in any
#: currency -- Money-shaping it invents precision rather than recovering it.
_MONEY_INVENTED = {"stats": {"friction": {"base": 2.0, "native": 2.1, "ccy": "USD"}}}

CASES: list[tuple[str, sweep.Check, Page, Page]] = [
    ("page renders",
     sweep.check_page_rendered,
     page(),
     _bare("<html><body>" + _SCRIPT + "</body></html>", tab="dashboard")),
    ("no junk bindings",
     sweep.check_no_junk_bindings,
     page(body="<div>$4.31</div>"),
     page(body="<div>undefined</div>")),
    ("no object stringified",
     sweep.check_no_junk_bindings,
     page(body="<div>$4.31</div>"),
     page(body="<div>[object Object]</div>")),
    ("active tab matches hash",
     sweep.check_selected_tab_matches_hash,
     page(tab="dashboard"),
     _bare("<html>" + _HEADER.format(tab="dashboard") + _SCRIPT + "</html>",
           tab="costs")),
    ("header icons grouped",
     sweep.check_header_icons_grouped,
     page(),
     _bare('<html><button class="tab on" data-tab="dashboard"></button>'
           '<button class="tab" data-tab="a"></button>'
           '<button class="tab" data-tab="b"></button>'
           '<button class="tab" data-tab="c"></button>'
           '<button class="tab" data-tab="d"></button>' + _SCRIPT + "</html>",
           tab="dashboard")),
    ("money keys whole",
     sweep.check_money_keys_are_shaped,
     page(payload=_MONEY_OK),
     page(payload=_MONEY_HALF)),
    ("friction stays flat",
     sweep.check_money_keys_are_shaped,
     page(payload=_MONEY_OK),
     page(payload=_MONEY_INVENTED)),
    ("costs block shares a basis",
     sweep.check_costs_block_shares_one_basis,
     page(tab="costs", body=_costs("$6.97", "$6.97", "$0.6965")),
     # The contradiction the model exists to prevent: a restated pill beside an
     # as-charged card, in one sentence.
     page(tab="costs", body=_costs("€6.07", "$6.97", "$0.6965"))),
    ("calendar pills match grid",
     sweep.check_calendar_pills_match_grid,
     page(tab="calendar", body=_CALENDAR_OK),
     page(tab="calendar", body=_CALENDAR_LIES)),
    ("positions subtotal column",
     sweep.check_positions_subtotal_column,
     page(tab="positions", body=_POS_OK),
     page(tab="positions", body=_POS_WRONG_COLUMN)),
    ("side is colourable",
     sweep.check_positions_side_is_colourable,
     page(tab="positions", body=_SIDE_OK),
     page(tab="positions", body=_SIDE_UNSTYLABLE)),
    ("no blank contract cells",
     sweep.check_no_blank_contract_cells,
     page(tab="trades", body='<span class="mono">GOOG <b>420C</b></span>'),
     page(tab="trades", body='<span class="mono"><b>420C</b></span>')),
    ("no ? headings",
     sweep.check_no_blank_contract_cells,
     page(tab="trades", body="<h3>SPY Single leg</h3>"),
     page(tab="trades", body="<h3>?</h3>")),
    ("closing events captioned",
     sweep.check_closing_events_are_captioned,
     page(tab="trades", body=_CLOSED_OK),
     page(tab="trades", body=_CLOSED_UNCAPTIONED)),
    ("drill-down selects one cell",
     sweep.check_drilldown_renders_from_url,
     page(tab="calendar", calday="2026-08-04", body=_DRILL_OK, payload=_DRILL_PAYLOAD),
     page(tab="calendar", calday="2026-08-04", body=_DRILL_NO_SELECTION,
          payload=_DRILL_PAYLOAD)),
    ("no uncaught javascript",
     sweep.check_no_uncaught_javascript,
     page(tab="trades", body="<div>fine</div>"),
     page(tab="trades", body="<div>blank</div>",
          console=("Uncaught ReferenceError: geo is not defined",))),
    ("replay draws its line and strikes",
     sweep.check_replay_renders_from_url,
     page(tab="trades", replay="lc:C1@2026-07-24", body=_REPLAY_OK,
          payload=_REPLAY_PAYLOAD),
     page(tab="trades", replay="lc:C1@2026-07-24", body=_REPLAY_NO_LINE,
          payload=_REPLAY_PAYLOAD)),
    ("replay reveals its annotation cards on open",
     sweep.check_replay_renders_from_url,
     page(tab="trades", replay="lc:C1@2026-07-24", body=_REPLAY_OK,
          payload=_REPLAY_PAYLOAD),
     page(tab="trades", replay="lc:C1@2026-07-24", body=_REPLAY_CARDS_HIDDEN,
          payload=_REPLAY_PAYLOAD)),
    ("an annotation card seeks to its own bar",
     sweep.check_replay_renders_from_url,
     page(tab="trades", replay="lc:C1@2026-07-24", body=_REPLAY_OK,
          payload=_REPLAY_PAYLOAD),
     page(tab="trades", replay="lc:C1@2026-07-24", body=_REPLAY_CARD_MISSEEKS,
          payload=_REPLAY_PAYLOAD)),
    ("drill-down legs carry ctx",
     sweep.check_drilldown_legs_have_context,
     page(tab="calendar", calday="2026-08-04", body=_DRILL_OK, payload=_DRILL_PAYLOAD),
     page(tab="calendar", calday="2026-08-04", body=_DRILL_PLAIN_LEGS,
          payload=_DRILL_PAYLOAD)),
]


@pytest.mark.parametrize("name,check,good,_bad", CASES, ids=[c[0] for c in CASES])
def test_check_passes_a_healthy_page(name, check, good, _bad):
    verdict = check(good)
    assert verdict.status == PASS, f"{name}: healthy page did not pass ({verdict.detail})"


@pytest.mark.parametrize("name,check,_good,bad", CASES, ids=[c[0] for c in CASES])
def test_check_fails_a_broken_page(name, check, _good, bad):
    """The point of the file: a violated invariant must FAIL, not skip.

    SKIP is called out separately because it is the silent failure mode -- a
    check whose precondition stopped matching the markup skips forever and
    looks like it is simply not applicable.
    """
    verdict = check(bad)
    assert verdict.status != SKIP, (
        f"{name}: check SKIPPED a page that violates it -- its precondition no"
        f" longer matches the markup ({verdict.detail})"
    )
    assert verdict.status == FAIL, f"{name}: check passed a broken page"
    assert verdict.detail, f"{name}: failed without saying why"


def test_every_check_has_a_negative_control():
    """No check may join the sweep without a broken page proving it can fail."""
    covered = {check.__name__ for _, check, _, _ in CASES}
    declared = {c.__name__ for c in sweep.CHECKS}
    missing = declared - covered
    assert not missing, f"checks with no negative control: {sorted(missing)}"


def test_drilldown_heals_a_day_that_has_no_fills():
    """A stale `calday` must fall back, not render a panel claiming fills.

    The failure mode this excludes is the worse one: an empty panel captioned
    with a date is indistinguishable from a day that genuinely had no activity.
    """
    healed = page(tab="calendar", calday="1999-01-01",
                  body="<div>pick a day</div>", payload=_DRILL_PAYLOAD)
    assert sweep.check_drilldown_renders_from_url(healed).status == PASS

    lying = page(tab="calendar", calday="1999-01-01",
                 body="<h3>1999-01-01 — 3 fill(s)</h3>", payload=_DRILL_PAYLOAD)
    assert sweep.check_drilldown_renders_from_url(lying).status == FAIL


def test_replay_heals_a_key_that_names_no_trade():
    """A stale replay key must fall back, not draw an empty frame.

    The trade-type filter changes which lifecycles exist, so a bookmarked key
    legitimately stops resolving -- and an axis with no line is the same class of
    lie as a drill-down panel captioned with a day that had no fills.
    """
    healed = page(tab="trades", replay="lc:nosuch@1999-01-01",
                  body="<div>cards</div>", payload=_REPLAY_PAYLOAD)
    assert sweep.check_replay_renders_from_url(healed).status == PASS

    lying = page(tab="trades", replay="lc:nosuch@1999-01-01",
                 body=_REPLAY_OK, payload=_REPLAY_PAYLOAD)
    assert sweep.check_replay_renders_from_url(lying).status == FAIL


def test_replay_requires_the_band_it_was_given():
    """The band is the only MODELLED series on the panel, and the only one with
    no broker figure anywhere else in the journal to contradict it. If it
    silently stops rendering, nothing but this notices.
    """
    no_band = _REPLAY_OK.replace('<polygon class="emband" points="1,2 3,4"/>', '')
    verdict = sweep.check_replay_renders_from_url(
        page(tab="trades", replay="lc:C1@2026-07-24", body=no_band,
             payload=_REPLAY_PAYLOAD))
    assert verdict.status == FAIL, "a dropped band went unnoticed"


def test_replay_requires_the_entry_to_be_marked():
    """Without it the session of context either side reads as part of the trade,
    which on a ten-bar trade is most of the picture.
    """
    no_edge = _REPLAY_OK.replace('<line class="edge in" x1="3" y1="0" x2="3" y2="9"/>', '')
    verdict = sweep.check_replay_renders_from_url(
        page(tab="trades", replay="lc:C1@2026-07-24", body=no_edge,
             payload=_REPLAY_PAYLOAD))
    assert verdict.status == FAIL


def test_replay_scrubber_must_span_every_bar():
    """A range that stops short leaves the tail of the series unreachable, which
    looks like a chart that ends early rather than a control that does.
    """
    short = _REPLAY_OK.replace('max="2"', 'max="1"')
    verdict = sweep.check_replay_renders_from_url(
        page(tab="trades", replay="lc:C1@2026-07-24", body=short,
             payload=_REPLAY_PAYLOAD))
    assert verdict.status == FAIL


def test_no_check_reads_the_raw_dom():
    """A check must read `markup` or `text`, never `dom`.

    The defect this forbids cost the first sweep run five false failures: every
    structural check matched page.html's own <script> source instead of its
    output, because ``--dump-dom`` embeds it. A comment saying "don't read dom"
    is a comment; parsing for it is a guard.

    `Page.dom` stays available deliberately -- it is the artifact, and worth
    having when a failure needs explaining -- so nothing but this test stops a
    check reaching for it.
    """
    source = Path(sweep.__file__).read_text()
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("check_"):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Attribute) and sub.attr == "dom"
                    and isinstance(sub.value, ast.Name)):
                offenders.append(f"{node.name} reads {sub.value.id}.dom")
    assert not offenders, (
        "these checks read the raw dump, which contains the page's own JS: "
        f"{offenders}"
    )


def test_the_matrix_covers_every_tab():
    """Every tab the UI offers gets rendered.

    A tab absent from the matrix is a tab the sweep cannot report on, which is
    how the drill-down went unchecked until `calday` joined the hash.
    """
    covered = {tab for tab, _, _ in sweep.page_coords("USD")}
    assert covered == set(sweep.TABS), f"tabs not swept: {set(sweep.TABS) - covered}"


def test_the_matrix_exercises_both_axes():
    """The currency toggle and the asset switch both appear in the matrix."""
    coords = sweep.page_coords("USD")
    assert any(c == "USD" for _, c, _ in coords), "currency toggle never exercised"
    assert any(k == "equities" for _, _, k in coords), "asset switch never exercised"
    assert all(c is None for _, c, _ in sweep.page_coords(None)), (
        "a journal with no quote currency must not be swept at one"
    )
