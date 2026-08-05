"""Rendering every page the UI can show, and asserting what each one must hold.

The unit suite proves the payload is right and the page's source is consistent
with it. Neither proves the page a reader actually gets is right: a template
error blanks a panel, a nullable field renders as literal "undefined", a pill
disagrees with the grid beneath it. Those need the page executed, and executed
across the whole matrix rather than once, because every one of a day's
rendering bugs lived on a tab the default view does not show.

Two properties make this worth keeping in the repo rather than as a scratch
script:

**Checks are pure functions of a rendered page.** A `Check` takes a `Page` --
DOM string, stripped text, the payload the page fetched, and the coordinates it
was rendered at -- and returns a verdict. No check touches a browser, a server
or the network. So `tests/test_sweep.py` can feed each one a hand-built broken
fragment and assert it FAILS, which is the only evidence that a green sweep
means anything. A suite of checks that has never been shown to fail is a suite
of checks that cannot fail.

**The sweep starts its own servers.** The scratch version required two journals
already serving on fixed ports, which made it unrunnable from a clean checkout
and silently测 stale code when a server predated the last edit. Here each
journal is served on an ephemeral port through the same `ServeConfig` and
handler `serve()` uses in production, so the sweep always exercises the code
that is on disk now -- a server started before the last edit cannot make a
stale page look green.

A check may also return SKIP, for a property whose precondition the data does
not reach -- an empty-month grid, or a native figure on a mixed-currency scope.
Those are insurance for states these journals do not currently produce, and the
report counts them separately from passes so a skip can never be mistaken for
evidence.
"""

from __future__ import annotations

import http.server
import json
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from optjournal import browser, web
from optjournal.ingest import DEFAULT_ASSET_FILTER

PASS = "pass"
FAIL = "fail"
SKIP = "skip"


@dataclass(frozen=True, slots=True)
class Page:
    """One rendered page: what the browser produced, and what it was given.

    `payload` is the same JSON the page itself fetched, so a check can compare
    the render against its own source of truth rather than a hardcoded oracle
    that drifts when the data changes.
    """

    tab: str
    ccy: str | None
    kind: str | None
    calday: str | None
    replay: str | None
    #: The raw dump, INCLUDING the page's own <script> source. NO check reads
    #: this, deliberately: a pattern for a rendered cell matches the template
    #: literal that generates it just as well, and every structural check in
    #: the first run did exactly that. Retained because it is the artifact --
    #: worth having when a failure needs explaining.
    dom: str
    #: Script and style bodies removed, tags intact -- what a structural check
    #: reads.
    markup: str
    #: Tags stripped too: only what a reader sees.
    text: str
    payload: dict[str, Any]
    #: Uncaught JS errors the browser reported while rendering this page. Last,
    #: because it is the only field with a default and a dataclass requires that.
    console: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        bits = [self.tab]
        if self.ccy:
            bits.append(f"ccy={self.ccy}")
        if self.kind:
            bits.append(f"type={self.kind}")
        if self.calday:
            bits.append(f"calday={self.calday}")
        if self.replay:
            bits.append(f"replay={self.replay}")
        return " ".join(bits)

    def url_hash(self) -> str:
        parts = [f"tab={self.tab}"]
        if self.ccy:
            parts.append(f"ccy={self.ccy}")
        if self.kind:
            parts.append(f"type={self.kind}")
        if self.calday:
            parts.append(f"calday={self.calday}")
        if self.replay:
            parts.append(f"replay={self.replay}")
        return "#" + "&".join(parts)


@dataclass(frozen=True, slots=True)
class Verdict:
    status: str
    detail: str = ""


#: A check: given a rendered page, PASS, FAIL with a reason, or SKIP with the
#: precondition that was absent.
Check = Callable[[Page], Verdict]


def ok() -> Verdict:
    return Verdict(PASS)


def bad(detail: str) -> Verdict:
    return Verdict(FAIL, detail)


def skip(why: str) -> Verdict:
    return Verdict(SKIP, why)


# ---------------------------------------------------------------------------
# Checks that apply to every page
# ---------------------------------------------------------------------------


