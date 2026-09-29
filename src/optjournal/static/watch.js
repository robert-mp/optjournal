/* Watchlist arithmetic: pure functions over plain data, no DOM and no globals.
 *
 * The second module beside replay.js, and it exists for the same stated reason
 * (README's module table): a function belongs here if it takes data and returns
 * data, and the moment it touches `document`, an element id or `S` it belongs in
 * page.html. What that buys on this tab is the first EXECUTED test of any
 * watchlist arithmetic -- everything else about the tab is checked by rendering
 * it and reading the markup back, which catches a wrong string and says nothing
 * about whether a derivation is right.
 *
 * Six functions, in two groups, and the groups are here for opposite reasons.
 *
 * THE THREE DERIVATIONS -- `shownPrice`, `bxDirection`, `earningsSoon` -- are here
 * because two surfaces need the same answer. `shownPrice` is the original: it is
 * called by the table row AND by the detail pane, so the two structurally cannot
 * disagree about which session they are describing; see its docstring for what
 * happened when the derivation was written once per surface. `earningsSoon` is here
 * because one of its three cases is a JavaScript trap (`null >= 0` is true) that no
 * amount of reading the page would catch.
 *
 * THE THREE GEOMETRY FUNCTIONS are here because NOTHING ELSE CAN SEE THEM. A gauge's
 * arithmetic is the one part of a drawing that can be wrong while every check still
 * passes: an inverted knob, a clamp to the wrong domain or a dasharray computed
 * against a diameter instead of a radius all render a well-formed picture, contain no
 * `undefined` and no `NaN`, and read as a measurement. `sweep.check_no_junk_bindings`
 * cannot see any of them, and a source grep for `-50` passes just as happily when the
 * literal sits in a comment. An executed test sees them, so the numbers live where
 * node can run them and the page keeps only the markup they position.
 */

/** The price to print for one watched row, and the 1d change that BELONGS to it.
 *
 * Returns `{price, live, chg1}`:
 *   price -- the fetched quote when there is one, else the newest stored close,
 *            else null. Never 0: a symbol with no bars has stated no price, and
 *            0.00 in a price column is a claim the journal cannot make.
 *   live  -- whether `price` came from a quote. The surfaces mark a stored close
 *            with `*` and a title naming the session, because an undated price
 *            is the one thing `marketdata.parse_quote` refuses outright.
 *   chg1  -- percent, from the SAME session as `price`.
 *
 * THE 1d BASIS IS THE WHOLE POINT OF THIS FUNCTION. Caught in a browser, not
 * reasoned about: showing the fetched quote (773.26) beside a change computed
 * from stored bars (-0.16%) put two DIFFERENT sessions in one row, because the
 * bars ended Thursday and the quote was Friday's. A quote carries its own
 * `previous_close`, so whenever a quote is shown the change is derived from it;
 * without one, both the price and the change fall back to the bars and the row is
 * internally consistent again.
 *
 * A `previous_close` of 0 or a negative is treated as ABSENT rather than divided
 * by: the source has answered with something that is not a price, and the bars'
 * own change is the honest fallback. 5d has no quote-side equivalent and stays on
 * the bars either way -- it is a week, so one session of drift does not
 * misdescribe it, which is why this function does not touch it.
 *
 * @param {Object} w a Watch row (last, change_1d)
 * @param {Object|null} quote the Quote for its symbol, if one has been fetched
 * @returns {{price: number|null, live: boolean, chg1: number|null}}
 */
export function shownPrice(w, quote) {
  const row = w || {};
  const live = !!(quote && quote.price != null);
  const price = live ? quote.price : row.last == null ? null : row.last;
  const prior =
    live && Number.isFinite(quote.previous_close) && quote.previous_close > 0
      ? quote.previous_close
      : null;
  const chg1 =
    prior == null
      ? row.change_1d == null
        ? null
        : row.change_1d
      : ((quote.price - prior) / prior) * 100;
  return { price, live, chg1 };
}

