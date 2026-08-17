#!/usr/bin/env python3
"""Are the staged demo beats still stageable? — run this before every rehearsal.

    make demo-check

Five properties of the seeded world make the three moments possible. None of them
is decoration and none is guaranteed to survive use, because the system really
does learn: every rehearsal moves posteriors, and a few rehearsals can retire the
very playbook the rollback beat depends on. That is Chronicler working correctly
and the demo quietly losing its best moment, which is exactly the failure this
script exists to catch before a camera is pointed at it.

    a  a challenger that can win     an active playbook with zero trials, so its
                                     flat prior gives Thompson sampling something
                                     to surprise the incumbent with
    b  a bad-fix lane                a playbook that makes things worse, still
                                     selectable, still inside the retrieval radius
    c  a merge-ready pair            two active siblings within 0.15 cosine, both
                                     above 0.5, neither an ancestor of the other
    d  a promotion candidate         a playbook one success from the institutional
                                     threshold
    e  a visible false alarm         at least one, in the backtest or the history

A failure here is not a bug report. It almost always means `make demo-reset`.
"""
from __future__ import annotations

from _env import bootstrap, require_dsn

bootstrap()

from nexus_common import db, posterior  # noqa: E402

MERGE_DISTANCE = 0.15
MERGE_MIN_MEAN = 0.5
PROMOTION_MEAN = 0.9
PROMOTION_TRIALS = 10
RETIREMENT_MEAN = 0.2
MAX_DISTANCE = 0.35   # Sentinel's retrieval radius


def say(msg: str = "") -> None:
    print(msg, flush=True)


class Check:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def that(self, condition: bool, claim: str, detail: str = "") -> bool:
        ok = bool(condition)
        self.rows.append((ok, claim, detail))
        say(f"   {'PASS' if ok else 'FAIL'}  {claim}" + (f"   [{detail}]" if detail else ""))
        return ok

    @property
    def failures(self):
        return [r for r in self.rows if not r[0]]


