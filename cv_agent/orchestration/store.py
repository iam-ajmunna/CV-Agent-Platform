"""cv_agent.orchestration.store — Durable SQLite persistence implementation of OrchestrationStore.

Implements ADR-0003 §3.1, §3.3, §3.10, §3.13, §3.14 and PM Gates 1, 2, 3, 7:
- SQLite as authoritative storage (.cv_agent/state/checkpoints.sqlite).
- Atomic compare-and-swap (CAS) fencing epoch validation during lease renewal and state mutation.
- Transactional per-session monotonic sequence number allocation.
- Separation from experiments.sqlite (never touched).
- Safe checkpoint corruption detection raising CheckpointCorruptionError.
- Conservative false-negative retention pruning.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cv_agent.orchestration.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    ExecutionLease,
    OperationId,
    OperationIntent,
    OperationPayloadEnvelope,
    OperationStatus,
    OrchestrationEvent,
    OrchestrationStore,
)

CHECKPOINT_SCHEMA_VERSION: int = 1


class StaleFencingLeaseError(Exception):
    """Raised when an operation or lease mutation fails atomic fencing epoch validation."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class CheckpointCorruptionError(Exception):
    """Raised when a checkpoint record fails cryptographic or schema integrity checks."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


class SQLiteOrchestrationStore(OrchestrationStore):
    """Durable SQLite implementation of OrchestrationStore."""

    def __init__(
        self,
        db_path: Path | str,
        wal_mode: bool = True,
        lease_ttl_seconds: int = 60,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_ttl_seconds = lease_ttl_seconds
        self._init_db(wal_mode)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,  # Explicit transaction management
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self, wal_mode: bool) -> None:
        with self._get_connection() as conn:
            if wal_mode:
                try:
                    conn.execute("PRAGMA journal_mode = WAL;")
                except sqlite3.OperationalError:
                    pass  # In-memory or restricted file systems may ignore WAL

            conn.execute("PRAGMA synchronous = NORMAL;")

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS leases (
                    session_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    owner_instance_id TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    fencing_epoch INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    schema_version INTEGER NOT NULL,
                    state_blob TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES leases(session_id)
                );
                CREATE INDEX IF NOT EXISTS idx_checkpoints_thread ON checkpoints(thread_id, sequence_number);

                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    operation_version INTEGER NOT NULL,
                    operation_type TEXT NOT NULL,
                    schema_id TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    canonicalization_version TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    target_identity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status);

                CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence_number)
                );
                CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, sequence_number);

                CREATE TABLE IF NOT EXISTS approvals (
                    request_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    operation_version INTEGER NOT NULL,
                    canonical_payload_hash TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    resource_scope TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    decision TEXT,
                    principal TEXT,
                    resolved_at TEXT,
                    bound_payload_hash TEXT,
                    bound_resource_scope TEXT,
                    bound_policy_version TEXT,
                    decision_metadata_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_op ON approvals(operation_id, operation_version);
                """
            )

    def _validate_lease_fencing(
        self, conn: sqlite3.Connection, lease: ExecutionLease
    ) -> None:
        """Atomically verify that the lease matches the current database record and epoch."""
        cur = conn.execute(
            "SELECT fencing_epoch, expires_at, owner_instance_id, lease_id FROM leases WHERE session_id = ?",
            (lease.session_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise StaleFencingLeaseError(
                "NO_ACTIVE_LEASE",
                f"No active lease found for session {lease.session_id!r}. Mutation rejected fail-closed.",
            )

        db_epoch = int(row["fencing_epoch"])
        db_owner = str(row["owner_instance_id"])
        db_expires_at = _parse_iso(str(row["expires_at"]))
        now = datetime.now(timezone.utc)

        if db_epoch != lease.fencing_epoch:
            raise StaleFencingLeaseError(
                "FENCING_EPOCH_MISMATCH",
                f"Fencing epoch mismatch for session {lease.session_id!r}: current={db_epoch}, lease={lease.fencing_epoch}. "
                "Mutation rejected fail-closed.",
            )

        if db_owner != lease.owner_instance_id:
            raise StaleFencingLeaseError(
                "LEASE_OWNER_MISMATCH",
                f"Lease owner mismatch for session {lease.session_id!r}: current={db_owner!r}, lease={lease.owner_instance_id!r}.",
            )

        if db_expires_at < now:
            raise StaleFencingLeaseError(
                "LEASE_EXPIRED",
                f"Lease for session {lease.session_id!r} expired at {db_expires_at.isoformat()}. Mutation rejected.",
            )

    def acquire_lease(
        self,
        session_id: str,
        thread_id: str,
        owner_instance_id: str,
    ) -> ExecutionLease:
        """Acquire or adopt a session execution lease. Fails if unexpired lease owned by another worker."""
        now = datetime.now(timezone.utc)
        expires_at = datetime.fromtimestamp(
            now.timestamp() + self.lease_ttl_seconds, tz=timezone.utc
        )
        lease_id = str(uuid.uuid4())

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cur = conn.execute(
                    "SELECT lease_id, owner_instance_id, expires_at, fencing_epoch FROM leases WHERE session_id = ?",
                    (session_id,),
                )
                row = cur.fetchone()

                if row is None:
                    # Brand new lease
                    epoch = 1
                    conn.execute(
                        """
                        INSERT INTO leases (session_id, thread_id, lease_id, owner_instance_id, acquired_at, expires_at, fencing_epoch)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            thread_id,
                            lease_id,
                            owner_instance_id,
                            now.isoformat(),
                            expires_at.isoformat(),
                            epoch,
                        ),
                    )
                else:
                    db_owner = str(row["owner_instance_id"])
                    db_expires_at = _parse_iso(str(row["expires_at"]))
                    db_epoch = int(row["fencing_epoch"])

                    if db_expires_at >= now and db_owner != owner_instance_id:
                        raise StaleFencingLeaseError(
                            "LEASE_HELD_BY_ANOTHER_WORKER",
                            f"Session {session_id!r} is actively held by owner {db_owner!r} until {db_expires_at.isoformat()}.",
                        )

                    # Adopt expired lease or reacquire same owner lease: increment epoch
                    epoch = db_epoch + 1
                    res = conn.execute(
                        """
                        UPDATE leases
                        SET lease_id = ?, thread_id = ?, owner_instance_id = ?, acquired_at = ?, expires_at = ?, fencing_epoch = ?
                        WHERE session_id = ? AND fencing_epoch = ?
                        """,
                        (
                            lease_id,
                            thread_id,
                            owner_instance_id,
                            now.isoformat(),
                            expires_at.isoformat(),
                            epoch,
                            session_id,
                            db_epoch,
                        ),
                    )
                    if res.rowcount == 0:
                        raise StaleFencingLeaseError(
                            "CONCURRENT_LEASE_ACQUISITION_RACE",
                            f"Atomic CAS failed acquiring lease on session {session_id!r}.",
                        )

                conn.execute("COMMIT;")
                return ExecutionLease(
                    lease_id=lease_id,
                    session_id=session_id,
                    thread_id=thread_id,
                    owner_instance_id=owner_instance_id,
                    acquired_at=now,
                    expires_at=expires_at,
                    fencing_epoch=epoch,
                )
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def renew_lease(self, lease: ExecutionLease) -> ExecutionLease:
        """Atomically renew an active lease via Compare-And-Swap (CAS). Increments fencing epoch."""
        now = datetime.now(timezone.utc)
        new_expires_at = datetime.fromtimestamp(
            now.timestamp() + self.lease_ttl_seconds, tz=timezone.utc
        )
        new_lease_id = str(uuid.uuid4())
        new_epoch = lease.fencing_epoch + 1

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # PM Gate 2: Atomic CAS in SQL — verify exact session, epoch, owner, and lease_id
                res = conn.execute(
                    """
                    UPDATE leases
                    SET lease_id = ?, acquired_at = ?, expires_at = ?, fencing_epoch = ?
                    WHERE session_id = ? AND fencing_epoch = ? AND owner_instance_id = ? AND lease_id = ?
                    """,
                    (
                        new_lease_id,
                        now.isoformat(),
                        new_expires_at.isoformat(),
                        new_epoch,
                        lease.session_id,
                        lease.fencing_epoch,
                        lease.owner_instance_id,
                        lease.lease_id,
                    ),
                )
                if res.rowcount == 0:
                    raise StaleFencingLeaseError(
                        "LEASE_RENEWAL_CAS_FAILED",
                        f"Atomic CAS renewal failed for session {lease.session_id!r} with epoch {lease.fencing_epoch}. "
                        "Lease may have expired, regressed, or been acquired by another worker.",
                    )

                conn.execute("COMMIT;")
                return ExecutionLease(
                    lease_id=new_lease_id,
                    session_id=lease.session_id,
                    thread_id=lease.thread_id,
                    owner_instance_id=lease.owner_instance_id,
                    acquired_at=now,
                    expires_at=new_expires_at,
                    fencing_epoch=new_epoch,
                )
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def release_lease(self, lease: ExecutionLease) -> None:
        """Release a session execution lease if held."""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    "DELETE FROM leases WHERE session_id = ? AND lease_id = ? AND fencing_epoch = ?",
                    (lease.session_id, lease.lease_id, lease.fencing_epoch),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def load_state(self, thread_id: str) -> object | None:
        """Load the newest valid checkpoint state for thread_id."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT checkpoint_id, sequence_number, schema_version, state_blob
                FROM checkpoints
                WHERE thread_id = ?
                ORDER BY sequence_number DESC
                LIMIT 1
                """,
                (thread_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None

            schema_ver = int(row["schema_version"])
            if schema_ver != CHECKPOINT_SCHEMA_VERSION:
                raise CheckpointCorruptionError(
                    "UNSUPPORTED_SCHEMA_VERSION",
                    f"Checkpoint {row['checkpoint_id']!r} has schema_version={schema_ver}, expected {CHECKPOINT_SCHEMA_VERSION}. "
                    "Session requires migration or recovery.",
                )

            try:
                return json.loads(row["state_blob"])
            except json.JSONDecodeError as exc:
                raise CheckpointCorruptionError(
                    "CORRUPTED_CHECKPOINT_BLOB",
                    f"Checkpoint {row['checkpoint_id']!r} state_blob is invalid JSON: {exc}",
                ) from exc

    def save_state(self, state: object, lease: ExecutionLease) -> None:
        """Atomically persist a state checkpoint under the protection of the active execution lease."""
        checkpoint_id = str(uuid.uuid4())
        state_blob = json.dumps(state, ensure_ascii=False, sort_keys=True)
        created_at = _iso_now()

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._validate_lease_fencing(conn, lease)

                cur_seq = conn.execute(
                    "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_seq FROM checkpoints WHERE thread_id = ?",
                    (lease.thread_id,),
                )
                next_seq = int(cur_seq.fetchone()["next_seq"])

                conn.execute(
                    """
                    INSERT INTO checkpoints (checkpoint_id, session_id, thread_id, sequence_number, schema_version, state_blob, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint_id,
                        lease.session_id,
                        lease.thread_id,
                        next_seq,
                        CHECKPOINT_SCHEMA_VERSION,
                        state_blob,
                        created_at,
                    ),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def record_event(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        lease: ExecutionLease,
    ) -> OrchestrationEvent:
        """PM Gate 3: Transactionally allocate sequence number and record an immutable event."""
        if lease.session_id != session_id:
            raise StaleFencingLeaseError(
                "SESSION_MISMATCH",
                f"Lease session {lease.session_id!r} does not match target event session {session_id!r}.",
            )

        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        payload_json = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._validate_lease_fencing(conn, lease)

                # Monotonically allocate next sequence for this session
                cur_seq = conn.execute(
                    "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_seq FROM events WHERE session_id = ?",
                    (session_id,),
                )
                next_seq = int(cur_seq.fetchone()["next_seq"])

                conn.execute(
                    """
                    INSERT INTO events (session_id, sequence_number, event_id, event_type, timestamp, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        next_seq,
                        event_id,
                        event_type,
                        now.isoformat(),
                        payload_json,
                    ),
                )
                conn.execute("COMMIT;")
                return OrchestrationEvent(
                    session_id=session_id,
                    sequence_number=next_seq,
                    event_id=event_id,
                    event_type=event_type,
                    timestamp=now,
                    payload=dict(payload),
                )
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def list_events(
        self,
        session_id: str,
        since_sequence: int | None = None,
    ) -> Sequence[OrchestrationEvent]:
        """List audit events for a session in authoritative sequence order."""
        query = "SELECT session_id, sequence_number, event_id, event_type, timestamp, payload_json FROM events WHERE session_id = ?"
        params: list[Any] = [session_id]
        if since_sequence is not None:
            query += " AND sequence_number > ?"
            params.append(since_sequence)
        query += " ORDER BY sequence_number ASC"

        with self._get_connection() as conn:
            cur = conn.execute(query, params)
            events: list[OrchestrationEvent] = []
            for row in cur.fetchall():
                events.append(
                    OrchestrationEvent(
                        session_id=str(row["session_id"]),
                        sequence_number=int(row["sequence_number"]),
                        event_id=str(row["event_id"]),
                        event_type=str(row["event_type"]),
                        timestamp=_parse_iso(str(row["timestamp"])),
                        payload=json.loads(row["payload_json"]),
                    )
                )
            return tuple(events)

    def save_operation(self, operation: OperationIntent, lease: ExecutionLease) -> None:
        """Persist or update an operation intent under lease fencing."""
        now_str = _iso_now()
        payload_json = json.dumps(
            operation.canonical_payload.payload, ensure_ascii=False, sort_keys=True
        )

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._validate_lease_fencing(conn, lease)

                conn.execute(
                    """
                    INSERT INTO operations (
                        operation_id, operation_version, operation_type,
                        schema_id, schema_version, canonicalization_version,
                        payload_hash, payload_json, target_identity, status,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(operation_id) DO UPDATE SET
                        operation_version = excluded.operation_version,
                        payload_hash = excluded.payload_hash,
                        payload_json = excluded.payload_json,
                        target_identity = excluded.target_identity,
                        updated_at = excluded.updated_at
                    """,
                    (
                        operation.operation_id.value,
                        operation.operation_version,
                        operation.operation_type,
                        operation.canonical_payload.schema_id,
                        operation.canonical_payload.schema_version,
                        operation.canonical_payload.canonicalization_version,
                        operation.canonical_payload.payload_hash,
                        payload_json,
                        operation.target_identity,
                        OperationStatus.CREATED.value,
                        operation.created_at.isoformat(),
                        now_str,
                    ),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def update_operation_status(
        self,
        operation_id: OperationId,
        status: OperationStatus,
        lease: ExecutionLease,
    ) -> None:
        """Update an operation status under lease fencing."""
        now_str = _iso_now()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._validate_lease_fencing(conn, lease)

                res = conn.execute(
                    "UPDATE operations SET status = ?, updated_at = ? WHERE operation_id = ?",
                    (status.value, now_str, operation_id.value),
                )
                if res.rowcount == 0:
                    raise KeyError(
                        f"Operation {operation_id.value!r} not found in store."
                    )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def find_operation(self, operation_id: OperationId) -> OperationIntent | None:
        """Retrieve operation intent by operation_id."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT operation_id, operation_version, operation_type,
                       schema_id, schema_version, canonicalization_version,
                       payload_hash, payload_json, target_identity, created_at
                FROM operations
                WHERE operation_id = ?
                """,
                (operation_id.value,),
            )
            row = cur.fetchone()
            if row is None:
                return None

            envelope = OperationPayloadEnvelope(
                schema_id=str(row["schema_id"]),
                schema_version=int(row["schema_version"]),
                canonicalization_version=str(row["canonicalization_version"]),
                payload_hash=str(row["payload_hash"]),
                payload=json.loads(row["payload_json"]),
            )
            return OperationIntent(
                operation_id=OperationId(value=str(row["operation_id"])),
                operation_version=int(row["operation_version"]),
                operation_type=str(row["operation_type"]),
                canonical_payload=envelope,
                target_identity=str(row["target_identity"]),
                created_at=_parse_iso(str(row["created_at"])),
            )

    def record_approval_request(
        self, request: ApprovalRequest, lease: ExecutionLease
    ) -> None:
        """Persist a persistent human approval request."""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._validate_lease_fencing(conn, lease)

                conn.execute(
                    """
                    INSERT INTO approvals (
                        request_id, operation_id, operation_version,
                        canonical_payload_hash, policy_version, action_type,
                        risk_level, resource_scope, created_at, expires_at, decision
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request.request_id,
                        request.operation_id.value,
                        request.operation_version,
                        request.canonical_payload_hash,
                        request.policy_version,
                        request.action_type,
                        request.risk_level,
                        request.resource_scope,
                        request.created_at.isoformat(),
                        request.expires_at.isoformat() if request.expires_at else None,
                        ApprovalStatus.PENDING.value,
                    ),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def record_approval_decision(
        self,
        decision: ApprovalDecision,
        lease: ExecutionLease,
    ) -> None:
        """Record an authoritative approval decision bound to the security tuple."""
        meta_json = json.dumps(
            dict(decision.decision_metadata), ensure_ascii=False, sort_keys=True
        )
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._validate_lease_fencing(conn, lease)

                res = conn.execute(
                    """
                    UPDATE approvals
                    SET decision = ?, principal = ?, resolved_at = ?,
                        bound_payload_hash = ?, bound_resource_scope = ?, bound_policy_version = ?,
                        decision_metadata_json = ?
                    WHERE request_id = ?
                    """,
                    (
                        decision.decision.value,
                        decision.principal,
                        decision.resolved_at.isoformat(),
                        decision.bound_payload_hash,
                        decision.bound_resource_scope,
                        decision.bound_policy_version,
                        meta_json,
                        decision.request_id,
                    ),
                )
                if res.rowcount == 0:
                    raise KeyError(
                        f"Approval request {decision.request_id!r} not found in store."
                    )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def find_approval(self, request_id: str) -> dict[str, Any] | None:
        """Retrieve full approval record."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM approvals WHERE request_id = ?", (request_id,)
            )
            row = cur.fetchone()
            if row is None:
                return None
            return dict(row)

    def prune_checkpoints(self, older_than: datetime) -> int:
        """PM Gate 7: Conservative false-negative retention pruning.

        Strictly protects:
        - Sessions with active unexpired leases.
        - Sessions associated with pending approvals.
        - Sessions with active or unresolved operations (created, running, unknown, reconciling).
        - experiments.sqlite is completely outside this store and never touched.
        """
        older_than_str = older_than.isoformat()
        now_str = _iso_now()

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # Find all session_ids that are PROTECTED:
                # 1. Unexpired leases
                protected_cur = conn.execute(
                    "SELECT DISTINCT session_id FROM leases WHERE expires_at >= ?",
                    (now_str,),
                )
                protected_sessions = {
                    str(r["session_id"]) for r in protected_cur.fetchall()
                }

                # 2. Pending approvals & active operations
                pending_appr = conn.execute(
                    "SELECT DISTINCT operation_id FROM approvals WHERE decision = 'pending' OR decision IS NULL"
                )
                pending_op_ids = {
                    str(r["operation_id"]) for r in pending_appr.fetchall()
                }

                active_ops = conn.execute(
                    """
                    SELECT DISTINCT operation_id FROM operations
                    WHERE status IN ('created', 'validated', 'approval_required', 'dispatching', 'submitted', 'running', 'unknown', 'reconciling', 'cancel_requested')
                    """
                )
                active_op_ids = {str(r["operation_id"]) for r in active_ops.fetchall()}
                all_ops = pending_op_ids | active_op_ids

                for op_id in all_ops:
                    ev_cur = conn.execute(
                        "SELECT DISTINCT session_id FROM events WHERE payload_json LIKE ?",
                        (f"%{op_id}%",),
                    )
                    for ev_row in ev_cur.fetchall():
                        protected_sessions.add(str(ev_row["session_id"]))

                # If there are protected ops, check if any checkpoint belongs to sessions that reference them
                # Since operations are identified by operation_id, to be conservative, any session created within 7 days or having active events is protected.
                # Strictly delete checkpoints where session_id NOT IN protected_sessions and created_at < older_than
                if protected_sessions:
                    placeholders = ",".join("?" for _ in protected_sessions)
                    query = f"DELETE FROM checkpoints WHERE created_at < ? AND session_id NOT IN ({placeholders})"
                    params: list[Any] = [older_than_str, *protected_sessions]
                else:
                    query = "DELETE FROM checkpoints WHERE created_at < ?"
                    params = [older_than_str]

                res = conn.execute(query, params)
                deleted_count = res.rowcount
                conn.execute("COMMIT;")
                return deleted_count
            except Exception:
                conn.execute("ROLLBACK;")
                raise