def check_page_rendered(p: Page) -> Verdict:
    """The tab bar exists -- its absence means the JS threw before drawing."""
    n = p.markup.count('data-tab="')
    if n < len(("dashboard", "calendar", "trades", "positions", "costs")):
        return bad(f"tab bar missing (found {n} tabs) -- page JS crashed on load")
    return ok()


def check_no_junk_bindings(p: Page) -> Verdict:
    """No binding rendered as literal junk.

    A failed binding inside a template literal does not throw -- it renders the
    word. None of these belong on the page as prose, and scanning the stripped
    text rather than the DOM keeps the page's own JS source out of scope.
    """
    hit = re.search(r"\b(undefined|NaN)\b", p.text)
    if hit:
        where = p.text[max(0, hit.start() - 60):hit.end() + 60].strip()
        return bad(f"binding rendered as {hit.group(1)!r}: ...{where}...")
    if "[object Object]" in p.text:
        return bad("an object reached the page as text -- a Money used where a number was meant")
    return ok()


def check_selected_tab_matches_hash(p: Page) -> Verdict:
    """The tab the URL asked for is the one marked active.

    Hash routing is the only navigation this page has; a mismatch means a deep
    link silently lands somewhere else.
    """
    on = re.findall(r'class="tab on"\s+data-tab="([^"]+)"', p.markup)
    if not on:
        return bad("no tab marked active")
    if len(on) > 1:
        return bad(f"{len(on)} tabs marked active at once: {on}")
    if on[0] != p.tab:
        return bad(f"URL asked for tab={p.tab} but {on[0]!r} is active")
    return ok()


def check_header_icons_grouped(p: Page) -> Verdict:
    """Both header icons sit in one group.

    They are one control group; when they were direct flex children the
    currency toggle stretched to fill the column and grew dead space inside
    it. The grouping is what holds the layout, so it is asserted on every
    page rather than eyeballed on one.
    """
    if 'class="hdr-icons"' not in p.markup:
        return bad("the .hdr-icons group is gone -- the toggle will stretch")
    return ok()


# ---------------------------------------------------------------------------
# Money: the invariant the whole model exists to hold
# ---------------------------------------------------------------------------

_CURRENCY_GLYPH = {"EUR": "€", "USD": "$", "GBP": "£", "SEK": "kr", "KRW": "₩"}


def check_costs_block_shares_one_basis(p: Page) -> Verdict:
    """Pill, card and per-unit on the Costs tab agree on a currency.

    They sit in one sentence, so "as charged · $6.97" beside a €6.07 pill is a
    contradiction rather than a rounding difference. This is the display half of
    the Money model: one rule applied to every figure in a block, not per
    figure.

    Scoped to the journal cost block, NOT the whole page. Two things outside it
    legitimately carry other currencies: the header's currency toggle labels
    every option with its own glyph (`€EUR $USD`), and the FX table below lists
    conversion pairs. A page-wide glyph count reported both as contradictions --
    it failed on a correct page, which is the failure mode that gets a check
    deleted rather than trusted.
    """
    if p.tab != "costs":
        return skip("not the costs tab")
    start = p.markup.find('class="pills"')
    if start < 0:
        return skip("no cost pill rendered")
    end = p.markup.find("FX conversions", start)
    block = p.markup[start:] if end < 0 else p.markup[start:end]
    glyphs = {g for g in _CURRENCY_GLYPH.values() if g in block}
    if len(glyphs) > 1:
        return bad(f"the costs block mixes currency symbols {sorted(glyphs)} in one sentence")
    if not glyphs:
        return skip("no monetary figure rendered in the costs block")
    return ok()


def check_money_keys_are_shaped(p: Page) -> Verdict:
    """Every Money in the payload is whole, and the flat figures stay flat.

    Two directions, because the model's meaning is carried by the shape itself:
    a nested object says "an as-charged figure could exist here", a flat
    `_base` float says it cannot. `friction` includes the estimated AutoFX
    markup, which IBKR never billed in any currency, so it must NOT be Money
    -- if it ever becomes one, someone has invented precision.
    """
    stats = p.payload.get("stats") or {}
    for key, value in stats.items():
        if (isinstance(value, dict) and "base" in value
                and (value.get("native") is None) != (value.get("ccy") is None)):
            return bad(f"stats.{key} is half-assigned: {value!r}")
    for forbidden in ("friction", "account_friction"):
        value = stats.get(forbidden)
        if isinstance(value, dict):
            return bad(
                f"stats.{forbidden} became Money-shaped -- it contains the AutoFX "
                f"estimate, which was never billed as a line item"
            )
    return ok()


