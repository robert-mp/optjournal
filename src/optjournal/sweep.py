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
and silently served stale code when a server predated the last edit. Here each
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

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from optjournal import browser, web

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

    def _coords(self) -> list[str]:
        """The optional coordinates that are set, as `key=value`, in hash order.

        One walk feeding both the label and the URL, which differ only in their
        separators and whether `tab` carries its key. They were two twelve-line
        methods stepping through the same four fields in lockstep, so a new
        coordinate meant editing both -- and a page whose label disagreed with the
        hash that produced it is a sweep report naming a page it did not render.
        """
        return [
            f"{key}={value}"
            for key, value in (
                ("ccy", self.ccy),
                ("type", self.kind),
                ("calday", self.calday),
                ("replay", self.replay),
            )
            if value
        ]

    @property
    def label(self) -> str:
        return " ".join([self.tab, *self._coords()])

    def url_hash(self) -> str:
        return "#" + "&".join([f"tab={self.tab}", *self._coords()])


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

#: The page's own `CCY` table, parsed rather than restated.
#:
#: These were two independent literals and they had already drifted BOTH ways:
#: the page rendered `¥` for JPY, which the sweep did not know was a currency at
#: all, so a costs block mixing yen with anything else would have passed; and the
#: sweep listed `kr` for SEK, which the page never emits -- it has no SEK glyph
#: and falls back to the ISO prefix, so `kr` could only ever match by accident in
#: prose.
#:
#: A check that reads glyphs back out of the page has to know which glyphs the
#: page can produce, and the page is the only honest source for that. Parsed the
#: same way `tests/test_sweep.py` parses `TABS`, and for the same reason.
def _currency_glyphs() -> dict[str, str]:
    """`{ISO: glyph}` as page.html declares it, or the fallback if it moved."""
    from optjournal.web import page_html

    block = re.search(r"const CCY=\{(.*?)\};", page_html())
    if block is None:  # pragma: no cover - the page always declares it
        return dict(_CURRENCY_GLYPH_FALLBACK)
    return dict(re.findall(r"(\w+):'([^']+)'", block.group(1)))


#: Used only when the literal cannot be found, so a moved declaration degrades to
#: a stale check rather than to no check.
_CURRENCY_GLYPH_FALLBACK = {"EUR": "€", "USD": "$", "GBP": "£", "KRW": "₩"}


#: The Costs headline: the figure, then the split line beneath it. Matched on the
#: rendered classes rather than on prose, so rewording the caption cannot silently
#: disable the checks below.
_HERO = re.compile(
    r'class="fig(?P<est>[^"]*)">(?P<figure>[^<]*)</div>\s*'
    r'<div class="split">(?P<split>.*?)</div>',
    re.S,
)


def check_costs_headline_states_its_basis(p: Page) -> Verdict:
    """An estimated headline never appears without the range it came from.

    This tab leads with ONE figure, and roughly a quarter of this account's
    friction is the AutoFX rate markup -- a published constant applied to real
    notional, not a measurement. The midpoint is printed because a headline has to
    print something, so the guarantee that keeps it honest is structural: whenever
    the total contains an estimate, the split line beneath it must show the band.

    Checked against the PAYLOAD's own `is_estimated`, so a scope with nothing
    estimated is required NOT to show a range -- a check that only looked for the
    range would pass a page that showed one unconditionally, which would imply
    uncertainty in a figure IBKR billed exactly.
    """
    if p.tab != "costs":
        return skip("not the costs tab")
    hero = _HERO.search(p.markup)
    if hero is None:
        return skip("no cost headline rendered")
    friction = (
        ((p.payload.get("broker_costs") or {}).get("totals") or {}).get("friction")
        or {}
    )
    if not friction:
        return skip("no friction in the payload")
    estimated = bool(friction.get("is_estimated"))
    split = hero.group("split")
    if estimated and "estimated" not in split:
        return bad(
            "the headline includes an estimate but the line beneath it does not "
            "say so, so a midpoint reads as a measured total"
        )
    if estimated and "–" not in split:
        return bad("an estimated headline shows no range, only a point estimate")
    if not estimated and "estimated" in split.replace("nothing estimated", ""):
        return bad(
            "nothing in this scope was estimated, yet the headline claims part of "
            "it was -- that implies uncertainty in a figure IBKR billed exactly"
        )
    if estimated and "est" not in hero.group("est"):
        return bad("an estimated figure carries no marker, so it reads as exact")
    return ok()