/** Which way the B-Xtrender reading is moving: "up", "down", "flat" or null.
 *
 * The published indicator's state is its SIGN crossed with rising-or-falling, so
 * the second half is real information and not decoration. It arrives as a state
 * NAME rather than as a glyph or a colour because both of those are the page's
 * business: a glyph is what carries it on screen, since `app.css`'s own rule is
 * that hue alone does not survive red-green colour blindness (measured there:
 * `--ok` against `--bad` separates by dE 2.4 under deuteranopia), and a second
 * shade of green for "positive but weakening" would be a colour-only signal.
 *
 * null for a null delta, which is a real state: a symbol on the session it first
 * crosses `trend.MIN_SETTLED` has a value and no earlier window to compare it
 * with, so the payload sends null. "flat" is reserved for a delta that was
 * MEASURED at zero, and the two must not collapse -- one means "not moving", the
 * other means "not known yet".
 *
 * @param {number|null} delta bx_daily_delta from the payload
 * @returns {"up"|"down"|"flat"|null}
 */
export function bxDirection(delta) {
  if (delta == null || !Number.isFinite(Number(delta))) return null;
  const moved = Number(delta);
  return moved > 0 ? "up" : moved < 0 ? "down" : "flat";
}

/** Whether a recorded earnings date is `within` days from now: the one filter rule
 * whose input is TYPED rather than measured.
 *
 * `days` is the payload's `earnings_in_days`, which is signed and derived from the ET
 * trading day at serialize time. Three answers, and only the first excludes a row
 * from the filtered list:
 *   a date inside the window   -> true
 *   a date further out         -> false
 *   NO DATE, or a date already past -> false, and both for stated reasons.
 *
 * A NULL DATE IS NOT A NEAR ONE, and this function exists because of how easily that
 * gets lost: `null >= 0` is TRUE in JavaScript, so the obvious inline spelling
 * (`days >= 0 && days <= within`) hides every symbol with no date recorded the moment
 * the filter is switched on. The reader would then be looking at a list captioned
 * "nothing here reports within 28 days" when the truth is "nothing here reports within
 * 28 days THAT YOU HAVE TOLD ME ABOUT" -- a claim the journal has no source for, since
 * nothing it can reach publishes an earnings date.
 *
 * A PAST DATE IS KEPT for a different reason: it is not an upcoming report, and the
 * date stands until the reader records the next one, so hiding it would delete what
 * they typed from view.
 *
 * @param {number|null} days earnings_in_days from the payload, signed
 * @param {number} within the filter's threshold in days
 * @returns {boolean}
 */
export function earningsSoon(days, within) {
  if (days == null || !Number.isFinite(Number(days))) return false;
  const off = Number(days);
  return off >= 0 && off <= Number(within);
}

/** Where the meter's knob sits on its track: an x in the SVG's own units.
 *
 * `box` carries the track AND the domain -- `{x, w, lo, hi}` -- because those two
 * cannot be allowed to drift apart: the numbers printed at the ends of the scale
 * and the position of the knob between them have to come from one object or the
 * picture stops meaning what its labels say.
 *
 * THE DOMAIN IS A CALLER'S CONSTANT, NOT A MEASUREMENT OF THE DATA, and that is
 * the whole point of the parameter. B-Xtrender is an RSI minus 50, so it lives in
 * [-50, +50] by construction -- a real statable bound rather than a preference. It
 * is also generous: MEASURED over 756 real daily closes (about three years, this
 * repo's own fetcher at the watched window) and the 637 settled short-arm readings
 * they yield, the widest excursion across TSLA, GOOG, NVDA and SPY was -37.7 (SPY)
 * and +41.6 (TSLA), and no symbol came within eight points of both ends. So the
 * bounds are headroom rather than a fit, which is exactly what makes them stable.
 * Auto-scaling to the visible rows would instead make one knob position mean a
 * different reading on every refresh, and a reader comparing two symbols would be
 * comparing two scales. So the page passes the literal bounds and this function
 * never looks at a series.
 *
 * CLAMPED AT BOTH ENDS, not extrapolated. A value past the bound is a value the
 * indicator's arithmetic cannot produce, so a knob drawn outside the track would
 * be a drawing error rendered as data; pinned at the end it reads as "at the
 * limit", which is the honest picture of an impossible input.
 *
 * null for a null or unmeasurable input, so the caller has to branch rather than
 * receiving a knob parked at the left end -- which is a reading, and "no reading"
 * is not one.
 *
 * @param {number|null} value the figure to place, in the domain's own units
 * @param {{x: number, w: number, lo: number, hi: number}} box track and domain
 * @returns {number|null}
 */
