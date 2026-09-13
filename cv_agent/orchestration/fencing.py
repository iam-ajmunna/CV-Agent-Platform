"""cv_agent.orchestration.fencing — Session ownership and CAS fencing epoch manager.

Implements ADR-0003 §3.3 and PM Gate 2:
- Protects session mutation via ExecutionLease with monotonically increasing fencing_epoch.
- Enforces compare-and-swap renewal.
- Provides context manager for scoped session ownership.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timezone

from cv_agent.orchestration.contracts import ExecutionLease, OrchestrationStore
from cv_agent.orchestration.store import StaleFencingLeaseError


class LeaseManager:
    """Manages session lease acquisition, renewal, and fencing verification."""

    def __init__(self, store: OrchestrationStore) -> None:
        self.store = store

    def acquire(
        self, session_id: str, thread_id: str, owner_instance_id: str
    ) -> ExecutionLease:
        """Acquire or adopt a session execution lease."""
        return self.store.acquire_lease(session_id, thread_id, owner_instance_id)

    def renew(self, lease: ExecutionLease) -> ExecutionLease:
        """Renew an execution lease using atomic compare-and-swap (CAS)."""
        return self.store.renew_lease(lease)

    def release(self, lease: ExecutionLease) -> None:
        """Release an execution lease."""
        self.store.release_lease(lease)

    def verify_lease(self, lease: ExecutionLease) -> None:
        """Verify that a lease is not locally expired and has a valid epoch."""
        now = datetime.now(timezone.utc)
        if lease.expires_at < now:
            raise StaleFencingLeaseError(
                "LEASE_EXPIRED",
                f"Lease {lease.lease_id!r} for session {lease.session_id!r} expired at {lease.expires_at.isoformat()}.",
            )
        if lease.fencing_epoch < 1:
            raise StaleFencingLeaseError(
                "INVALID_FENCING_EPOCH",
                f"Lease fencing epoch must be >= 1, got {lease.fencing_epoch}.",
            )

    @contextmanager
    def session_scope(
        self,
        session_id: str,
        thread_id: str,
        owner_instance_id: str,
    ) -> Iterator[ExecutionLease]:
        """Context manager guaranteeing acquisition and safe release of an ExecutionLease."""
        lease = self.acquire(session_id, thread_id, owner_instance_id)
        try:
            yield lease
        finally:
            with suppress(Exception):
                self.release(lease)
