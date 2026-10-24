"""The device lease: the only writer of `Broker.session` and of its deadline.

`docs/redesign/contracts/jobs.md` section 3. The broker grants a lease in three places
(an immediate `acquire`, a FIFO grant for a work-session waiter and a job-only lease
for a pre-requested job) and ends it in exactly one (`Broker._end_ownership`). All of
them now go through this object, and so do the three deadline writes (revocation
shortening twice, `renew` once).

The rule "never release device ownership while owned processes or device work remain
active" lives in `end()`: it refuses while the `physically_busy` callback reports busy,
before any release side effect runs.

Re-entrancy is unchanged on purpose. `Broker._grant_job_lease` grants a lease and then
calls `prepare`/`execute`, whose `_lease()` runs `_expire()` and so a nested
`maintenance_tick`. That nested tick sees the new lease, so it never grants another one,
and it keeps the same timing (revocation/expiry checks right after the grant). Removing
it would move those checks to the next 0.25 s tick, which is a behaviour change.

Lease expiry and revocation are different things. Reaching `deadline` ends an *idle*
lease and stops a queued job from being admitted, but a job that was already admitted
and is running keeps its lease (and device ownership) until it finishes
(DESIGN-CONTINUATION-TICKETS.md section 5.3: "実行中のジョブは、リースの期限が来ても所有を
保つ"). Revocation (principal no longer valid, scope lost) is recorded with
`revoke()`, which also sets the sticky `revoked` flag, so the running-job checks can
still stop the job. Neither is persisted; a lease is process-local.

Every method is synchronous: a caller's check and the write share one loop step.
"""

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class Session:
    capability: str = field(repr=False)
    id: str
    owner: str
    deadline: float
    principal_id: str | None = None
    work_session_id: str | None = None
    request_id: str | None = None
    chain_ticket: str | None = None
    job_id: str | None = None
    # Set only by `LeaseManager.revoke`; never cleared. A running job checks this
    # instead of the clock (see the module docstring).
    revoked: bool = False


class ReleaseRefused(Exception):
    """Ownership cannot end while physical device work or owned processes are active."""


class LeaseManager:
    def __init__(self, physically_busy: Callable[[], bool]) -> None:
        self._physically_busy = physically_busy
        self._current: Session | None = None

    @property
    def current(self) -> Session | None:
        return self._current

    def grant(self, lease: Session) -> Session:
        """Make `lease` the device owner. Callers have already checked the slot is free."""
        self._current = lease
        return lease

    def end(self, finalize: Callable[[Session], None]) -> Session | None:
        """Release the current lease; the only path that clears it.

        Returns None when there is no lease. Raises `ReleaseRefused` while physically
        busy, before `finalize` runs. `finalize` performs the caller's release side
        effects (cancel intents, write the request row) while the lease is still current;
        if it raises, the lease stays.
        """
        session = self._current
        if session is None:
            return None
        if self._physically_busy():
            raise ReleaseRefused
        finalize(session)
        self._current = None
        return session

    @staticmethod
    def shorten(session: Session, until: float) -> None:
        """Never extend: revocation moves the deadline to `until` if that is earlier."""
        session.deadline = min(session.deadline, until)

    @staticmethod
    def revoke(session: Session, until: float) -> None:
        """Authority was withdrawn: shorten like `shorten` and mark the lease revoked.

        The flag is sticky, so a later successful authentication cannot revive the
        lease for the job that was running under it.
        """
        session.revoked = True
        session.deadline = min(session.deadline, until)

    @staticmethod
    def renew(session: Session, until: float, cap: float | None = None) -> None:
        """Replace the deadline (it may move earlier), capped by the work-session deadline."""
        session.deadline = until if cap is None else min(until, cap)

    def overwrite(self, session: Session | None) -> None:
        """Test seam behind the `Broker.session` setter. Production code never calls it."""
        self._current = session