export function meterKnob(value, box) {
  const b = box || {};
  const x = Number(b.x), w = Number(b.w), lo = Number(b.lo), hi = Number(b.hi);
  const v = Number(value);
  if (value == null || !Number.isFinite(v)) return null;
  if (!Number.isFinite(x) || !Number.isFinite(w)) return null;
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return null;
  const held = Math.min(hi, Math.max(lo, v));
  return x + ((held - lo) / (hi - lo)) * w;
}

/** The ring's value arc as an SVG `stroke-dasharray` pair, `{on, off}`.
 *
 * A dash pattern is how a circle is drawn part-way round: `on` is the length of
 * stroke that is painted and `off` the length left bare, both in user units, so
 * they must be computed from the CIRCUMFERENCE of the circle the arc is stroked
 * on. Passing a diameter, or the radius itself, yields a picture that is
 * plausible at every rank and right at none -- which is the class of defect this
 * module exists to make executable.
 *
 * 0 is an empty arc and 100 a full circle, and both are real states rather than
 * edges to be avoided: a rank of 0 means today's realised vol IS this year's
 * lowest reading, which is worth seeing as an empty ring rather than as nothing
 * drawn at all.
 *
 * CLAMPED to 0..100 because a rank is a position inside a range and cannot leave
 * it; a value that did would wrap the arc past its own start and read as a
 * SMALLER figure than it is. null in, null out, for `meterKnob`'s reason.
 *
 * Where the arc STARTS and which way it runs are not this function's business and
 * cannot be: a dasharray carries a length, not an angle. The caller rotates the
 * circle by -90 degrees so it begins at 12 o'clock, and `ringPoint` is what pins
 * that convention under test.
 *
 * @param {number|null} rank 0 to 100
 * @param {number} radius the circle's radius, in the SVG's own units
 * @returns {{on: number, off: number}|null}
 */
export function ringArc(rank, radius) {
  const r = Number(radius), v = Number(rank);
  if (rank == null || !Number.isFinite(v)) return null;
  if (!Number.isFinite(r) || r <= 0) return null;
  const circumference = 2 * Math.PI * r;
  const held = Math.min(100, Math.max(0, v));
  const on = (held / 100) * circumference;
  return { on, off: circumference - on };
}

/** A point a fraction of the way around a circle, CLOCKWISE FROM 12 O'CLOCK.
 *
 * Returns `{x, y}` as offsets from the centre, so the caller adds its own `cx`
 * and `cy`. Offsets rather than absolute coordinates because the convention being
 * fixed here is the direction of travel, and a test that has to supply a centre
 * to check it is a test with two things going on.
 *
 * `x = r*sin(turn)`, `y = -r*cos(turn)`, and the minus is the whole subtlety: SVG
 * y grows DOWNWARD, so 12 o'clock is negative y and the pair above walks top ->
 * right -> bottom -> left, which is clockwise on screen. Drop the minus and the
 * arithmetic still returns points on the circle, at the mirrored angle -- so a
 * threshold tick would sit at 100 minus the threshold and the picture would
 * disagree with the filter chips while looking entirely reasonable. The node
 * tests check 0, 0.25 and 0.5 for exactly that reason.
 *
 * The fraction is deliberately not clamped: it is an angle, and 1.25 turns is a
 * legitimate way to spell a quarter turn. What the caller must not do is hand it
 * a percentage -- hence `RVR_MID/100` at the call site.
 *
 * @param {number} pct fraction of a full turn, 0 at the top
 * @param {number} radius distance from the centre, in the SVG's own units
 * @returns {{x: number, y: number}|null}
 */
