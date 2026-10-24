"""The single writer of job state at runtime.

Every runtime job-state change goes through `JobLedger.transition()`. Its legality is
enforced by SQLite: the UPDATE only matches rows whose current state is an allowed
from-state for the requested to-state, and the rowcount says whether it was written.
An illegal transition is refused and counted; it never changes a committed outcome.

Not covered here on purpose (already guarded in SQL, see the store): `finish_firmware`,
`finish_fixed_trial`, `consume_home_attempt`, `finish_home_recovery`, the legacy
`awaiting_approval` migration and `recover()`. Insertion (`add_job`,
`enqueue_requested`) always writes `queued`.

All methods are synchronous: a caller's check and this write share one loop step.
"""

import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from typing import Any

STATES = ("queued", "running", "succeeded", "failed", "cancelled", "needs_review")
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "needs_review"})

# to-state -> allowed from-states, taken from what the code writes today
# (docs/redesign/contracts/jobs.md section 1.3). `awaiting_approval` is legacy and is
# rewritten only by the startup migration and `Store.recover()`.
TRANSITIONS: Mapping[str, frozenset[str]] = {
    "running": frozenset({"queued"}),
    "succeeded": frozenset({"running"}),
    "failed": frozenset({"running"}),
    "cancelled": frozenset({"queued", "running"}),
    "needs_review": frozenset({"queued", "running"}),
}


def allowed(source: str, target: str) -> bool:
    return source in TRANSITIONS.get(target, frozenset())


class JobLedger:
    def __init__(
        self,
        db: sqlite3.Connection,
        on_written: Callable[[str, str], None] | None = None,
    ) -> None:
        self.db = db
        self.on_written = on_written
        # Observability for refused writes: fixed (from, to) keys only, no caller text.
        self.refusals: Counter[tuple[str, str]] = Counter()
        self.on_refused: Callable[[], None] | None = None

    def transition(
        self,
        job_id: str,
        to: str,
        result: str | None = None,
        diagnostic: dict[str, Any] | None = None,
        *,
        expect: Iterable[str] | None = None,
    ) -> bool:
        """Write `to` only if the row is in an allowed from-state; return whether it was.

        `expect` narrows the table's from-states for one call site; it can never widen
        them. Entering `running` clears result/diagnostic; a terminal state stamps
        `finished`.
        """
        sources = TRANSITIONS.get(to)
        if sources is None:
            raise ValueError("job_transition_target_unknown")
        if expect is not None:
            narrowed = frozenset(expect)
            if not narrowed or not narrowed <= sources:
                raise ValueError("job_transition_expectation_invalid")
            sources = narrowed
        ordered = sorted(sources)
        cursor = self.db.execute(
            f"""UPDATE jobs SET state=?, result=?, diagnostic=?,
            finished=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END
            WHERE id=? AND state IN ({",".join("?" * len(ordered))})""",
            (
                to,
                result,
                json.dumps(diagnostic) if diagnostic else None,
                to in TERMINAL,
                job_id,
                *ordered,
            ),
        )
        written = cursor.rowcount == 1
        self.db.commit()
        if written:
            if self.on_written is not None:
                self.on_written(job_id, to)
        else:
            self._refuse(job_id, to)
        return written

    def annotate(self, job_id: str, diagnostic_update: Mapping[str, Any]) -> bool:
        """Merge keys into a finished job's diagnostic; never changes state or result.

        Only a terminal row is annotated, and only if its state did not change between
        the read and the write. `finished` is left as committed.
        """
        row = self.db.execute("SELECT state,diagnostic FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row[0] not in TERMINAL:
            return False
        diagnostic = dict(json.loads(row[1]) if row[1] else {})
        diagnostic.update(diagnostic_update)
        cursor = self.db.execute(
            "UPDATE jobs SET diagnostic=? WHERE id=? AND state=?",
            (json.dumps(diagnostic) if diagnostic else None, job_id, row[0]),
        )
        written = cursor.rowcount == 1
        self.db.commit()
        return written

    def mark_reviewed(self, job_id: str) -> bool:
        """Acknowledge a needs_review row (offline operator); not a state transition."""
        cursor = self.db.execute(
            "UPDATE jobs SET reviewed=1 WHERE id=? AND state='needs_review'", (job_id,)
        )
        written = cursor.rowcount == 1
        self.db.commit()
        return written

    def _refuse(self, job_id: str, to: str) -> None:
        # Observability must never affect the committed job or the caller's flow.
        try:
            row = self.db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            source = "missing" if row is None else row[0] if row[0] in STATES else "other"
            self.refusals[(source, to)] += 1
            if self.on_refused is not None:
                self.on_refused()
        except Exception:
            pass