def beats(conn) -> dict:
    """Everything the five checks need, in one pass over the memory."""
    out: dict = {}

    out["challengers"] = conn.execute(
        """
        SELECT name, outcome_category FROM playbooks
        WHERE status = 'active' AND success_count = 0 AND failure_count = 0
        ORDER BY created_at DESC
        """
    ).fetchall()

    out["bad_fix"] = conn.execute(
        """
        SELECT name, success_count, failure_count, status FROM playbooks
        WHERE name = 'Scale out under regression'
        """
    ).fetchone()

    # The bad fix has to be retrievable as well as alive: Sentinel only considers
    # playbooks inside `MAX_DISTANCE` of the prediction, and one parked outside
    # that radius can never lose on camera however bad it is. The distance that
    # matters is to a *precursor window* of its category — that is what a live
    # prediction's embedding looks like — not to a sibling playbook.
    out["bad_fix_distance"] = conn.execute(
        """
        SELECT min(pb.precursor_embedding <=> ps.trajectory_embedding)
        FROM playbooks pb, precursor_snapshots ps
        WHERE pb.name = 'Scale out under regression'
          AND ps.outcome_category = pb.outcome_category
          AND ps.led_to_incident
        """
    ).fetchone()

    # A merge-ready pair: same category, both active, close, both healthy, and
    # unrelated. The lineage condition is what stops a parent and its own variant
    # counting as convergence.
    out["merge_pairs"] = conn.execute(
        f"""
        SELECT a.name, b.name, a.precursor_embedding <=> b.precursor_embedding AS d,
               a.success_count, a.failure_count, b.success_count, b.failure_count
        FROM playbooks a JOIN playbooks b
          ON a.outcome_category = b.outcome_category AND a.id < b.id
        WHERE a.status = 'active' AND b.status = 'active'
          AND a.precursor_embedding <=> b.precursor_embedding < {MERGE_DISTANCE}
          AND a.id <> ALL(b.lineage) AND b.id <> ALL(a.lineage)
        ORDER BY d
        LIMIT 5
        """
    ).fetchall()

    out["promotion"] = conn.execute(
        """
        SELECT name, success_count, failure_count, memory_tier FROM playbooks
        WHERE status = 'active' AND memory_tier <> 'institutional'
          AND success_count + failure_count >= 8
        ORDER BY (success_count + 1.0) / (success_count + failure_count + 2.0) DESC
        LIMIT 3
        """
    ).fetchall()

    out["false_alarms_history"] = conn.execute(
        "SELECT count(*) FROM predictions WHERE prevention_status = 'false_alarm'"
    ).fetchone()[0]
    out["backtest"] = conn.execute(
        "SELECT false_positive, sample_size, method FROM backtest_runs "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    out["snapshots"] = conn.execute("SELECT count(*) FROM precursor_snapshots").fetchone()[0]
    out["playbooks"] = conn.execute(
        "SELECT count(*) FROM playbooks WHERE status = 'active'").fetchone()[0]
    return out


def main() -> int:
    require_dsn()
    facts = db.tx_retry(beats)
    check = Check()

    say(f"\n{'─' * 74}\ndemo world · {facts['snapshots']} snapshots, "
        f"{facts['playbooks']} active playbooks\n{'─' * 74}")

    say("\na · a challenger that can win")
    names = [f"{n} ({c})" for n, c in facts["challengers"]]
    for n in names[:3]:
        say(f"     {n}")
    check.that(bool(names), "an active playbook has zero trials, so its prior is flat")

    say("\nb · the bad-fix lane")
    bad = facts["bad_fix"]
    if bad is None:
        check.that(False, "the bad-fix playbook exists", "not found — run `make demo-reset`")
    else:
        name, s, f, status = bad
        mean = posterior.mean(s, f)
        say(f"     {name}: {s}/{f}, posterior {mean:.3f}, {status}")
        check.that(status == "active",
                   "it is still selectable", f"status {status}")
        check.that(mean >= RETIREMENT_MEAN,
                   "it is still above the retirement line",
                   f"{mean:.3f} vs {RETIREMENT_MEAN}")
        headroom = 0
        while posterior.mean(s, f + headroom + 1) >= RETIREMENT_MEAN:
            headroom += 1
        check.that(headroom >= 2,
                   "it can survive at least two more rollbacks before retiring",
                   f"{headroom} failures of headroom")
        distance = (facts["bad_fix_distance"] or [None])[0]
        check.that(distance is not None and float(distance) < MAX_DISTANCE,
                   "it sits inside Sentinel's retrieval radius, so it can be chosen",
                   f"{float(distance):.3f} from the nearest real precursor "
                   f"(radius {MAX_DISTANCE})" if distance else "—")

    say("\nc · a merge-ready pair")
    ready = [
        p for p in facts["merge_pairs"]
        if posterior.mean(p[3], p[4]) > MERGE_MIN_MEAN
        and posterior.mean(p[5], p[6]) > MERGE_MIN_MEAN
    ]
    for a, b, d, *_ in ready[:3]:
        say(f"     {a} ↔ {b}   distance {float(d):.3f}")
    check.that(bool(ready),
               "two unrelated active siblings are inside the merge predicate",
               f"{len(ready)} pair(s)")

    say("\nd · a promotion candidate")
    close = []
    for name, s, f, tier in facts["promotion"]:
        mean, trials = posterior.mean(s, f), s + f
        after = posterior.mean(s + 1, f)
        say(f"     {name} [{tier}]: {s}/{f} → {mean:.3f}, one success → {after:.3f}")
        if after > PROMOTION_MEAN and trials + 1 >= PROMOTION_TRIALS:
            close.append(name)
    check.that(bool(close),
               "a playbook is one success from the institutional threshold",
               ", ".join(close) or "none within one trial")

    say("\ne · a visible false alarm")
    bt = facts["backtest"]
    if bt:
        fp, n, method = bt
        say(f"     backtest ({method}): {fp} false positive(s) over {n} windows")
    say(f"     history: {facts['false_alarms_history']} prediction(s) resolved as false_alarm")
    check.that((bt and bt[0] > 0) or facts["false_alarms_history"] > 0,
               "failure is visible somewhere a judge will look",
               "backtest false positives" if bt and bt[0] else "prediction history")

    say(f"\n{'─' * 74}")
    passed = len(check.rows) - len(check.failures)
    say(f"   {passed} of {len(check.rows)} beats stageable")
    if check.failures:
        say("\n   Run `make demo-reset` to restore the seeded world, then re-check.")
        for _, claim, detail in check.failures:
            say(f"     · {claim}" + (f"  [{detail}]" if detail else ""))
        return 1
    say("   Every beat is stageable. `make demo-run`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