def check_costs_ledger_shows_every_charged_currency(p: Page) -> Verdict:
    """Every currency the payload says was billed appears in the ledger.

    The reason costs are `Charge` rather than `Money`: under a scope spanning
    asset categories no single currency can speak for the total, and `Money`
    withholds the native entirely at that point. The ledger keeps all of them, so
    widening the scope adds columns instead of deleting exactness -- and a missing
    column is a charge the reader silently stopped being shown.

    The FX table and the currency toggle also carry other currencies, so this
    reads the LEDGER block only, matched by class.
    """
    if p.tab != "costs":
        return skip("not the costs tab")
    totals = (p.payload.get("broker_costs") or {}).get("totals") or {}
    charged = ((totals.get("friction") or {}).get("stated") or {}).get("charged") or {}
    if not charged:
        return skip("nothing charged in this scope")
    start = p.markup.find('class="ledger"')
    if start < 0:
        return bad(
            f"the payload reports charges in {sorted(charged)} but no ledger "
            "was rendered, so the as-charged figures are not on screen"
        )
    block = p.markup[start:p.markup.find("</div>", start) + 6]
    missing = sorted(code for code in charged if code not in block)
    if missing:
        return bad(f"charged currencies missing from the ledger: {missing}")
    return ok()


def check_costs_shows_what_it_cannot_attribute(p: Page) -> Verdict:
    """Fees reach the screen at every scope, or the total is overstated nothing.

    Account fees carry no asset category -- no fee row in this archive carries a
    contract or trade id -- so they cannot narrow with the reader's selection and
    are reported whole. The failure this guards is the tempting one: hiding them
    under a narrow scope, which makes a tab captioned "broker cost" quietly
    measure less than it claims.
    """
    if p.tab != "costs":
        return skip("not the costs tab")
    totals = (p.payload.get("broker_costs") or {}).get("totals") or {}
    unattributable = (totals.get("unattributable") or {}).get("base") or 0.0
    if not unattributable:
        return skip("nothing unattributable in this journal")
    if "attributable to nothing" not in p.text:
        return bad(
            f"{unattributable:.2f} of account-level cost is in the payload but the "
            "page renders no unattributable block, so the reader sees a total "
            "without the charges that make it up"
        )
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
    """A close that repeats its own card's strategy reads "Closed", not the name.

    The caption is contextual: inside a "Short put" card an event labelled
    "Short put close" is noise and should read "Closed", while an event that
    carries NEW information keeps its full name. That second half is what this
    check first got wrong. It demanded a "Closed" on every closed card, and a
    strangle closed one leg at a time has none -- its closes are "Short put close"
    and "Short call close" on a "Strangle" card, which is exactly the new
    information the rule exists to keep. Five real-journal pages failed on that
    for weeks while rendering correctly. So the defect is checked as it is
    defined: an event caption equal to its OWN card's label plus " close".
    """
    if p.tab not in ("trades", "odte"):
        return skip("no lifecycle cards on this tab")
    cards = re.findall(r'<div class="card"[^>]*>(.*?)(?=<div class="card"|$)', p.markup, re.S)
    seen = 0
    for card in cards:
        if "CLOSED" not in card:
            continue
        label = re.search(r'<span class="dim lbl">([^<]*)</span>', card)
        captions = re.findall(r'<span class="gl">[^<]*?— ([^<]*?)\s*<span', card)
        if not label or not captions:
            continue
        seen += 1
        repeated = f"{label.group(1).strip()} close"
        if repeated in (c.strip() for c in captions):
            return bad(f"a closing event repeats its card's strategy as {repeated!r} "
                       "instead of reading 'Closed'")
    if not seen:
        return skip("no closed lifecycle with captioned events on this page")
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


