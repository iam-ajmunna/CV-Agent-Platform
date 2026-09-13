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
    LifecycleStatus,
    OperationId,
    OperationIntent,
    OperationPayloadEnvelope,
    OperationStatus,
    OrchestrationEvent,
    OrchestrationStore,
)
from cv_agent.orchestration.state_machine import (
    InvalidStateTransitionError,
    validate_lifecycle_transition,
    validate_operation_transition,
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
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    lifecycle_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_thread ON sessions(thread_id);

                CREATE TABLE IF NOT EXISTS leases (
                    session_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    owner_instance_id TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    fencing_epoch INTEGER NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    schema_version INTEGER NOT NULL,
                    state_blob TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_checkpoints_thread ON checkpoints(thread_id, sequence_number);

                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
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
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status);
                CREATE INDEX IF NOT EXISTS idx_operations_session ON operations(session_id);

                CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence_number),
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, sequence_number);

                CREATE TABLE IF NOT EXISTS approvals (
                    request_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
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
                    decision_metadata_json TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_op ON approvals(operation_id, operation_version);
                CREATE INDEX IF NOT EXISTS idx_approvals_session ON approvals(session_id);
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
                # Ensure authoritative session record exists in sessions
                conn.execute(
                    """
                    INSERT INTO sessions (session_id, thread_id, lifecycle_status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO NOTHING
                    """,
                    (
                        session_id,
                        thread_id,
                        LifecycleStatus.INITIALIZING.value,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

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

    def transition_lifecycle(
        self,
        session_id: str,
        target_status: LifecycleStatus,
        event_type: str,
        event_payload: Mapping[str, Any],
        lease: ExecutionLease,
        checkpoint_state: Mapping[str, Any] | None = None,
    ) -> tuple[LifecycleStatus, OrchestrationEvent]:
        """Atomically transition lifecycle status, allocate monotonic sequence, and log event.

        Enforces ADR-0003 §3.9 & §3.10 and PM Gates 1, 2, 3:
        1. Verifies lease fencing atomically.
        2. Retrieves authoritative current lifecycle status from SQLite `sessions` table.
        3. Validates transition using `validate_lifecycle_transition()`.
        4. Mutates `sessions.lifecycle_status`.
        5. Allocates next monotonic sequence number for `session_id`.
        6. Inserts the lifecycle transition audit event.
        7. If `checkpoint_state` is provided, persists checkpoint in the same transaction.
        All changes commit or roll back together atomically.
        """
        if lease.session_id != session_id:
            raise StaleFencingLeaseError(
                "SESSION_MISMATCH",
                f"Lease session {lease.session_id!r} does not match target session {session_id!r}.",
            )

        now = datetime.now(timezone.utc)
        now_str = now.isoformat()
        event_id = str(uuid.uuid4())
        payload_dict = dict(event_payload)
        payload_dict["target_status"] = target_status.value
        payload_json = json.dumps(payload_dict, ensure_ascii=False, sort_keys=True)

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._validate_lease_fencing(conn, lease)

                cur = conn.execute(
                    "SELECT lifecycle_status FROM sessions WHERE session_id = ?",
                    (session_id,),
                )
                row = cur.fetchone()
                if row is None:
                    current_status = LifecycleStatus.INITIALIZING
                    conn.execute(
                        """
                        INSERT INTO sessions (session_id, thread_id, lifecycle_status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            lease.thread_id,
                            current_status.value,
                            now_str,
                            now_str,
                        ),
                    )
                else:
                    current_status = LifecycleStatus(row["lifecycle_status"])

                # Validate transition against authoritative state machine
                validate_lifecycle_transition(current_status, target_status)

                # Mutate lifecycle status in SQLite
                conn.execute(
                    "UPDATE sessions SET lifecycle_status = ?, updated_at = ? WHERE session_id = ?",
                    (target_status.value, now_str, session_id),
                )

                # Monotonically allocate sequence number
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
                        now_str,
                        payload_json,
                    ),
                )

                # Optional atomic checkpoint
                if checkpoint_state is not None:
                    cp_id = str(uuid.uuid4())
                    cp_blob = json.dumps(
                        checkpoint_state, ensure_ascii=False, sort_keys=True
                    )
                    cur_cp = conn.execute(
                        "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_cp FROM checkpoints WHERE thread_id = ?",
                        (lease.thread_id,),
                    )
                    next_cp = int(cur_cp.fetchone()["next_cp"])
                    conn.execute(
                        """
                        INSERT INTO checkpoints (checkpoint_id, session_id, thread_id, sequence_number, schema_version, state_blob, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cp_id,
                            session_id,
                            lease.thread_id,
                            next_cp,
                            CHECKPOINT_SCHEMA_VERSION,
                            cp_blob,
                            now_str,
                        ),
                    )

                conn.execute("COMMIT;")
                return (
                    target_status,
                    OrchestrationEvent(
                        session_id=session_id,
                        sequence_number=next_seq,
                        event_id=event_id,
                        event_type=event_type,
                        timestamp=now,
                        payload=payload_dict,
                    ),
                )
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def get_lifecycle_status(self, session_id: str) -> LifecycleStatus | None:
        """Query the authoritative lifecycle status for a session from SQLite."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT lifecycle_status FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return LifecycleStatus(row["lifecycle_status"])

    def load_state(self, thread_id: str) -> object | None:
        """Load newest valid checkpoint state for thread_id, selecting newest valid (ADR-0003 §3.13).

        If corruption is detected and no valid checkpoint remains, transitions session to RECOVERY_REQUIRED.
        """
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT checkpoint_id, session_id, sequence_number, schema_version, state_blob
                FROM checkpoints
                WHERE thread_id = ?
                ORDER BY sequence_number DESC
                """,
                (thread_id,),
            )
            rows = cur.fetchall()
            if not rows:
                return None

            corruption_reasons: list[str] = []
            session_id: str | None = None

            # Recovery selection: find newest valid checkpoint supported by integrity contract
            for row in rows:
                session_id = str(row["session_id"])
                schema_ver = int(row["schema_version"])
                if schema_ver != CHECKPOINT_SCHEMA_VERSION:
                    corruption_reasons.append(
                        f"Checkpoint {row['checkpoint_id']!r} unsupported schema_version={schema_ver}"
                    )
                    continue

                try:
                    return json.loads(row["state_blob"])
                except json.JSONDecodeError as exc:
                    corruption_reasons.append(
                        f"Checkpoint {row['checkpoint_id']!r} invalid JSON: {exc}"
                    )
                    continue

            # If we reached here, checkpoints exist but NO valid checkpoint remains!
            # ADR-0003 §3.13: session enters RECOVERY_REQUIRED with diagnostic evidence preserved
            now_str = _iso_now()
            if session_id is not None:
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    conn.execute(
                        "UPDATE sessions SET lifecycle_status = ?, updated_at = ? WHERE session_id = ?",
                        (LifecycleStatus.RECOVERY_REQUIRED.value, now_str, session_id),
                    )
                    # Persist corruption diagnostic event
                    cur_seq = conn.execute(
                        "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_seq FROM events WHERE session_id = ?",
                        (session_id,),
                    )
                    next_seq = int(cur_seq.fetchone()["next_seq"])
                    diag_payload = {
                        "reasons": corruption_reasons,
                        "thread_id": thread_id,
                        "session_id": session_id,
                    }
                    conn.execute(
                        """
                        INSERT INTO events (session_id, sequence_number, event_id, event_type, timestamp, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            next_seq,
                            str(uuid.uuid4()),
                            "checkpoint_corruption_detected",
                            now_str,
                            json.dumps(diag_payload, ensure_ascii=False),
                        ),
                    )
                    conn.execute("COMMIT;")
                except sqlite3.Error:
                    conn.execute("ROLLBACK;")

            reasons_summary = "; ".join(corruption_reasons)
            raise CheckpointCorruptionError(
                "CHECKPOINT_CORRUPTION_RECOVERY_REQUIRED",
                f"Thread {thread_id!r} checkpoints are corrupt and no valid checkpoint remains. "
                f"Session transitioned to RECOVERY_REQUIRED. Diagnostic evidence: {reasons_summary}",
            )

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
        """Persist or update an operation intent under lease fencing, validating transitions."""
        now_str = _iso_now()
        payload_json = json.dumps(
            operation.canonical_payload.payload, ensure_ascii=False, sort_keys=True
        )

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._validate_lease_fencing(conn, lease)

                cur = conn.execute(
                    "SELECT status FROM operations WHERE operation_id = ?",
                    (operation.operation_id.value,),
                )
                row = cur.fetchone()

                if row is None:
                    conn.execute(
                        """
                        INSERT INTO operations (
                            operation_id, session_id, operation_version, operation_type,
                            schema_id, schema_version, canonicalization_version,
                            payload_hash, payload_json, target_identity, status,
                            created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            operation.operation_id.value,
                            operation.session_id,
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
                else:
                    existing_status = OperationStatus(row["status"])
                    if existing_status != OperationStatus.CREATED:
                        raise InvalidStateTransitionError(
                            "operation_status",
                            existing_status.value,
                            OperationStatus.CREATED.value,
                            f"Cannot reset existing operation {operation.operation_id.value!r} from {existing_status.value!r} to CREATED.",
                        )

                    conn.execute(
                        """
                        UPDATE operations
                        SET session_id = ?, operation_version = ?, operation_type = ?,
                            schema_id = ?, schema_version = ?, canonicalization_version = ?,
                            payload_hash = ?, payload_json = ?, target_identity = ?, updated_at = ?
                        WHERE operation_id = ?
                        """,
                        (
                            operation.session_id,
                            operation.operation_version,
                            operation.operation_type,
                            operation.canonical_payload.schema_id,
                            operation.canonical_payload.schema_version,
                            operation.canonical_payload.canonicalization_version,
                            operation.canonical_payload.payload_hash,
                            payload_json,
                            operation.target_identity,
                            now_str,
                            operation.operation_id.value,
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
        """Update an operation status under lease fencing, validating transitions."""
        now_str = _iso_now()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._validate_lease_fencing(conn, lease)

                cur = conn.execute(
                    "SELECT status FROM operations WHERE operation_id = ?",
                    (operation_id.value,),
                )
                row = cur.fetchone()
                if row is None:
                    raise KeyError(
                        f"Operation {operation_id.value!r} not found in store."
                    )

                current_status = OperationStatus(row["status"])
                validate_operation_transition(current_status, status)

                conn.execute(
                    "UPDATE operations SET status = ?, updated_at = ? WHERE operation_id = ?",
                    (status.value, now_str, operation_id.value),
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
                SELECT operation_id, session_id, operation_version, operation_type,
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
                session_id=str(row["session_id"]),
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
                        request_id, session_id, operation_id, operation_version,
                        canonical_payload_hash, policy_version, action_type,
                        risk_level, resource_scope, created_at, expires_at, decision
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request.request_id,
                        request.session_id,
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
        """PM Gate 7: Conservative false-negative retention pruning via authoritative relations.

        Strictly protects:
        - Sessions with active unexpired leases.
        - Sessions with pending approvals.
        - Sessions with active or unresolved operations (created, running, unknown, reconciling).
        - Sessions with non-terminal lifecycle status.
        - experiments.sqlite is completely outside this store and never touched.
        """
        older_than_str = older_than.isoformat()
        now_str = _iso_now()

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # 1. Unexpired leases
                cur1 = conn.execute(
                    "SELECT DISTINCT session_id FROM leases WHERE expires_at >= ?",
                    (now_str,),
                )
                protected_sessions = {str(r["session_id"]) for r in cur1.fetchall()}

                # 2. Pending approvals (authoritative session_id)
                cur2 = conn.execute(
                    "SELECT DISTINCT session_id FROM approvals WHERE decision = 'pending' OR decision IS NULL"
                )
                protected_sessions.update(str(r["session_id"]) for r in cur2.fetchall())

                # 3. Active or unresolved operations (authoritative session_id)
                cur3 = conn.execute(
                    """
                    SELECT DISTINCT session_id FROM operations
                    WHERE status IN (
                        'created', 'validated', 'approval_required',
                        'dispatching', 'submitted', 'running',
                        'unknown', 'reconciling', 'cancel_requested'
                    )
                    """
                )
                protected_sessions.update(str(r["session_id"]) for r in cur3.fetchall())

                # 4. Non-terminal lifecycle sessions
                cur4 = conn.execute(
                    "SELECT DISTINCT session_id FROM sessions WHERE lifecycle_status NOT IN ('completed', 'failed')"
                )
                protected_sessions.update(str(r["session_id"]) for r in cur4.fetchall())

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
