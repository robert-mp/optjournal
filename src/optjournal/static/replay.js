/* Replay chart maths: pure functions over plain data, no DOM and no globals.
 *
 * This file exists to be TESTED. Everything in page.html is verified by
 * rendering it in a real browser and reading the markup back (see sweep.py),
 * which catches a wrong string but says nothing about whether a scale is right
 * or whether scrubbing to bar 7 selects bar 7. Those are arithmetic, and
 * arithmetic wants unit tests -- so the arithmetic lives here and page.html
 * keeps only the markup that wraps it.
 *
 * The split is deliberate: a function belongs here if it takes data and returns
 * data. The moment it touches document, an element id, or S, it belongs in the
 * page. That boundary is what makes this file importable by `node --test`
 * without a browser or a DOM shim.
 */

/* An empty or single-point series has no shape to draw and no scale to derive.
 * Two points is the minimum that can express a direction. */
export const MIN_POINTS = 2;

/** Vertical domain covering the price path, the strikes, AND the band.
 *
 * The strikes have to be inside it: a strike is the level the whole chart exists
 * to compare against, so a far-OTM one clipped off-canvas would leave a price
 * line and no answer to "how close did it get", which is the question the panel
 * is for.
 */
export function domainOf(points, strikes = [], band = []) {
  const values = points.map((p) => p[1]);
  for (const strike of strikes) {
    if (Number.isFinite(strike.strike)) values.push(strike.strike);
  }
  for (const row of band) {
    if (Number.isFinite(row[1])) values.push(row[1]);
    if (Number.isFinite(row[2])) values.push(row[2]);
  }
  const low = Math.min(...values);
  const high = Math.max(...values);
  /* A flat series has zero range, which would divide by zero and collapse every
   * point onto one line. One unit of padding keeps it centred instead. */
  const pad = (high - low) * 0.07 || 1;
  return { lo: low - pad, hi: high + pad, span: high - low + 2 * pad };
}

/** Geometry for one panel: the two scales plus a wall-clock lookup.
 *
 * x is ORDINAL -- bar position, not elapsed time. Measured on this journal's own
 * TSLA trade, a linear time axis spent 80.9% of its width on hours the market
 * was shut, and all five of its largest visual moves were overnight or weekend
 * gaps rather than anything that happened while trading.
 */
export function plotGeometry(points, strikes, band, box) {
  const { left, top, width, height } = box;
  const { lo, hi, span } = domainOf(points, strikes, band);
  const last = points.length - 1;
  const xAt = (i) => left + (last < 1 ? width / 2 : (i * width) / last);
  const yOf = (v) => top + height - ((v - lo) * height) / span;
  return {
    lo,
    hi,
    span,
    xAt,
    yOf,
    xs: points.map((_, i) => xAt(i)),
    /* A wall-clock instant onto the ordinal axis: find the bar interval it falls
     * in and interpolate across it. Nearest-bar rounding would drop a fill onto
     * the hour mark, which on a daily chart moves an entry by half a session. */
    at(ts) {
      if (ts == null || !Number.isFinite(ts)) return null;
      if (ts <= points[0][0]) return { x: xAt(0), y: yOf(points[0][1]) };
      if (ts >= points[last][0]) return { x: xAt(last), y: yOf(points[last][1]) };
      for (let i = 1; i <= last; i++) {
        if (points[i][0] >= ts) {
          const room = points[i][0] - points[i - 1][0] || 1;
          const f = (ts - points[i - 1][0]) / room;
          return {
            x: xAt(i - 1) + f * (xAt(i) - xAt(i - 1)),
            y: yOf(points[i - 1][1] + f * (points[i][1] - points[i - 1][1])),
          };
        }
      }
      return { x: xAt(last), y: yOf(points[last][1]) };
    },
  };
}

/** Clamp a scrub position to a real bar index.
 *
 * A range input yields strings, and a fractional or out-of-range value would
 * index past the end of the series and read undefined -- which renders as the
 * word "undefined" in the readout rather than failing.
 */
export function clampIndex(value, length) {
  const index = Math.trunc(Number(value));
  if (!Number.isFinite(index) || length < 1) return 0;
  return Math.max(0, Math.min(length - 1, index));
}

/** Everything the panel shows for one scrub position.
 *
 * `revealWidth` is the clip that HIDES the future: a replay you can see the end
 * of is not reviewing a decision, it is reading an outcome. The half-unit of
 * slack keeps the marker at the revealed edge rather than cutting it in half.
 *
 * `pnl` and `delta` come from the modelled marks, looked up by TIMESTAMP rather
 * than by shared index: the marks series is shorter than the bars whenever a
 * leading bar had no solvable vol, so index alignment would silently report an
 * earlier bar's P&L against a later bar's price.
 */
