"""Keep a Google calendar equal to a set of events, deterministically.

This is a general engine with a specific history. Its first caller scraped nine
university event sites and paid a model session every day to decide what
belonged on a calendar; the judgment turned out to be unnecessary (the user
wanted every event) and the sync turned out to be a diff. What was left, once
the university came out of it, is the part worth keeping: **given a set of
events you want on a calendar, make the calendar say that, without trampling
anything a person did by hand.**

That is a general problem. Anything that produces a list of dated things --
a scraped programme, a fixture list, events pulled out of mail, a rota someone
publishes as JSON -- wants exactly this and gets it wrong in exactly the same
ways.

The rules, each of which is a bug someone had first:

- **Identity is stamped, not inferred.** Every event Herald writes carries
  `extendedProperties.private.syncKey` and a `syncHash` of its fields, so the
  next pass recognises its own work rather than creating a second copy of it.
- **Adopt rather than duplicate.** An event with the right title at the right
  time that nobody owns is stamped and taken over -- because it is almost
  always the same event, added by hand or by a previous version of the tool.
  Duplicating it is the single most visible way a calendar sync fails.
- **A hand-edited event is the person's.** If they changed the summary or the
  time, the sync refreshes only location and link and keeps everything else.
- **Nothing is deleted for being over.** An event the source now calls
  cancelled, or that has vanished from a source known to publish its whole
  list, is retitled and greyed. Google Calendar has no trash.
- **Only a complete source can say "gone".** A paginated or partial feed omits
  things for reasons that have nothing to do with cancellation, so vanishing
  only counts for the sources the caller names as complete.
- **Bulk changes are a bug, not a mandate.** A pass that wants to cancel or
  delete more than the caller's cap is refused wholesale and reported. A real
  source never drops half a calendar at once; a stale or mis-keyed snapshot
  does, and one did -- 78 live events greyed in four minutes on 9 September
  2026, before this cap existed.
- **Deletion is narrow on purpose.** `purge` removes an event only if it is
  clean, Herald-written, and carries one of the caller's own key prefixes; a
  hand-edited or foreign event is left alone and reported.

`plan()` is pure and tested. `apply()` does the writing, through `gwrite`, so
every change lands in the `actions` table and the next digest can report it.
"""

from __future__ import annotations

import datetime as dt

from . import gwrite

#: op names `plan` emits, in the order they are worth reading
OPS = ("create", "update", "merge", "stamp", "cancel", "purge", "unchanged")

#: Safety caps. See the module docstring: a pass that wants more than this has
#: bad input, not a big job.
MAX_CANCELS_PER_PASS = 15
MAX_PURGES_PER_PASS = 60

_COUNT_NAMES = {"create": "created", "update": "updated", "merge": "merged",
                "stamp": "adopted", "cancel": "cancelled", "purge": "removed"}


def plan(live: list[dict], want: dict, cancelled: set[str],
         complete_sources: set[str], today: dt.date,
         ignored: dict | None = None) -> list[tuple]:
    """The list of `(op, key, live_event_or_None, body_or_None, note)`.

    `live` is `gwrite.calendar_window(...)`; `want` maps syncKey -> event body
    (with `_aliases`, the other keys the caller's de-duplicator considers the
    same event); `cancelled` are keys the source now says are cancelled;
    `complete_sources` are the key prefixes whose feeds publish their whole
    list, so an absence from them means something; `ignored` maps a key to why
    it is unwanted, and produces a purge rather than a skip.
    """
    ignored = ignored or {}
    by_key = {e["syncKey"]: e for e in live if e["syncKey"]}
    unowned = [e for e in live if not e["syncKey"] or e["syncKey"] not in want]
    used: set[str] = set()
    ops: list[tuple] = []

    for key, body in want.items():
        existing, how = by_key.get(key), "key"
        if not existing:
            for alt in body.get("_aliases", ()):
                if alt in by_key and by_key[alt]["id"] not in used:
                    existing, how = by_key[alt], "rekey"
                    break
        if not existing:
            start = body["start"].get("dateTime") or body["start"].get("date")
            nt = gwrite.norm_title(body["summary"])
            for e in unowned:
                if e["id"] in used:
                    continue
                if (gwrite.norm_title(e["summary"].removeprefix(gwrite.CANCEL_PREFIX)) == nt
                        and gwrite.same_start(e["start"], start)):
                    existing, how = e, ("rekey" if e["syncKey"] else "adopt")
                    break
        if not existing:
            ops.append(("create", key, None, body, ""))
            continue
        used.add(existing["id"])
        if existing["user_modified"]:
            ops.append(("merge", key, existing, body, how))
            continue
        bs = body["start"].get("dateTime") or body["start"].get("date")
        be = body["end"].get("dateTime") or body["end"].get("date")
        same = (existing["summary"] == body["summary"]
                and gwrite.same_start(existing["start"], bs)
                and gwrite.same_start(existing["end"], be)
                and existing["location"] == body["location"]
                and (existing["colorId"] or "") == (body.get("colorId") or "")
                and existing["description"][:600] == body["description"][:600])
        if same and how == "key":
            ops.append(("unchanged", key, existing, None, ""))
        elif same:
            # Right in every visible way but not stamped as ours: take
            # ownership without touching what the reader sees.
            ops.append(("stamp", key, existing, body, how))
        else:
            ops.append(("update", key, existing, body, how))

    for e in live:
        if e["id"] in used or not e["syncKey"]:
            continue
        key = e["syncKey"]
        reason = ignored.get(key)
        if reason:
            ops.append(("purge", key, e, None, reason))
            continue
        src = key.split(":", 1)[0]
        explicit = key in cancelled
        # The fact behind a live event can have aged out of the caller's
        # window, so this reads the source off the event Herald itself wrote.
        vanished = (src in complete_sources and key not in want
                    and (e["start"] or "")[:10] > today.isoformat())
        if explicit or vanished:
            if e["summary"].startswith(gwrite.CANCEL_PREFIX):
                ops.append(("unchanged", key, e, None, "already cancelled"))
            else:
                ops.append(("cancel", key, e, None,
                            "source says cancelled" if explicit else "gone from source"))
    return ops


