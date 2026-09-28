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

export const num = (value, digits = 2) => value == null
  ? null
  : Number(value).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });

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
  return `${amount < 0 ? "−" : ""}${sym(currency)}${
    num(Math.abs(amount), digits == null ? dp(amount) : digits)
  }`;
}

export const cls = (value) => value == null || Number(value) === 0
  ? ""
  : Number(value) > 0 ? "pos signed" : "neg signed";

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
  : Number(value).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    useGrouping: false,
  });

export function strike(value) {
  if (value == null) return "";
  const amount = Number(value);
  if (Number.isInteger(amount)) return num(amount, 0);
  const decimals = String(value).split(".")[1]?.length || 0;
  return num(amount, decimals > 2 ? 2 : decimals);
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