export function frameAt(state, value) {
  const { points, marks = [], xs } = state;
  const index = clampIndex(value, points.length);
  const [ts, price] = points[index];
  const mark = markAt(marks, ts);
  return {
    index,
    ts,
    price,
    x: xs[index],
    revealWidth: xs[index] + 1.2,
    pnl: mark ? mark[1] : null,
    delta: mark ? mark[2] : null,
  };
}

/** The modelled mark for an exact bar timestamp, or null when there is none.
 *
 * Exact rather than nearest: a mark exists for a bar or it does not, and a
 * neighbouring bar's P&L presented as this bar's would be a quiet fabrication.
 * Points before the first solvable vol legitimately have none.
 */
export function markAt(marks, ts) {
  for (const row of marks) {
    if (row[0] === ts) return row;
    if (row[0] > ts) return null;
  }
  return null;
}

/** Indices where the ET calendar day changes, for the session rules.
 *
 * Takes the day-resolving function so this stays pure and testable: passing
 * dates in avoids depending on the host's Intl data inside a unit test.
 */
export function sessionBreaks(points, dayOf) {
  const breaks = [];
  for (let i = 1; i < points.length; i++) {
    if (dayOf(points[i][0]) !== dayOf(points[i - 1][0])) breaks.push(i);
  }
  return breaks;
}

/** Symmetric domain for the effective-delta axis, or null when there is none.
 *
 * Symmetric on purpose: delta-neutral is the state a strangle is opened in, and
 * it should read as the middle of the axis rather than as some arbitrary height.
 * The floor stops a genuinely flat series from being magnified into noise -- a
 * position that never moved off 0.001 should look flat, not dramatic.
 */
export function deltaDomain(marks, floor = 0.05) {
  const values = marks.map((m) => m[2]).filter((v) => Number.isFinite(v));
  if (!values.length) return null;
  const reach = Math.max(floor, ...values.map(Math.abs));
  return { lo: -reach, hi: reach, reach };
}

/** Pixel span of a strike's holding period, clamped to the plot.
 *
 * A null start or end means "beyond this chart": still held, or an entry date we
 * do not have. Clamping to the edge is the honest rendering of both -- the line
 * runs to where the picture stops, rather than stopping where the data does and
 * implying the position did too.
 */
export function strikeSpan(strike, geometry, box) {
  const from = strike.frm == null ? null : geometry.at(strike.frm);
  const to = strike.to == null ? null : geometry.at(strike.to);
  const x1 = from ? from.x : box.left;
  const x2 = to ? to.x : box.left + box.width;
  return { x1: Math.min(x1, x2), x2: Math.max(x1, x2) };
}

/** Upper and lower edges of the expected-move envelope, as point lists. */
export function bandEdges(band, geometry) {
  const upper = [];
  const lower = [];
  for (const row of band) {
    const spot = geometry.at(row[0]);
    if (!spot) continue;
    upper.push([spot.x, geometry.yOf(row[2])]);
    lower.push([spot.x, geometry.yOf(row[1])]);
  }
  return { upper, lower };
}

/** The bar an instant belongs to: the last bar at or before it.
 *
 * The bar CONTAINING the event, not the nearest one. A fill at 10:35 belongs to
 * the 10:30 bar because that bar spans 10:30-11:30; rounding to the nearest
 * would attribute it to 11:30, which on a daily chart moves an event a whole
 * session and puts it after bars that were actually later than it.
 */
export function indexOfTs(points, ts) {
  if (!points.length || ts == null || !Number.isFinite(ts)) return 0;
  let found = 0;
  for (let i = 0; i < points.length; i++) {
    if (points[i][0] > ts) break;
    found = i;
  }
  return found;
}

/** Which event annotations the replay has reached, by timestamp.
 *
 * `ts <= frame` rather than a pixel comparison, but chosen to agree with one:
 * the fill dots are clipped by the reveal edge, so a card appearing while its
 * dot is still hidden (or the reverse) would have the panel contradict itself
 * mid-scrub. An event inside a bar is reached at the FOLLOWING bar, which is
 * where its interpolated x actually falls.
 *
 * Returns the timestamps rather than the objects, so the caller can toggle
 * existing DOM by key instead of re-rendering cards on every scrub tick.
 */
export function reachedEvents(events, ts) {
  return events.filter((e) => e && Number.isFinite(e.ts) && e.ts <= ts)
    .map((e) => e.ts);
}