def guard(ops: list[tuple], *, max_cancels: int = MAX_CANCELS_PER_PASS,
          max_purges: int = MAX_PURGES_PER_PASS,
          report=None) -> list[tuple]:
    """Drop bulk destruction from a plan that has clearly gone wrong."""
    out = ops
    for op, cap, why in (("cancel", max_cancels, "the snapshot is probably wrong"),
                         ("purge", max_purges, "the filter is probably wrong")):
        planned = [o for o in out if o[0] == op]
        if len(planned) > cap:
            if report:
                report(f"refusing to {op} {len(planned)} events in one pass "
                       f"(limit {cap}); {why}")
            out = [o for o in out if o[0] != op]
    return out


def describe(ops: list[tuple]) -> list[str]:
    """One line per op, for a dry run or an audit."""
    lines = []
    for op, key, ex, body, note in ops:
        if op == "unchanged":
            continue
        title = (body or ex)["summary"]
        when = ((body["start"].get("dateTime") or body["start"].get("date"))
                if body else ex["start"])
        lines.append(f"{op:9} {(when or '')[:16]:17} {title[:60]:60} {key} {note}")
    return lines


def apply(con, ops: list[tuple], *, actor: str, cal_id: str, cal_name: str,
          cancelled_color: str | None = None,
          purgeable_prefixes: tuple[str, ...] = (),
          dry_run: bool = False, report=None) -> dict:
    """Carry out a plan. Every write goes through gwrite, so every write is
    logged to the `actions` table and reported by the next digest."""
    counts = {"created": 0, "updated": 0, "merged": 0, "adopted": 0,
              "cancelled": 0, "removed": 0, "kept back": 0, "unchanged": 0}
    for op, key, ex, body, note in ops:
        if op == "unchanged":
            counts["unchanged"] += 1
            continue
        if body is not None:
            body = {k: v for k, v in body.items() if not k.startswith("_")}
        if dry_run:
            counts[_COUNT_NAMES[op]] += 1
            continue
        if op == "create":
            gwrite.calendar_insert(con, actor=actor, cal_id=cal_id,
                                   cal_name=cal_name, body=body)
            counts["created"] += 1
        elif op == "update":
            gwrite.calendar_update(con, actor=actor, cal_id=cal_id,
                                   cal_name=cal_name, event_id=ex["id"], body=body)
            counts["updated"] += 1
            if note in ("adopt", "rekey"):
                counts["adopted"] += 1
        elif op == "merge":
            patch = gwrite.merge_for_update(ex, body)
            gwrite.calendar_patch(
                con, actor=actor, cal_id=cal_id, cal_name=cal_name,
                event_id=ex["id"], patch=patch,
                what=f"refreshed location/link on '{ex['summary']}' (kept your edits)")
            counts["merged"] += 1
        elif op == "stamp":
            gwrite.calendar_patch(con, actor=actor, cal_id=cal_id, cal_name=cal_name,
                                  event_id=ex["id"],
                                  patch={"extendedProperties": body["extendedProperties"]},
                                  what="", quiet=True)
            counts["adopted"] += 1
        elif op == "cancel":
            gwrite.calendar_cancel(con, actor=actor, cal_id=cal_id, cal_name=cal_name,
                                   existing=ex, sync_key=key, color_id=cancelled_color)
            counts["cancelled"] += 1
        elif op == "purge":
            gone = gwrite.calendar_delete_synced(
                con, actor=actor, cal_id=cal_id, cal_name=cal_name,
                event=ex, reason=note, key_prefixes=purgeable_prefixes)
            if gone:
                counts["removed"] += 1
            else:
                # Hand-edited, or written by something other than this sync.
                # Deleting it is not this caller's call; say so and move on.
                counts["kept back"] += 1
                if report:
                    report(f"left '{ex['summary'][:50]}' ({note}) alone: "
                           f"not a clean event of ours")
    return counts