# ---------------------------------------------------------------------------
# Per-tab checks
# ---------------------------------------------------------------------------


def check_calendar_pills_match_grid(p: Page) -> Verdict:
    """The green/red day counts equal the cells actually coloured.

    Derived independently from the DOM, so a blank grid claiming seven green
    days fails. This is the tab's own invariant -- its numbers move only with a
    control it displays -- made checkable.
    """
    if p.tab != "calendar":
        return skip("not the calendar tab")
    cells = re.findall(r'<div class="day ([^"]*)"', p.markup)
    if not cells:
        return bad("the calendar grid never rendered")
    green = sum(1 for c in cells if re.match(r"^g\b", c.strip()))
    red = sum(1 for c in cells if re.match(r"^r\b", c.strip()))
    claimed = re.search(
        r"Green days\s*<b>(\d+)</b>.*?Red days\s*<b>(\d+)</b>", p.markup, re.S
    )
    if not claimed:
        return bad("the green/red day pills never rendered")
    want_g, want_r = int(claimed.group(1)), int(claimed.group(2))
    if (want_g, want_r) != (green, red):
        return bad(
            f"pills claim {want_g} green / {want_r} red, grid has {green} / {red}"
        )
    if not any(c.strip() for c in cells):
        return skip("no coloured days this month -- counts are trivially 0")
    return ok()


def check_positions_subtotal_column(p: Page) -> Verdict:
    """The positions subtotal lands under `value`, not four columns left of it.

    A subtotal in the wrong column is not obviously wrong to look at -- it is a
    plausible number under the wrong heading, which is worse than a blank.

    The expected spans are DERIVED from the rendered header row rather than
    hardcoded. Hardcoding them meant adding a column (the `trend` miniature)
    failed this check while the alignment was in fact still correct, which
    trains a reader to bump the constant instead of reading the assertion.
    """
    if p.tab != "positions":
        return skip("not the positions tab")
    rows = re.findall(r'<tr class="grp">(.*?)</tr>', p.markup, re.S)
    if not rows:
        return skip("no position groups on this page")
    headers = re.findall(r"<th[^>]*>(.*?)</th>", p.markup)
    if "value" not in headers:
        return bad(f"no `value` column in the positions header: {headers}")
    before = headers.index("value")
    after = len(headers) - before - 1
    expected = [str(before), str(after)]
    for row in rows:
        spans = re.findall(r'colspan="(\d+)"', row)
        if spans != expected:
            return bad(
                f"subtotal row has colspans {spans}, expected {expected} "
                f"for a {len(headers)}-column table with `value` at {before}"
            )
    return ok()


def check_positions_side_is_colourable(p: Page) -> Verdict:
    """The side cell carries a class CSS actually matches.

    `side` alone renders the word with no hue; the buy/sell modifier is what
    makes the column readable at a glance.

    The pattern must NOT require whitespace after `side`. Written as
    `"side ([^"]*)"` it does not match a bare `class="side"` at all, so the
    check reported "no positions held" and skipped -- on the one input it
    exists to reject. The negative control caught that; without it the check
    would have skipped forever and read as green.
    """
    if p.tab != "positions":
        return skip("not the positions tab")
    cells = re.findall(r'<td class="side\s*([^"]*)"', p.markup)
    if not cells:
        return skip("no positions held")
    wrong = [c or "(no modifier)" for c in cells if c.strip() not in ("buy", "sell")]
    if wrong:
        return bad(f"side cells carry unstylable classes {wrong}")
    return ok()


def check_no_blank_contract_cells(p: Page) -> Verdict:
    """No contract renders as an empty or `?` heading.

    The demo used to emit `""` for a stock row's underlying where IBKR sends
    the ticker, so lifecycles were captioned `?`. A blank identifier is a
    rendering bug that looks like missing data.
    """
    if p.tab not in ("trades", "positions", "odte"):
        return skip("no contract cells on this tab")
    if re.search(r">\s*\?\s*<", p.markup):
        return bad("a contract or lifecycle rendered as '?'")
    if re.search(r'<span class="mono">\s*<b>', p.markup):
        return bad("a contract cell rendered with no underlying symbol")
    return ok()