export function ringPoint(pct, radius) {
  const r = Number(radius), p = Number(pct);
  if (!Number.isFinite(r) || !Number.isFinite(p)) return null;
  const turn = 2 * Math.PI * p;
  return { x: r * Math.sin(turn), y: -r * Math.cos(turn) };
}

/** Whether a row's price alert stands, and whether the price has crossed it.
 *
 * `"hit"` when the shown price is at or beyond either level, `"set"` when a level
 * exists and has not been reached, `null` when there is no alert. Judged against
 * the price ON SCREEN, `shownPrice`'s, so the bell and the close beside it cannot
 * describe two different sessions. No price means no verdict: an alert that
 * cannot be judged is `"set"`, not `"hit"`.
 *
 * @param {number|null} price
 * @param {number|null} above
 * @param {number|null} below
 * @returns {"hit"|"set"|null}
 */
export function alertState(price, above, below) {
  const up = above != null && above > 0 ? Number(above) : null;
  const down = below != null && below > 0 ? Number(below) : null;
  if (up == null && down == null) return null;
  if (price == null) return "set";
  if ((up != null && price >= up) || (down != null && price <= down)) return "hit";
  return "set";
}

/** Where the $/$$/$$$ share-price filter cuts, in the quote's own currency. */
export const PRICE_TIERS = [50, 200];

/** The share-price tier: under 50, 50 to under 200, and 200 or more. Null
 * without a price, so an unpriced row matches no tier.
 *
 * @param {number|null} price
 * @returns {"1"|"2"|"3"|null}
 */
export function priceTier(price) {
  if (price == null || !(price > 0)) return null;
  if (price < PRICE_TIERS[0]) return "1";
  return price < PRICE_TIERS[1] ? "2" : "3";
}

/** The row's five-session B-Xtrender histogram, as bars in a box.
 *
 * Every bar grows up from the BASELINE, its height the reading's size against the
 * arm's own bound on that side (`box.hi` above zero, `box.lo` below), and the sign
 * is carried by `pos` for the colour: IAG's shape, and twice the height a
 * centre-line chart gets in a row this short, which is what made a -4.5 readable
 * as red at all. A full bar means the indicator's limit on every row alike. A bar is `faded` when the
 * arm moved TOWARD zero since the session before, the indicator's own light and
 * dark shades. A warm-up session (null) draws nothing and keeps its slot, so the
 * newest bar is always the rightmost.
 *
 * `min` is a floor on bar height, so a reading of 0.3 still draws a visible dash
 * rather than nothing, which would read as a missing session.
 *
 * @param {Array<number|null>} values oldest first
 * @param {{w: number, h: number, gap: number, lo: number, hi: number, min: number}} box
 * @returns {Array<{x: number, y: number, w: number, h: number, pos: boolean, faded: boolean}>}
 */
export function histBars(values, box) {
  const n = values.length;
  if (!n) return [];
  const width = (box.w - box.gap * (n - 1)) / n;
  const out = [];
  values.forEach((value, i) => {
    if (value == null) return;
    const limit = value >= 0 ? box.hi : -box.lo;
    const share = Math.min(1, Math.abs(value) / limit);
    const height = Math.max(box.min, share * box.h);
    const prev = i > 0 ? values[i - 1] : null;
    out.push({
      x: i * (width + box.gap),
      y: box.h - height,
      w: width,
      h: height,
      pos: value >= 0,
      faded: prev != null && Math.abs(value) < Math.abs(prev),
    });
  });
  return out;
}
