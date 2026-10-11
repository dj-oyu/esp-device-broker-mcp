"""Bounded, non-authorizing view of the authenticated pending-plan snapshot."""

from __future__ import annotations

from dataclasses import dataclass

from .gui_approvals import PlanList

MAX_SNAPSHOT_AGE = 6.0


@dataclass(frozen=True)
class BadgeView:
    label: str
    notice: str
    new: bool = False


class PlanBadge:
    def __init__(self) -> None:
        self.listing: PlanList | None = None
        self.started_at = 0.0
        self.epoch = ""
        self.known: set[str] = set()
        self.unread: set[str] = set()

    def update(self, listing: PlanList | None, started_at: float, now: float) -> None:
        self.view(now)  # Expire old rows before recognizing a reappearance.
        self.listing = listing
        self.started_at = started_at
        if listing is None or now - started_at >= MAX_SNAPSHOT_AGE:
            self.listing = None
            return
        current = {row.plan_hash for row in listing.plans if row.expires_at > now}
        if listing.broker_epoch != self.epoch:
            self.known.clear()
            self.unread.clear()
        self.epoch = listing.broker_epoch
        self.unread = (self.unread & current) | (current - self.known)
        self.known = current

    def acknowledge(self, now: float) -> None:
        self.view(now)
        self.unread.clear()

    def view(self, now: float) -> BadgeView:
        listing = self.listing
        if listing is None or now - self.started_at >= MAX_SNAPSHOT_AGE:
            return BadgeView("Pending (?)", "Plan count unavailable; waiting for a fresh snapshot")
        current = {row.plan_hash for row in listing.plans if row.expires_at > now}
        self.known &= current
        self.unread &= current
        # A lease may be renewed remotely. Once a visible row reaches its local
        # deadline, ask for a fresh snapshot instead of guessing that it is gone.
        if len(current) != len(listing.plans):
            return BadgeView("Pending (?)", "Plan count changed; waiting for a fresh snapshot")
        count = f"{len(current)}{'+' if listing.has_more else ''}"
        new = bool(self.unread)
        return BadgeView(
            f"Pending ({count}){' NEW' if new else ''}",
            "New plan approval request in the timeline"
            if new
            else f"Pending plan approvals: {count}",
            new,
        )