def check_closing_events_are_captioned(p: Page) -> Verdict:
    """A closing event on a closed lifecycle reads "Closed".

    The caption is contextual: an event whose shape matches its card's label is
    an open or a close, and spelling the full strategy name twice on one card
    reads as two separate trades.
    """
    if p.tab not in ("trades", "odte"):
        return skip("no lifecycle cards on this tab")
    cards = re.findall(r'<div class="card"[^>]*>(.*?)(?=<div class="card"|$)', p.markup, re.S)
    seen = 0
    for card in cards:
        if "CLOSED" not in card:
            continue
        if not re.search(r"\b(STC|BTC)\b", card):
            continue
        seen += 1
        if "Closed" not in card:
            return bad("a closed lifecycle's closing event is not captioned 'Closed'")
    if not seen:
        return skip("no closed lifecycle with a closing fill on this page")
    return ok()


def check_no_uncaught_javascript(p: Page) -> Verdict:
    """The page rendered without the browser refusing to run it.

    Added after a single undefined identifier blanked the whole Trades view while
    470 Python tests stayed green: the payload contract reads page.html's source
    and the unit suite reads pure functions, and neither knows the browser threw.
    Every other check here reads what DID render, so a view that rendered nothing
    passes them all by having nothing to disagree with.
    """
    if p.console:
        return bad("; ".join(p.console[:2]))
    return ok()


