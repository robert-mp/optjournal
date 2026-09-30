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

/** The same clamp, keeping the fraction between bars.
 *
 * Playback used to step one whole bar per timer tick, so the marker jumped from
 * data point to data point. This is what lets it MOVE instead: a fractional
 * position interpolates the marker's x and the reveal clip, while every VALUE
 * still comes from a real bar (see frameAt).
 */
export function clampPosition(value, length) {
  const position = Number(value);
  if (!Number.isFinite(position) || length < 1) return 0;
  return Math.max(0, Math.min(length - 1, position));
}

/** Linear x for a fractional position between two bars.
 *
 * Only the GEOMETRY is interpolated. The x axis is ordinal -- bars are evenly
 * spaced by index, not by clock -- so a straight line between neighbouring xs is
 * exactly where the marker belongs, with no assumption about elapsed time. A
 * session gap is one index step like any other, which is why the chart marks
 * breaks separately (sessionBreaks) rather than spacing them out.
 */
export function xAtPosition(xs, position) {
  if (!xs.length) return 0;
  const clamped = clampPosition(position, xs.length);
  const low = Math.floor(clamped);
  const high = Math.min(xs.length - 1, low + 1);
  const t = clamped - low;
  return xs[low] + (xs[high] - xs[low]) * t;
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
 *
 * A FRACTIONAL `value` is accepted, and this is the line between smooth motion
 * and fabricated data. `x` and `revealWidth` interpolate, so the marker glides.
 * `index`, `ts`, `price`, `pnl` and `delta` snap to the NEAREST REAL BAR, so
 * every number the panel prints is one a bar actually recorded. Interpolating
 * those would contradict markAt directly, whose whole point is that a
 * neighbouring bar's P&L presented as this bar's is a quiet fabrication -- and a
 * price between two closes is a price the option never traded at.
 */
export function frameAt(state, value) {
  const { points, marks = [], xs } = state;
  const position = clampPosition(value, points.length);
  const index = clampIndex(Math.round(position), points.length);
  const [ts, price] = points[index];
  const mark = markAt(marks, ts);
  const x = xAtPosition(xs, position);
  return {
    index,
    position,
    ts,
    price,
    x,
    revealWidth: x + 1.2,
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

/** The delta line as SEGMENTS, split wherever the position was not held.
 *
 * A gap is the statement. `modelled_marks` reports delta as null before the
 * opening fill and after a close takes the position flat, because on a symmetric
 * axis 0.0 means delta-neutral rather than absent. One polyline over those bars
 * would draw a straight line from the last real delta to the first one after the
 * gap -- inventing a smooth glide across a stretch holding nothing, which is the
 * same fabrication in line form that 0.0 was in number form.
 *
 * Returns a list of point lists, one per continuous holding period, so a contract
 * closed and later reopened draws two separate lines rather than one joined
 * across the flat. A single held bar yields a one-point segment: the caller draws
 * those as dots, since a polyline of one point renders nothing at all.
 */
export function deltaSegments(marks, geometry, yOf) {
  const segments = [];
  let run = [];
  for (const row of marks) {
    const value = row[2];
    if (!Number.isFinite(value)) {
      if (run.length) segments.push(run);
      run = [];
      continue;
    }
    const spot = geometry.at(row[0]);
    if (!spot) continue;
    run.push([spot.x, yOf(value)]);
  }
  if (run.length) segments.push(run);
  return segments;
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

/** The bar an instant belongs to: the first bar whose close is at or after it.
 *
 * A point is stamped at its bar's CLOSE (replay._drawn), the instant the bar is
 * priced at, so a fill at 10:35 belongs to the bar stamped 11:30: that bar spans
 * 10:30 to 11:30, and its P&L already holds the fill. The nearer point, stamped
 * 10:30, closed before the fill happened; taking it would place the event a bar
 * early, which on a daily chart is a whole session.
 *
 * It is the frame `reachedEvents` first lights the event at, so a card seeks
 * to, and playback stops on, the frame where the card, its dot and the P&L
 * readout all show it. An instant after the last close clamps to the last bar.
 */
export function indexOfTs(points, ts) {
  if (!points.length || ts == null || !Number.isFinite(ts)) return 0;
  for (let i = 0; i < points.length; i++) {
    if (points[i][0] >= ts) return i;
  }
  return points.length - 1;
}

/** Which event annotations the replay has reached, by timestamp.
 *
 * `ts <= frame` rather than a pixel comparison, but chosen to agree with one:
 * the fill dots are clipped by the reveal edge, so a card appearing while its
 * dot is still hidden (or the reverse) would have the panel contradict itself
 * mid-scrub. An event inside a bar is reached at THAT bar, since the bar's point
 * is stamped at its close: its interpolated x falls between the previous point
 * and this one, and the bar's mark already counts it.
 *
 * Returns the timestamps rather than the objects, so the caller can toggle
 * existing DOM by key instead of re-rendering cards on every scrub tick.
 */
export function reachedEvents(events, ts) {
  return events.filter((e) => e && Number.isFinite(e.ts) && e.ts <= ts)
    .map((e) => e.ts);
}

/** The bar index playback must not advance past, given where it started.
 *
 * Playback STOPS at a decision rather than sliding through it. A roll is the
 * moment the trade changed shape, and the whole point of a replay is to sit at
 * that moment and read what it did -- at 4x, an event card lit for a third of a
 * second and the reader saw a strike move with no idea why.
 *
 * The bar an event stops on is `indexOfTs`'s, the same one its card seeks to and
 * the first whose frame reveals its dot and lights its card, so the pause lands
 * exactly where the annotation is rather than a bar either side of it.
 *
 * `from` is EXCLUSIVE, which is what lets play resume: standing on a stop and
 * pressing play again looks past it to the next one instead of halting on the
 * spot forever. Returns null when no event lies ahead, meaning "run to the end".
 */
export function nextStop(events, points, from) {
  let best = null;
  for (const event of events) {
    if (!event || !Number.isFinite(event.ts)) continue;
    const index = indexOfTs(points, event.ts);
    if (index <= from) continue;
    if (best === null || index < best) best = index;
  }
  return best;
}

/** Bars per millisecond, so a replay's LENGTH does not set its pace.
 *
 * The speed control used to mean milliseconds per bar, which made the setting a
 * different promise on every trade: a 24-bar strangle and a 461-bar LEAP at "1x"
 * ran the same bars-per-second and therefore took 19x longer for the LEAP. The
 * reader's question is "watch this trade", not "watch 240ms of each of its
 * bars", so the duration is what should be fixed and the pace what should
 * follow.
 *
 * `secondsFor` is the wall-clock a whole replay should take at 1x; a speed
 * multiplier divides it. Two bars is the floor `MIN_POINTS` guarantees, and a
 * one-bar series would otherwise divide by zero and advance infinitely fast.
 */
export function barsPerMs(length, secondsFor, speed = 1) {
  const bars = Math.max(1, length - 1);
  const ms = (Math.max(0.001, secondsFor) * 1000) / Math.max(0.001, speed);
  return bars / ms;
}

/** Round axis values inside a domain: the "nice number" rule every charting
 * library uses, and the one this chart was missing.
 *
 * The performance chart labelled `[hi, (hi+lo)/2, lo]` -- the data's own padded
 * extremes -- so a reader got "€1,626 / €726 / −€174". Those are three numbers
 * nobody chose, they change on every fill, and none of them is the one value that
 * matters on a cumulative P&L chart: zero.
 *
 * The step comes from {1, 2, 2.5, 5, 10} x 10^n, so a label is always a figure a
 * person would say out loud. Ticks are then the multiples of that step which fall
 * INSIDE the domain -- the domain is NOT extended to whole steps, which is the
 * other common approach and costs real plot height here: rounding [-174, 1626]
 * out to [-500, 2000] would leave the series using 69% of the card and read as a
 * smaller move than happened. So the line keeps the full height and the labels
 * are still round; the top gridline simply need not be flush with the frame.
 *
 * Zero lands on a gridline whenever it is in range, which is free: it is a
 * multiple of every step.
 *
 * @param {number} lo domain minimum
 * @param {number} hi domain maximum
 * @param {number} target roughly how many ticks are wanted
 * @returns {number[]} ascending round values within [lo, hi]
 */
export function niceTicks(lo, hi, target = 4) {
  if (!isFinite(lo) || !isFinite(hi) || hi <= lo || target < 1) return [];
  const raw = (hi - lo) / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  // The first multiple that covers `raw`. 2.5 is in the list because a 250 step
  // is a figure people read fluently and a 200 or 300 step is not.
  const step = [1, 2, 2.5, 5, 10].find((m) => raw <= m * mag) * mag;
  const out = [];
  // Both ends compared with a billionth of a step to spare, because the division
  // and the product each carry float error: 3 * 0.1 is 0.30000000000000004, so
  // `niceTicks(0, 0.3)` dropped its top tick, and 1.1 / 0.1 is 11.000000000000002,
  // which `ceil` took past the bottom one.
  const slack = 1e-9;
  // `Math.round(v / step) * step` rather than accumulating `v += step`: adding a
  // float repeatedly drifts, and a tick at 1499.9999999999998 formats as a round
  // number while sitting a hair off its own gridline.
  for (let k = Math.ceil(lo / step - slack); k * step <= hi + step * slack; k += 1) {
    // `+ 0` normalises NEGATIVE ZERO. `Math.ceil(-174 / 500)` is -0, and -0 *
    // step stays -0, which `toLocaleString` renders with a minus sign: the axis
    // would have labelled break-even "−€0". Caught by the unit test, not by
    // reading the arithmetic.
    out.push(Math.round(k * step * 1e6) / 1e6 + 0);
  }
  return out;
}
