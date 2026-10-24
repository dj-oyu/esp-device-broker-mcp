"""Single-use continuation tickets and the device-hold reservation they enable.

A ticket only orders and reserves: it never queues, approves or runs anything. Every
job is still queued by its own explicit request. Records hold the ticket id, the parent
job, a state and the principal that owns the parent; everything else (position, budget,
state) is derived from job rows when asked. Tickets are evaluated lazily from job state
by the broker's maintenance tick, so no job exit path can skip the void/hold decision.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .store import Store

# Crockford base32 without I, L, O, U: short enough to relay, unambiguous to read.
TICKET_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
TICKET_LENGTH = 8
HOLD_SECONDS = 60.0
CHAIN_BUDGET_SECONDS = 1800.0
TICKET_TTL_SECONDS = 1800.0
MAX_ISSUED_TICKETS = 256
TERMINAL_FAILURES = ("failed", "cancelled", "needs_review", "interrupted")


class TicketError(ValueError):
    """Fixed public reason; never carries a ticket, job id or request content."""


@dataclass
class Hold:
    """Scheduling reservation of the single device slot; USB stays closed."""

    ticket_id: str
    principal_id: str
    deadline: float


class Tickets:
    def __init__(
        self,
        store: Callable[[], Store],
        epoch: Callable[[], str],
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._epoch = epoch
        self._wall = wall

    # -- persistence --------------------------------------------------------------

    def _row(self, ticket_id: str) -> dict[str, Any] | None:
        row = (
            self._store()
            .db.execute("SELECT * FROM continuation_tickets WHERE ticket_id=?", (ticket_id,))
            .fetchone()
        )
        return dict(row) if row else None

    def issue(
        self,
        parent_job_id: str,
        principal_id: str,
        *,
        total_hint: int | None = None,
    ) -> str:
        """Issue one ticket for ``parent_job_id`` (idempotent for the same parent)."""
        db = self._store().db
        existing = db.execute(
            "SELECT ticket_id FROM continuation_tickets WHERE parent_job_id=?",
            (parent_job_id,),
        ).fetchone()
        if existing is not None:
            return str(existing[0])
        issued = db.execute(
            "SELECT COUNT(*) FROM continuation_tickets WHERE state='issued'"
        ).fetchone()[0]
        if issued >= MAX_ISSUED_TICKETS:
            raise TicketError("ticket_limit")
        previous = db.execute(
            "SELECT chain_started, chain_total FROM continuation_tickets WHERE consumed_by_job=?",
            (parent_job_id,),
        ).fetchone()
        now = self._wall()
        started = float(previous["chain_started"]) if previous is not None else now
        total = (
            int(previous["chain_total"])
            if previous is not None and previous["chain_total"] is not None
            else total_hint
        )
        for _ in range(16):
            ticket_id = "".join(secrets.choice(TICKET_ALPHABET) for _ in range(TICKET_LENGTH))
            if self._row(ticket_id) is None:
                break
        else:  # pragma: no cover - 32**8 space
            raise TicketError("ticket_limit")
        db.execute(
            "INSERT INTO continuation_tickets(ticket_id,parent_job_id,principal_id,epoch,state,"
            "chain_started,chain_total,created) VALUES(?,?,?,?,'issued',?,?,?)",
            (ticket_id, parent_job_id, principal_id, self._epoch(), started, total, now),
        )
        db.commit()
        return ticket_id

    def mark_state(self, ticket_id: str, state: str, reason: str) -> None:
        db = self._store().db
        db.execute(
            "UPDATE continuation_tickets SET state=?, reason=? "
            "WHERE ticket_id=? AND state='issued'",
            (state, reason, ticket_id),
        )
        db.commit()

    def set_fence(self, ticket_id: str, seq: int) -> None:
        db = self._store().db
        db.execute(
            "UPDATE continuation_tickets SET fence_seq=? WHERE ticket_id=? AND fence_seq IS NULL",
            (seq, ticket_id),
        )
        db.commit()

    def set_hold(self, ticket_id: str, value: str) -> None:
        db = self._store().db
        db.execute("UPDATE continuation_tickets SET hold=? WHERE ticket_id=?", (value, ticket_id))
        db.commit()

    def peek(self, ticket_id: str, principal_id: str) -> dict[str, Any]:
        """Validate a ticket for its owner without spending it."""
        row = self._row(ticket_id)
        if row is None or row["principal_id"] != principal_id:
            raise TicketError("ticket_invalid")
        if row["state"] == "consumed":
            raise TicketError("ticket_consumed")
        if row["state"] == "expired":
            raise TicketError("hold_expired")
        if row["state"] != "issued" or row["epoch"] != self._epoch():
            raise TicketError("ticket_void")
        return row

    def consume(self, ticket_id: str, principal_id: str) -> dict[str, Any]:
        """Atomically spend an issued ticket. Unknown and foreign tickets look the same."""
        row = self.peek(ticket_id, principal_id)
        db = self._store().db
        db.execute(
            "UPDATE continuation_tickets SET state='consumed' WHERE ticket_id=? AND state='issued'",
            (ticket_id,),
        )
        db.commit()
        return row

    def note_consumer(self, ticket_id: str, job_id: str) -> None:
        db = self._store().db
        db.execute(
            "UPDATE continuation_tickets SET consumed_by_job=? "
            "WHERE ticket_id=? AND consumed_by_job IS NULL",
            (job_id, ticket_id),
        )
        db.commit()

    def issued(self) -> list[dict[str, Any]]:
        rows = self._store().db.execute(
            "SELECT * FROM continuation_tickets WHERE state='issued' ORDER BY created"
        )
        return [dict(row) for row in rows]

    def void_stale(self) -> None:
        """A restart voids every unspent ticket: holds are never restored."""
        db = self._store().db
        db.execute(
            "UPDATE continuation_tickets SET state='void', reason='broker_restart' "
            "WHERE state='issued' AND epoch<>?",
            (self._epoch(),),
        )
        db.commit()

    def void_for_job(self, job_id: str, reason: str) -> None:
        db = self._store().db
        db.execute(
            "UPDATE continuation_tickets SET state='void', reason=? "
            "WHERE parent_job_id=? AND state='issued'",
            (reason, job_id),
        )
        db.commit()

    # -- derived views ------------------------------------------------------------

    def row_for_parent(self, job_id: str) -> dict[str, Any] | None:
        row = (
            self._store()
            .db.execute("SELECT * FROM continuation_tickets WHERE parent_job_id=?", (job_id,))
            .fetchone()
        )
        return dict(row) if row else None

    def _previous(self, job_id: str) -> dict[str, Any] | None:
        row = (
            self._store()
            .db.execute("SELECT * FROM continuation_tickets WHERE consumed_by_job=?", (job_id,))
            .fetchone()
        )
        return dict(row) if row else None

    def chain_jobs(self, job_id: str) -> tuple[list[str], list[str], dict[str, Any] | None]:
        """(jobs before, jobs after, ticket the last known job issued)."""
        before: list[str] = []
        current = job_id
        for _ in range(MAX_ISSUED_TICKETS):
            previous = self._previous(current)
            if previous is None:
                break
            current = str(previous["parent_job_id"])
            before.append(current)
        before.reverse()
        after: list[str] = []
        current = job_id
        last: dict[str, Any] | None = None
        for _ in range(MAX_ISSUED_TICKETS):
            last = self.row_for_parent(current)
            if last is None or last["consumed_by_job"] is None:
                break
            current = str(last["consumed_by_job"])
            after.append(current)
            last = None
        return before, after, last

    def budget_remaining(self, ticket: dict[str, Any] | None) -> float | None:
        if ticket is None:
            return None
        return max(0.0, CHAIN_BUDGET_SECONDS - (self._wall() - float(ticket["chain_started"])))

    @staticmethod
    def finished_epoch(job: dict[str, Any]) -> float | None:
        """Wall time a job finished (rows store UTC CURRENT_TIMESTAMP text)."""
        finished = job.get("finished")
        if not isinstance(finished, str):
            return None
        try:
            return datetime.strptime(finished, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()
        except ValueError:
            return None


MAX_PENDING_DEFERRED = 64


class Deferred:
    """Jobs the owner already requested, waiting for their parent job to finish.

    A waiting job is not in the job table and never holds a queue position. When the
    parent succeeds the broker prepares and queues the exact plan the owner committed
    to; when it fails the waiting job is cancelled with a fixed reason code.
    """

    def __init__(self, store: Callable[[], Store], epoch: Callable[[], str]) -> None:
        self._store = store
        self._epoch = epoch

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = (
            self._store()
            .db.execute("SELECT * FROM deferred_jobs WHERE job_id=?", (job_id,))
            .fetchone()
        )
        return dict(row) if row else None

    def pending(self) -> list[dict[str, Any]]:
        rows = self._store().db.execute(
            "SELECT * FROM deferred_jobs WHERE state='pending' ORDER BY created"
        )
        return [dict(row) for row in rows]

    def add(
        self,
        job_id: str,
        plan_hash: str,
        parent_job_id: str,
        principal_id: str,
        origin: str,
        ticket_id: str | None = None,
    ) -> None:
        db = self._store().db
        count = db.execute("SELECT COUNT(*) FROM deferred_jobs WHERE state='pending'").fetchone()[0]
        if count >= MAX_PENDING_DEFERRED:
            raise TicketError("queue_full")
        db.execute(
            "INSERT INTO deferred_jobs(job_id,plan_hash,parent_job_id,principal_id,origin,epoch,"
            "ticket_id,state,created) VALUES(?,?,?,?,?,?,?,'pending',?)",
            (
                job_id,
                plan_hash,
                parent_job_id,
                principal_id,
                origin,
                self._epoch(),
                ticket_id,
                time.time(),
            ),
        )
        db.commit()

    def set_wait(self, job_id: str, wait_id: str) -> None:
        db = self._store().db
        db.execute("UPDATE deferred_jobs SET wait_id=? WHERE job_id=?", (wait_id, job_id))
        db.commit()

    def remove(self, job_id: str) -> None:
        db = self._store().db
        db.execute("DELETE FROM deferred_jobs WHERE job_id=? AND state='pending'", (job_id,))
        db.commit()

    def finish(
        self,
        job_id: str,
        state: str,
        reason: str | None = None,
        resolved_plan_hash: str | None = None,
    ) -> None:
        db = self._store().db
        db.execute(
            "UPDATE deferred_jobs SET state=?, reason=?, resolved_plan_hash=? "
            "WHERE job_id=? AND state='pending'",
            (state, reason, resolved_plan_hash, job_id),
        )
        db.commit()

    def cancel_stale(self) -> None:
        """A restart cancels every waiting job; nothing is resumed automatically."""
        db = self._store().db
        db.execute(
            "UPDATE deferred_jobs SET state='cancelled', reason='broker_restart' "
            "WHERE state='pending' AND epoch<>?",
            (self._epoch(),),
        )
        db.commit()

    def view(self, row: dict[str, Any]) -> dict[str, Any]:
        """Job-shaped record for a waiting or finished deferred job."""
        state = (
            ("waiting_for_device" if row.get("wait_id") else "waiting_parent")
            if row["state"] == "pending"
            else str(row["state"])
        )
        job: dict[str, Any] = {
            "id": row["job_id"],
            "plan_hash": row["plan_hash"],
            "state": state,
            "stage": None,
            "result": row["reason"],
            "diagnostic": None,
            "reviewed": 0,
            "created": None,
            "finished": None,
            "continues": {"parent_job_id": row["parent_job_id"]},
        }
        if row["resolved_plan_hash"]:
            job["resolved_plan_hash"] = row["resolved_plan_hash"]
        return job