def check_replay_renders_from_url(p: Page) -> Verdict:
    """A replay panel opens from the URL alone, with a price line and its strikes.

    The panel is the only place the price bars are rendered, so a broken chart is
    invisible to every other check: they read money and rows, and an empty <svg>
    would still leave the card looking untouched.

    A stale key must heal away rather than draw an empty frame -- an axis with no
    line reads as "this trade did nothing", which is a claim about the trade
    rather than about the data.
    """
    if not p.replay:
        return skip("not a replay page")
    replay = (p.payload.get("replays") or {}).get(p.replay)
    if replay is None:
        if 'class="replay"' in p.markup:
            return bad(f"stale replay={p.replay} rendered a panel anyway")
        return ok()
    panels = p.markup.count('class="replay"')
    if panels != 1:
        return bad(f"{panels} replay panels rendered for one key, expected exactly 1")
    if not replay["points"]:
        # Honest empty state, not a blank chart pretending to be one.
        if "no price bars stored" not in p.text:
            return bad("no bars stored, but the panel does not say so")
        return ok()
    if 'class="pxline"' not in p.markup:
        return bad("panel rendered but the underlying price line is absent")
    drawn = re.findall(r'class="pxline" points="([^"]*)"', p.markup)[0].split()
    if len(drawn) != len(replay["points"]):
        return bad(f"{len(drawn)} points drawn, payload holds {len(replay['points'])}")
    # x is ordinal, so every bar occupies the same width. A linear time axis
    # would space them by elapsed seconds, which on this journal's own data put
    # 80.9% of the width on hours the market was shut and drew every overnight
    # gap as a long diagonal. Uneven steps here mean that regressed.
    steps = [round(float(b.split(",")[0]) - float(a.split(",")[0]), 1)
             for a, b in zip(drawn, drawn[1:], strict=False)]  # pairwise: b is 1 shorter
    # Bounded by SPREAD, not by distinct count: coordinates are rounded to one
    # decimal, so an even axis still yields two neighbouring values (12.7 and
    # 12.8). A count-based bound of two therefore admitted exactly the shape it
    # was meant to catch -- one short step and one long overnight one.
    if steps and max(steps) - min(steps) > 0.5:
        return bad(f"x steps span {min(steps)}..{max(steps)} -- the axis is "
                   "spacing bars by elapsed time, not by position")
    labels = re.findall(r'class="sklab[^"]*"[^>]*>([^<]+)<', p.markup)
    if len(labels) != len(replay["strikes"]):
        return bad(f"{len(labels)} strike labels, payload holds {len(replay['strikes'])}")
    # The label carries the strike and the right. Side is no longer written out:
    # it is the dash, and the legend says so -- which is what frees the label to
    # sit on the segment instead of crowding the right margin the delta axis uses.
    for strike_row in replay["strikes"]:
        want = str(strike_row["put_call"]).upper()
        if not any(lab.strip().upper().endswith(want) for lab in labels):
            return bad(f"no strike label ends in '{want}': {labels}")
        css = f'class="sk {"call" if want == "C" else "put"}'
        if css not in p.markup:
            return bad(f"a {want} strike is not hue-coded: expected {css!r}")
        dashed = 'class="sk put long"' in p.markup or 'class="sk call long"' in p.markup
        if strike_row["side"] == "long" and not dashed:
            return bad("a bought strike is not dashed, so it reads as sold")
    # The band is the only modelled series on the panel and the only one with no
    # broker figure to contradict it, so its absence is invisible everywhere else.
    if replay.get("band") and 'class="emband"' not in p.markup:
        return bad(f"payload holds {len(replay['band'])} band rows, none drawn")
    # An entry inside the window must be marked, or the session of context either
    # side reads as part of the trade.
    opened = replay.get("opened_ts")
    inside = opened and replay["points"][0][0] <= opened <= replay["points"][-1][0]
    if inside and 'class="edge in"' not in p.markup:
        return bad("entry falls inside the window but is not marked on the line")
    # Playback: a scrubber whose range does not span the bars would leave part of
    # the series unreachable.
    scrub = re.search(r'id="rscrub"[^>]*max="(\d+)"', p.markup)
    if not scrub:
        return bad("no scrubber rendered")
    if int(scrub.group(1)) != len(replay["points"]) - 1:
        return bad(f"scrubber max={scrub.group(1)} for "
                   f"{len(replay['points'])} bars -- part of the series is unreachable")
    if 'id="rclip"' not in p.markup:
        return bad("no reveal clip, so scrubbing cannot hide the future")
    # The clip has to be a real SVG group. A rename once turned <g> into <geo>,
    # an unknown element, so the price line inside it stopped rendering entirely
    # -- while every markup check still passed, because the STRING was present.
    if 'clip-path="url(#rclip)"' not in p.markup:
        return bad("the reveal group is not applying the clip path")
    if not re.search(r"<g\b[^>]*clip-path", p.markup):
        return bad("the clipped layer is not an SVG <g>, so nothing inside it draws")
    # A segment's ends must reflect the holding period: it may only touch an edge
    # of the plot where the payload says the position extended past it. Asserted
    # edge by edge rather than by a width threshold -- the first version used one,
    # and failed a correct chart whose entry was four bars into a 134-bar window,
    # so the segment legitimately covered 97% of the width.
    spans = re.findall(r'class="sk [^"]*" x1="([\d.]+)"[^>]*x2="([\d.]+)"', p.markup)
    if replay["strikes"] and not spans:
        return bad("no strike segments rendered")
    first, last = replay["points"][0][0], replay["points"][-1][0]
    # The plot edges come from the AXIS lines. Deriving them from the segments
    # themselves was circular: with one segment, its own end trivially equalled
    # "the edge" and the assertion could never fail.
    axes = re.findall(r'class="ax" x1="([\d.]+)"[^>]*x2="([\d.]+)"', p.markup)
    if not axes:
        return bad("no axis lines, so the plot edges cannot be established")
    left = min(float(a) for a, _ in axes)
    right = max(float(b) for _, b in axes)
    for strike_row, (raw_x1, raw_x2) in zip(replay["strikes"], spans, strict=False):
        started = strike_row.get("frm")
        ended = strike_row.get("to")
        if started and started > first and float(raw_x1) <= left:
            return bad(
                f"the {strike_row['strike']:g}{strike_row['put_call']} segment "
                "starts at the plot edge, but it was opened after the first bar"
            )
        if ended and ended < last and float(raw_x2) >= right:
            return bad(
                f"the {strike_row['strike']:g}{strike_row['put_call']} segment "
                "runs to the plot edge, but it went flat before the last bar"
            )
    # Eff delta: computed per bar and useless if it never reaches the page.
    if replay.get("marks") and 'class="dline"' not in p.markup:
        return bad(f"{len(replay['marks'])} marks held, no eff-delta series drawn")
    if 'class="rkey"' not in p.markup:
        return bad("no legend, so the hue and dash encodings are unexplained")
    return ok()


