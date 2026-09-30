/* Pure presentation helpers shared by every tab.
 *
 * They live outside page.html so formatting rules are executable without a DOM.
 * A monetary sign, half strike, or date label can otherwise be wrong while the
 * page still renders valid markup.
 */

export const esc = (value) => String(value ?? "").replace(
  /[&<>"]/g,
  (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[char],
);

/** The minus sign every formatter here prints: U+2212, the typographic minus,
 * which `money` always used. Nothing parses a formatted figure back into a number
 * (an <input> is filled with `toFixed`), so the hyphen buys nothing. */
const MINUS = "−";

/* `value` at `digits` places without its sign, and whether what prints is ZERO.
 * Zero is decided on the printed text, by the same formatter at the same
 * precision, so a value that rounds away is exactly one that prints as zero:
 * -0.0028 at two places is "0.00", and a minus before it would say it was below
 * something it prints as equal to. */
function figure(value, digits, grouping = true) {
  const options = {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    useGrouping: grouping,
  };
  const text = Math.abs(value).toLocaleString(undefined, options);
  return { text, zero: text === (0).toLocaleString(undefined, options) };
}

function signed(value, digits, grouping) {
  const amount = Number(value);
  const { text, zero } = figure(amount, digits, grouping);
  return amount < 0 && !zero ? `${MINUS}${text}` : text;
}

export const num = (value, digits = 2) => value == null ? null : signed(value, digits);

const CURRENCIES = { EUR: "€", USD: "$", GBP: "£", JPY: "¥", KRW: "₩" };

export function sym(currency) {
  const code = String(currency || "").toUpperCase();
  return CURRENCIES[code] || (code ? `${code} ` : "");
}

export function dp(value) {
  const amount = Math.abs(Number(value));
  return amount > 0 && amount < 0.005 ? 4 : 2;
}

export function money(value, currency, digits) {
  if (value == null) return "—";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "—";
  const { text, zero } = figure(amount, digits == null ? dp(amount) : digits);
  return `${amount < 0 && !zero ? MINUS : ""}${sym(currency)}${text}`;
}

/** An amount in at most five characters, for a cell too narrow for `money`: a
 * calendar day on a phone, where "−€1,729.42" was clipped to "−€1,7". No symbol
 * and no cents, because the month's total beside the grid carries the currency
 * and the day's own label carries the exact figure: 716, −198, 1.1k, −3.3k, 12k.
 * Anything that rounds to zero prints without a sign, so there is no "−0".
 */
export function compact(value) {
  if (value == null) return "—";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "—";
  const size = Math.abs(amount);
  const body = size < 999.5 ? num(Math.round(size), 0)
    : size < 9950 ? `${num(size / 1000, 1)}k`
    : `${num(Math.round(size / 1000), 0)}k`;
  return `${amount < 0 && body !== "0" ? MINUS : ""}${body}`;
}

/** The sign class for a figure: "pos signed", "neg signed", or "" for zero.
 *
 * Judged on the figure AS PRINTED, because both halves of the class are claims
 * about its sign: the hue, and the "+" that `.pos.signed` draws. `digits` is the
 * precision the caller prints at; without it, the one `money` would choose. A
 * value that rounds to zero there is zero, so it is neither red nor "+0.00".
 */
export function cls(value, digits) {
  if (value == null) return "";
  const amount = Number(value);
  if (!(amount > 0 || amount < 0)) return "";
  if (figure(amount, digits == null ? dp(amount) : digits).zero) return "";
  return amount > 0 ? "pos signed" : "neg signed";
}

/** An index level or a listed strike, UNGROUPED: 7706.03, never 7,706.03.
 *
 * Its own formatter rather than `num`, and the difference is one separator that
 * matters at the density the 0DTE ladder is read: seven numeric columns of
 * four-digit levels, scanned for where a strike sits relative to its neighbours,
 * where a thousands comma in every cell is ink that carries nothing -- these are
 * always thousands. It is also how every platform quoting SPX prints a strike, and
 * a ladder that has to be checked against a broker screen should not differ from
 * it typographically. Money keeps its grouping, everywhere, through `num`.
 */
export const level = (value, digits = 2) => value == null
  ? "—"
  : signed(value, digits, false);

/** A listed strike wherever a contract is named: as `level` prints it, UNGROUPED,
 * and only to the places it was listed at (267.5, 7755, never 7,755). Every tab
 * names contracts through this, so a strike reads the same on the Trades tab, the
 * replay chart, the Watchlist and the 0DTE ladder. */
export function strike(value) {
  if (value == null) return "";
  const amount = Number(value);
  if (Number.isInteger(amount)) return level(amount, 0);
  const decimals = String(value).split(".")[1]?.length || 0;
  return level(amount, decimals > 2 ? 2 : decimals);
}

export const pct = (value, digits = 1) => value == null
  ? "—"
  : `${num(value, digits)}%`;

const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

export function monthLabel(month) {
  if (!month || month === "ALL") return "All time";
  const [year, number] = month.split("-");
  return `${MONTHS[Number(number) - 1]} ${year}`;
}

export function dayLabel(iso, withYear) {
  const [year, month, day] = String(iso || "").split("-");
  if (!year || !month || !day) return String(iso || "");
  return `${Number(day)} ${MONTHS[Number(month) - 1]}${
    withYear ? ` '${year.slice(2)}` : ""
  }`;
}