def check_no_source_comment_leaked_into_the_markup(p: Page) -> Verdict:
    """No `/*` in the rendered page, anywhere.

    The page is one file of JavaScript building HTML out of template literals, and
    the two languages have different comment syntax with no boundary between them
    on screen. A `/* ... */` written one line too deep -- inside a template literal
    rather than inside a `${...}` expression -- is not a comment at all: it is TEXT,
    and it lands in the middle of a tag, where the browser reads it as bogus
    attributes and renders the element looking almost right.

    Caught exactly that way while the 0DTE strip was being written: a five-line
    rationale ended up inside a `<span`, and every Python test stayed green because
    the page still parsed, still had its classes, and still said the right words.
    Only the rendered markup shows it.
    """
    if "/*" in p.markup:
        at = p.markup.index("/*")
        return bad(f"a source comment reached the markup: {p.markup[at:at + 60]!r}")
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
    # Band and marks come from ONE vol series, so either both exist or neither
    # does, and they must end on the same bar. Two consumers reading one series
    # can disagree: expected_move refuses a negative horizon while bs_price
    # quietly clamps to intrinsic, which had the mark series outliving the band
    # by two months on a contract that had already settled. The checks above are
    # each conditional on their own series, so neither could see the mismatch.
    band, marks = replay.get("band") or [], replay.get("marks") or []
    if bool(band) != bool(marks):
        return bad(
            f"{len(band)} band rows against {len(marks)} marks -- one vol series "
            "reached only one of its two consumers"
        )
    if band and marks and band[-1][0] != marks[-1][0]:
        return bad(
            "band ends at bar stamped "
            f"{band[-1][0]} but marks run to {marks[-1][0]} -- the position is "
            "being priced past the envelope's own horizon"
        )
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
    # Event annotation cards: one per decision, each seeking to its own bar.
    events = replay.get("events") or []
    cards = re.findall(r'class="evc[^"]*" data-rts="(\d+)" data-rseek="(\d+)"', p.markup)
    if len(cards) != len(events):
        return bad(f"{len(cards)} annotation cards for {len(events)} events")
    for (raw_ts, raw_seek), event in zip(cards, events, strict=False):
        if int(raw_ts) != event["ts"]:
            return bad(f"card timestamp {raw_ts} does not match event {event['ts']}")
        # A card seeks to the bar CONTAINING its event. Landing past the event
        # would jump the chart to a frame where the card is not yet revealed.
        seek = int(raw_seek)
        if not 0 <= seek < len(replay["points"]):
            return bad(f"card seeks to bar {seek}, outside {len(replay['points'])} bars")
        if replay["points"][seek][0] > event["ts"]:
            return bad(f"card for {event['at']} seeks past its own event")
    # Revealed on open. The strip renders hidden and bindReplayControls syncs it,
    # so a missing sync leaves every card invisible with the scrubber at the end.
    if events and p.markup.count('class="evc on') != len(events):
        return bad(f"{p.markup.count('class=\"evc on')} of {len(events)} cards "
                   "revealed with the scrubber at the last bar -- the open sync "
                   "did not run, so the strip stays blank until an interaction")
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
    check_costs_headline_states_its_basis,
    check_costs_ledger_shows_every_charged_currency,
    check_costs_shows_what_it_cannot_attribute,
    check_calendar_pills_match_grid,
    check_positions_subtotal_column,
    check_positions_side_is_colourable,
    check_no_blank_contract_cells,
    check_closing_events_are_captioned,
    check_no_uncaught_javascript,
    check_no_source_comment_leaked_into_the_markup,
    check_replay_renders_from_url,
    check_drilldown_renders_from_url,
    check_drilldown_legs_have_context,
)


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

TABS = ("dashboard", "calendar", "trades", "positions", "costs", "annual", "odte",
        "market", "watchlist")


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
    # web.serve_ephemeral, not a spin-up of our own: it wires the same
    # ServeConfig and handler production uses, so the sweep exercises the
    # real request path. This module and test_rendered had each built that
    # twelve-line block separately, byte-identical.
    with web.serve_ephemeral(
        db_path=db_path, archive_dir=archive_dir, query_id=query_id,
    ) as base:
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


def _tally(checks: list[tuple[str, Verdict]]) -> dict[str, int]:
    """Pass/fail/skip counts for ONE page's checks.

    Per page, which is the granularity the report prints. `Result` used to carry a
    per-JOURNAL `tally` property that nothing called, while `format_report`
    accumulated its own per-page counts inline -- so the two were never
    interchangeable and the unread one could not have been substituted for the
    read one. Keyed by all three statuses whether or not they occur, so a page
    with no skips still prints " 0 skip" rather than raising.
    """
    out = {PASS: 0, FAIL: 0, SKIP: 0}
    for _, verdict in checks:
        out[verdict.status] += 1
    return out


def format_report(results: list[Result]) -> str:
    """A report that states its skips, because a skip is not a pass."""
    lines: list[str] = []
    total = {PASS: 0, FAIL: 0, SKIP: 0}
    for result in results:
        for journal, label, checks in result.pages:
            tally = _tally(checks)
            for status, n in tally.items():
                total[status] += n
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