def check_drilldown_renders_from_url(p: Page) -> Verdict:
    """A calendar day panel opens from the URL alone, with one cell selected.

    Before `calday` joined the hash this state was unreachable without a click,
    which meant it could not be asserted at all.
    """
    if not p.calday:
        return skip("not a drill-down page")
    days = [d.get("day") for d in ((p.payload.get("stats") or {}).get("days") or [])]
    if p.calday not in days:
        # A stale or invented day must heal away rather than render an empty
        # panel claiming that date had fills.
        if re.search(re.escape(p.calday) + r"\s*—\s*\d+\s*fill", p.markup):
            return bad(f"stale calday={p.calday} rendered a panel for a day with no fills")
        return ok()
    if not re.search(re.escape(p.calday) + r"\s*—\s*\d+\s*fill", p.text):
        return bad(f"calday={p.calday} in the URL but no drill-down panel rendered")
    selected = p.markup.count(" selected")
    if selected != 1:
        return bad(f"{selected} calendar cells marked selected, expected exactly 1")
    return ok()


def check_drilldown_legs_have_context(p: Page) -> Verdict:
    """Every leg row in a drill-down carries `.ctx`.

    Without it the row loses the columns that say which contract and strategy
    the fill belonged to -- the whole point of the panel.
    """
    if not p.calday:
        return skip("not a drill-down page")
    panel = re.search(r'fill\(s\)(.*)$', p.markup, re.S)
    if not panel:
        return skip("no drill-down panel on this page")
    legs = re.findall(r'<div class="leg([^"]*)"', panel.group(1))
    if not legs:
        return skip("panel rendered no leg rows")
    plain = [f"leg{c}" for c in legs if "ctx" not in c]
    if plain:
        return bad(f"{len(plain)} drill-down leg row(s) missing .ctx: {plain[:3]}")
    return ok()


#: Every check, run against every page. Each decides for itself whether it
#: applies, so adding a page coordinate cannot silently skip a check and
#: adding a check cannot miss a page.
CHECKS: tuple[Check, ...] = (
    check_page_rendered,
    check_no_junk_bindings,
    check_selected_tab_matches_hash,
    check_header_icons_grouped,
    check_money_keys_are_shaped,
    check_costs_block_shares_one_basis,
    check_calendar_pills_match_grid,
    check_positions_subtotal_column,
    check_positions_side_is_colourable,
    check_no_blank_contract_cells,
    check_closing_events_are_captioned,
    check_no_uncaught_javascript,
    check_replay_renders_from_url,
    check_drilldown_renders_from_url,
    check_drilldown_legs_have_context,
)


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

TABS = ("dashboard", "calendar", "trades", "positions", "costs", "annual", "odte")


def page_coords(quote: str | None) -> list[tuple[str, str | None, str | None]]:
    """(tab, ccy, kind) for every page worth rendering.

    Every tab at default, then the axes that changed a figure's basis: the
    currency toggle across the tabs that show money, and the asset-category
    switch across the three tabs whose filter bar offers it. Not a full cross
    product -- that multiplies pages without adding a distinct render path.
    """
    coords: list[tuple[str, str | None, str | None]] = [(t, None, None) for t in TABS]
    if quote:
        coords += [(t, quote, None) for t in ("dashboard", "annual", "costs", "trades")]
    coords += [("trades", None, "equities"), ("dashboard", quote, "equities")]
    coords += [("odte", None, "odte")]
    return coords


