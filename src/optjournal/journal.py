"""What the trader intended, which no statement records.

Everything else in this database is re-derivable. Delete `journal.db`, re-ingest
`raw/`, and the trades, the positions, the episodes, the campaigns and every
statistic come back identical -- that is the whole design, and it is why nothing
here caches a derived figure. A PLAN is different: it existed for a few seconds
in someone's head before an order went out, and no Flex query has ever carried
it. These rows are the only ones a lost file actually loses.

So this module is deliberately dull. Text, three small enumerations, and a key.
No greeks, no probability, no percent-of-net-liquidation -- the reference
implementation asks a reader to TYPE all of those at entry, and a typed number
that the app could compute is a number that will disagree with the app. What is
computable stays computed (`vol.rank` for IV rank, `campaigns` for the grouping,
`stats` for the outcome); what is not computable is what lives here.

KEYED ON AN ORDER ID. The unit a reader journals is the DECISION -- a strangle is
one entry, and a roll continues it rather than starting a second -- and that is
`campaigns.Campaign`. But a campaign is rebuilt on every ingest from a 90-second
clustering heuristic, and its `episode_indices` are positions in a list that is
itself rebuilt, so keying notes on any of that would lose them the first time a
roll changed a grouping. `Campaign.anchor` is the lowest order id the decision
filled under: IBKR issued it, it names one placement forever, and it does not
move when the campaign grows.

The anchor can still be orphaned -- a fill arriving inside the 90-second window
could join a cluster and lower its anchor -- so an entry also records the
underlying and the open date it was written against, and `orphans` reports any
row no campaign claims. The row then reads as "the META decision opened
2026-08-03" and can be re-attached by hand. Losing a reader's own writing
silently is the one failure this table may not have.

A campaign built only from position snapshots has no fills, therefore no order
id, therefore no anchor. It cannot be journalled, and `save` says so rather than
inventing a key: a position the archive holds no fills for is one this journal
cannot yet describe.

The one other thing here is a LINK: the reader saying two orders were one
decision when the 90-second window could not see it, a roll closed one day and
reopened the next. It is intent for the same reason a plan is. No statement
records that the second order continued the first.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from optjournal.db import DEFAULT_BROKER

__all__ = [
    "ADHERENCE",
    "TRIGGERS",
    "Entry",
    "JournalError",
    "delete",
    "link",
    "links",
    "entry_for",
    "entries",
    "orphans",
    "save",
    "unlink",
]

#: Answers to "did you follow the plan". THREE values, not a boolean: a trade
#: taken off at target never reached its invalidation, so "there was no loss exit
#: to follow" is a real answer and storing it as False would count a winner as
#: discipline broken -- which is the exact statistic this table exists to make
#: trustworthy.
ADHERENCE: tuple[str, ...] = ("yes", "no", "na")

#: Why the trade actually came off. A FIXED LIST because the question a journal
#: exists to answer is "how often do I close on a time stop rather than at
#: target", and free text cannot be grouped. `other` carries its own text field
#: so the list can stay short without forcing a wrong choice.
#:
#: Each name is the reason a desk would give, not a price movement: "the position
#: went against me" is not a trigger, it is what a trigger reacts to.
TRIGGERS: dict[str, str] = {
    "target": "Hit the profit target",
    "time": "Time-based (approaching expiry)",
    "max_loss": "Hit the maximum loss",
    "tested": "Tested side went in the money",
    "external": "External (earnings, news, macro)",
    "assigned": "Assigned or expired, not closed by me",
    "other": "Other",
}

#: Columns a caller may write, in the order the table declares them. The key
#: columns are absent on purpose: they come from the campaign, not from the form,
#: so a request cannot silently re-point an entry at another decision.
FIELDS: tuple[str, ...] = (
    "plan_target",
    "plan_invalidation",
    "entry_note",
    "followed_target",
    "followed_invalidation",
    "why_not_target",
    "why_not_invalidation",
    "exit_trigger",
    "exit_trigger_other",
    "lessons",
    "close_note",
)


class JournalError(ValueError):
    """A journal write that cannot be honoured, with a reader-facing reason."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One decision's journal row, as stored."""

    broker: str
    account_id: str
    anchor_order_id: str
    underlying_symbol: str | None
    opened_on: str | None
    created_at: str
    updated_at: str
    #: The written fields, `FIELDS` keys to text. Absent keys were never written;
    #: a present key holding None was cleared.
    values: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """Nothing written. A row can reach this by having every field cleared,
        and `save` deletes rather than storing one -- an empty entry would count
        against journal completeness while saying nothing.
        """
        return not any(v not in (None, "") for v in self.values.values())

    def payload(self) -> dict[str, Any]:
        return {
            "anchor": self.anchor_order_id,
            "account_id": self.account_id,
            "underlying": self.underlying_symbol,
            "opened_on": self.opened_on,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            **{k: self.values.get(k) for k in FIELDS},
        }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clean(value: Any) -> str | None:
    """Text as stored: stripped, and empty becomes None.

    One rule for every field, so a cleared textarea and an untouched one are the
    same absence. `''` and `None` would otherwise both mean "nothing written"
    while comparing unequal, and `is_empty` would then depend on which surface
    did the writing.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _validated(values: dict[str, Any]) -> dict[str, str | None]:
    """The writable fields, cleaned, with the two enumerations checked.

    Raises rather than coercing an unknown enum value to None: a caller sending
    `followed_target='true'` has a bug, and quietly storing "not answered"
    would make the adherence count wrong in the reassuring direction, which is
    the direction nobody audits.
    """
    unknown = sorted(set(values) - set(FIELDS))
    if unknown:
        raise JournalError(
            f"not journal fields: {unknown}. Writable: {sorted(FIELDS)}"
        )

    out = {k: _clean(v) for k, v in values.items()}
    for key in ("followed_target", "followed_invalidation"):
        answer = out.get(key)
        if answer is not None and answer not in ADHERENCE:
            raise JournalError(
                f"{key}={answer!r} is not one of {list(ADHERENCE)}"
            )
    trigger = out.get("exit_trigger")
    if trigger is not None and trigger not in TRIGGERS:
        raise JournalError(
            f"exit_trigger={trigger!r} is not one of {sorted(TRIGGERS)}"
        )
    return out


def _row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        broker=row["broker"],
        account_id=row["account_id"],
        anchor_order_id=str(row["anchor_order_id"]),
        underlying_symbol=row["underlying_symbol"],
        opened_on=row["opened_on"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        values={k: row[k] for k in FIELDS},
    )


def entry_for(
    conn: sqlite3.Connection,
    anchor_order_id: str,
    *,
    account_id: str,
    broker: str = DEFAULT_BROKER,
) -> Entry | None:
    """The entry written against this anchor, or None."""
    row = conn.execute(
        "SELECT * FROM journal_entries"
        " WHERE broker = ? AND account_id = ? AND anchor_order_id = ?",
        (broker, account_id, str(anchor_order_id)),
    ).fetchone()
    return None if row is None else _row_to_entry(row)


def entries(
    conn: sqlite3.Connection, *, broker: str | None = None
) -> dict[tuple[str, str, str], Entry]:
    """Every entry, keyed by `(broker, account_id, anchor)`.

    A dict rather than a list because every caller is answering "does THIS
    decision have an entry" for a page full of decisions, and one query plus a
    lookup is what keeps that off the N+1 path the Trades tab would otherwise
    take.
    """
    sql = "SELECT * FROM journal_entries"
    params: tuple[Any, ...] = ()
    if broker is not None:
        sql += " WHERE broker = ?"
        params = (broker,)
    out = {}
    for row in conn.execute(sql, params):
        entry = _row_to_entry(row)
        out[(entry.broker, entry.account_id, entry.anchor_order_id)] = entry
    return out


def save(
    conn: sqlite3.Connection,
    anchor_order_id: str | None,
    *,
    account_id: str,
    values: dict[str, Any],
    underlying_symbol: str | None = None,
    opened_on: str | None = None,
    broker: str = DEFAULT_BROKER,
) -> Entry | None:
    """Write one decision's entry, or delete it when nothing is left.

    Returns the stored entry, or None when the write emptied it. Saving an entry
    with every field blank DELETES rather than storing a row of nulls: an empty
    entry is indistinguishable from no entry to a reader, but would count as
    "journalled" in any completeness tally.

    `anchor_order_id=None` is the snapshot-only campaign -- no fills, so no order
    to key on -- and raises rather than storing under a placeholder, because a
    placeholder would collide with the next such campaign.

    Merges into an existing row: only the keys present in `values` are touched,
    so the entry form and the close-review form can be separate surfaces without
    either blanking the other's fields. Passing a key with an empty value clears
    it, which is how a reader deletes a sentence they no longer stand behind.

    `created_at` survives an update for the same reason `trades.first_seen_at`
    does: when the journal first gained this entry is a fact about the journal.
    """
    if anchor_order_id is None:
        raise JournalError(
            "this position has no fills in the archive, only a snapshot, so "
            "there is no order id to attach a journal entry to"
        )
    fields = _validated(values)
    anchor = str(anchor_order_id)
    existing = entry_for(conn, anchor, account_id=account_id, broker=broker)
    merged = dict(existing.values) if existing else {}
    merged.update(fields)
    stamp = _now()

    if not any(v not in (None, "") for v in merged.values()):
        delete(conn, anchor, account_id=account_id, broker=broker)
        return None

    columns = ("broker", "account_id", "anchor_order_id",
               "underlying_symbol", "opened_on",
               *FIELDS, "created_at", "updated_at")
    # Identity is re-stated on conflict but `created_at` is not: see the
    # docstring. `underlying_symbol` and `opened_on` ARE refreshed, since they
    # describe which decision the anchor currently belongs to and a corrected
    # symbol should reach the row that quotes it back.
    updates = ", ".join(
        f"{c}=excluded.{c}" for c in columns
        if c not in {"broker", "account_id", "anchor_order_id", "created_at"}
    )
    conn.execute(
        f"INSERT INTO journal_entries ({', '.join(columns)})"
        f" VALUES ({', '.join('?' for _ in columns)})"
        " ON CONFLICT(broker, account_id, anchor_order_id) DO UPDATE SET"
        f" {updates}",
        (broker, account_id, anchor, underlying_symbol, opened_on,
         *(merged.get(k) for k in FIELDS),
         existing.created_at if existing else stamp, stamp),
    )
    conn.commit()
    stored = entry_for(conn, anchor, account_id=account_id, broker=broker)
    return stored


def delete(
    conn: sqlite3.Connection,
    anchor_order_id: str,
    *,
    account_id: str,
    broker: str = DEFAULT_BROKER,
) -> bool:
    """Remove an entry. True if a row went."""
    cur = conn.execute(
        "DELETE FROM journal_entries"
        " WHERE broker = ? AND account_id = ? AND anchor_order_id = ?",
        (broker, account_id, str(anchor_order_id)),
    )
    conn.commit()
    return bool(cur.rowcount)


def orphans(
    conn: sqlite3.Connection, live_anchors: set[str]
) -> list[Entry]:
    """Entries no current campaign claims.

    `live_anchors` is every `Campaign.anchor` the caller resolved, PASSED IN rather
    than derived here, so the irreplaceable table's module does not acquire the
    layer that rebuilds everything else. `tests/test_layering.py` holds that to
    `db` alone.

    Expected to be empty, and worth reporting anyway. Clustering decides
    membership from a 90-second window, so a fill arriving late inside that window
    can lower a campaign's anchor and leave the note written against the old one
    pointing at nothing. The row still records its underlying and open date, so an
    orphan is a thing a reader can act on rather than a loss they never hear
    about.
    """
    return [
        entry for key, entry in sorted(entries(conn).items())
        if key[2] not in live_anchors
    ]


def _pair(a: str, b: str) -> tuple[str, str]:
    """One spelling per pair, lower id first. By length then text, which is
    numeric order for the digit strings IBKR issues and total for anything else.
    """
    first, second = sorted((str(a), str(b)), key=lambda o: (len(o), o))
    return first, second


def links(
    conn: sqlite3.Connection, broker: str = DEFAULT_BROKER
) -> list[tuple[str, str]]:
    """Every hand-made link, as `campaigns.link` takes them."""
    return [
        (row["order_id"], row["joins_order_id"])
        for row in conn.execute(
            "SELECT order_id, joins_order_id FROM campaign_links"
            " WHERE broker = ? ORDER BY order_id, joins_order_id",
            (broker,),
        )
    ]


def link(
    conn: sqlite3.Connection, a: str, b: str, *, broker: str = DEFAULT_BROKER
) -> tuple[str, str]:
    """Record that orders `a` and `b` were one decision. Idempotent."""
    if str(a) == str(b):
        raise JournalError("an order cannot be linked to itself")
    pair = _pair(a, b)
    conn.execute(
        "INSERT OR IGNORE INTO campaign_links"
        " (broker, order_id, joins_order_id, created_at) VALUES (?, ?, ?, ?)",
        (broker, *pair, _now()),
    )
    conn.commit()
    return pair


def unlink(
    conn: sqlite3.Connection, a: str, b: str, *, broker: str = DEFAULT_BROKER
) -> bool:
    """Remove a link. True if a row went."""
    cur = conn.execute(
        "DELETE FROM campaign_links"
        " WHERE broker = ? AND order_id = ? AND joins_order_id = ?",
        (broker, *_pair(a, b)),
    )
    conn.commit()
    return bool(cur.rowcount)