@contextmanager
def _serve(db_path: Path, archive_dir: Path, query_id: str | None) -> Iterator[str]:
    """The real handler over a real journal on an ephemeral port.

    Wired exactly as `serve()` wires production -- same ServeConfig, same
    handler -- so the sweep exercises the request path rather than a lookalike,
    and never collides with a journal already serving on 8765/8766.
    """
    cfg = web.ServeConfig(
        db_path=db_path,
        archive_dir=archive_dir,
        query_id=query_id,
        assets=tuple(DEFAULT_ASSET_FILTER),
    )

    class _Srv(http.server.ThreadingHTTPServer):
        daemon_threads = True

    httpd = _Srv(("127.0.0.1", 0), partial(web._Handler, cfg))
    port = httpd.socket.getsockname()[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


@dataclass
class Result:
    """What the sweep found, per page and in total."""

    pages: list[tuple[str, str, list[tuple[str, Verdict]]]] = field(default_factory=list)

    @property
    def failures(self) -> list[tuple[str, str, str, str]]:
        return [
            (journal, label, name, v.detail)
            for journal, label, checks in self.pages
            for name, v in checks
            if v.status == FAIL
        ]

    def tally(self) -> dict[str, int]:
        out = {PASS: 0, FAIL: 0, SKIP: 0}
        for _, _, checks in self.pages:
            for _, v in checks:
                out[v.status] += 1
        return out


def sweep_journal(
    name: str,
    db_path: Path,
    archive_dir: Path,
    profile: Path,
    query_id: str | None = None,
    caldays: int = 2,
) -> Result:
    """Render one journal's whole matrix and apply every check to every page."""
    result = Result()
    with _serve(db_path, archive_dir, query_id) as base:
        with urllib.request.urlopen(base + "/api/state") as res:
            payload = json.load(res)
        quotes = [q["code"] for q in (payload.get("fx") or {}).get("quotes") or []]
        quote = quotes[0] if quotes else None

        coords: list[tuple[str, str | None, str | None, str | None, str | None]] = [
            (t, c, k, None, None) for t, c, k in page_coords(quote)
        ]
        # Drill-downs, which only became URL-addressable when calday joined the
        # hash. One invented day proves the healing branch.
        all_days = (payload.get("stats") or {}).get("days") or []
        days = [d["day"] for d in all_days if d.get("trades")]
        coords += [("calendar", None, None, d, None) for d in days[:caldays]]
        coords += [("calendar", None, None, "1999-01-01", None)]
        # Replay panels. A lifecycle key exercises the Trades entry point and a
        # standalone position key the snapshot-only one -- the LEAP, whose chart
        # is reachable ONLY from Positions because it has no lifecycle at all.
        # One invented key proves the healing branch, the same shape as the
        # invented calday above.
        replays = payload.get("replays") or {}
        lc_keys = [k for k in replays if k.startswith("lc:")]
        pos_keys = [k for k in replays if k.startswith("pos:")]
        coords += [("trades", None, None, None, k) for k in lc_keys[:2]]
        coords += [("positions", None, None, None, k) for k in pos_keys[:1]]
        coords += [("trades", None, None, None, "lc:nosuchconid@1999-01-01")]

        for tab, ccy, kind, calday, replay in coords:
            page = Page(tab=tab, ccy=ccy, kind=kind, calday=calday, replay=replay,
                        dom="", markup="", text="", payload=payload)
            dom = browser.dump_dom(base + "/" + page.url_hash(), profile)
            if dom is None:
                result.pages.append(
                    (name, page.label, [("render", skip("no browser produced a DOM"))])
                )
                continue
            page = Page(tab=tab, ccy=ccy, kind=kind, calday=calday, replay=replay,
                        console=tuple(browser.last_console_errors()),
                        dom=dom, markup=browser.markup(dom),
                        text=browser.rendered_text(dom), payload=payload)
            result.pages.append((name, page.label, [(c.__name__, c(page)) for c in CHECKS]))
    return result


def format_report(results: list[Result]) -> str:
    """A report that states its skips, because a skip is not a pass."""
    lines: list[str] = []
    total = {PASS: 0, FAIL: 0, SKIP: 0}
    for result in results:
        for journal, label, checks in result.pages:
            tally = {PASS: 0, FAIL: 0, SKIP: 0}
            for _, v in checks:
                tally[v.status] += 1
                total[v.status] += 1
            mark = "FAIL" if tally[FAIL] else "ok"
            lines.append(
                f"  {mark:4}  {journal:5} {label:38}  "
                f"{tally[PASS]:2} pass  {tally[SKIP]:2} skip"
            )
            for name, v in checks:
                if v.status == FAIL:
                    lines.append(f"          -> {name}: {v.detail}")
    head = (
        f"{total[PASS]} passed, {total[FAIL]} failed, {total[SKIP]} skipped "
        f"across {sum(len(r.pages) for r in results)} pages"
    )
    return head + "\n" + "\n".join(lines)
