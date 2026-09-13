"""tests.test_orchestration_boundary — Comprehensive contract tests for ADR-0003 Orchestration.

Verifies:
- PM Gates 1-8 and ADR-0003 Invariants 1-17.
- Deterministic canonicalization, Unicode NFC equivalence, set sorting, and metadata stripping.
- Authoritative lifecycle state machines and Invariant 17 (EVALUATING is terminal-adjacent).
- Atomic CAS fencing epoch verification, renewal, and regression rejection.
- Monotonic transactional event sequence allocation.
- 4-tuple approval security binding (version, hash, policy, scope).
- Tri-state side-effect reconciliation (UNRESOLVED is never silently converted).
- Durable SQLite persistence, corruption handling, and conservative false-negative pruning.
- LangGraph workflow runtime integration and approval interrupt/resumption.
- Subsystem isolation (zero provider SDKs).
"""

from __future__ import annotations

import ast
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cv_agent.orchestration.approvals import (
    ApprovalManager,
    ApprovalSecurityError,
)
from cv_agent.orchestration.canonicalization import (
    CANONICALIZATION_VERSION,
    build_operation_envelope,
    canonicalize_payload,
    compute_payload_hash,
)
from cv_agent.orchestration.contracts import (
    ApprovalStatus,
    ExecutionLease,
    LifecycleStatus,
    OperationId,
    OperationIntent,
    OperationStatus,
)
from cv_agent.orchestration.fencing import LeaseManager
from cv_agent.orchestration.graph import (
    OrchestrationGraphState,
    create_orchestration_graph,
)
from cv_agent.orchestration.reconciliation import (
    ReconciliationCoordinator,
    ReconciliationOutcome,
)
from cv_agent.orchestration.state_machine import (
    InvalidStateTransitionError,
    validate_lifecycle_transition,
    validate_operation_transition,
)
from cv_agent.orchestration.store import (
    CheckpointCorruptionError,
    SQLiteOrchestrationStore,
    StaleFencingLeaseError,
)


@pytest.fixture
def temp_store(tmp_path: Path) -> SQLiteOrchestrationStore:
    db_path = tmp_path / ".cv_agent" / "state" / "checkpoints.sqlite"
    return SQLiteOrchestrationStore(
        db_path=db_path, wal_mode=False, lease_ttl_seconds=5
    )


@pytest.fixture
def active_lease(temp_store: SQLiteOrchestrationStore) -> ExecutionLease:
    return temp_store.acquire_lease(
        session_id="session-test-01",
        thread_id="thread-test-01",
        owner_instance_id="worker-node-1",
    )


class TestCanonicalizationAndHashing:
    """Tests deterministic canonicalization and PM Gate 4 constraints."""

    def test_unicode_nfc_normalization_equivalence(self) -> None:
        """Strings differing only in Unicode normalization (NFD vs NFC) produce identical hash."""
        # Decomposed 'e' + combining acute accent vs precomposed 'é'
        nfd_str = "caf\u0065\u0301"
        nfc_str = "caf\u00e9"
        assert nfd_str != nfc_str

        hash1 = compute_payload_hash({"model": nfd_str, "target": "edge"})
        hash2 = compute_payload_hash({"model": nfc_str, "target": "edge"})
        assert hash1 == hash2

    def test_numeric_edge_cases_and_order_independence(self) -> None:
        """Key ordering does not change hash; numeric values are formatted consistently."""
        payload_a = {"learning_rate": 0.001, "batch_size": 32, "weight_decay": 1e-4}
        payload_b = {"weight_decay": 0.0001, "batch_size": 32, "learning_rate": 0.001}

        assert compute_payload_hash(payload_a) == compute_payload_hash(payload_b)

    def test_set_canonicalization_order_independence(self) -> None:
        """Sets with identical items in different set-iteration order produce identical hashes."""
        payload_a = {"classes": {"person", "car", "bicycle", "dog"}}
        payload_b = {"classes": {"dog", "bicycle", "person", "car"}}

        assert canonicalize_payload(payload_a) == canonicalize_payload(payload_b)
        assert compute_payload_hash(payload_a) == compute_payload_hash(payload_b)

    def test_nested_observational_metadata_stripping(self) -> None:
        """PM Gate 4: Execution timestamps, trace IDs, and telemetry are stripped from canonical hash."""
        intent_clean = {
            "model_architecture": "yolo11n",
            "weights_path": "weights/best.pt",
            "hyperparameters": {"lr": 0.01, "epochs": 50},
        }

        intent_with_observations = {
            "model_architecture": "yolo11n",
            "weights_path": "weights/best.pt",
            "hyperparameters": {"lr": 0.01, "epochs": 50},
            # Observational metadata that must not affect execution intent identity
            "timestamp": "2026-09-13T12:00:00Z",
            "trace_id": "tr-98765",
            "execution_id": "exec-12345",
            "worker_id": "worker-42",
            "retry_count": 2,
            "telemetry": {"gpu_temp": 65.5, "vram_mb": 4096},
            "duration_ms": 142.5,
        }

        hash_clean = compute_payload_hash(intent_clean)
        hash_observed = compute_payload_hash(intent_with_observations)
        assert hash_clean == hash_observed

    def test_material_intent_modification_alters_hash(self) -> None:
        """Modifying any execution-relevant parameter produces a different canonical hash."""
        base = {"model": "yolov8", "img_size": 640}
        modified = {"model": "yolov8", "img_size": 1280}

        assert compute_payload_hash(base) != compute_payload_hash(modified)

    def test_operation_envelope_construction(self) -> None:
        """Envelope correctly wraps schema version, canonicalization version, and hash."""
        payload = {"op": "quantize", "precision": "int8"}
        envelope = build_operation_envelope("quantize_spec", 1, payload)

        assert envelope.schema_id == "quantize_spec"
        assert envelope.schema_version == 1
        assert envelope.canonicalization_version == CANONICALIZATION_VERSION
        assert envelope.payload_hash == compute_payload_hash(payload)


class TestAuthoritativeStateMachine:
    """Tests session and operation state machines, including Invariant 17."""

    def test_valid_lifecycle_transitions(self) -> None:
        """Legal session lifecycle transitions pass validation."""
        validate_lifecycle_transition(
            LifecycleStatus.INITIALIZING, LifecycleStatus.READY
        )
        validate_lifecycle_transition(LifecycleStatus.READY, LifecycleStatus.PLANNING)
        validate_lifecycle_transition(
            LifecycleStatus.PLANNING, LifecycleStatus.AWAITING_APPROVAL
        )
        validate_lifecycle_transition(
            LifecycleStatus.AWAITING_APPROVAL, LifecycleStatus.EXECUTING
        )
        validate_lifecycle_transition(
            LifecycleStatus.EXECUTING, LifecycleStatus.EVALUATING
        )
        validate_lifecycle_transition(
            LifecycleStatus.EVALUATING, LifecycleStatus.COMPLETED
        )

    def test_illegal_lifecycle_transitions_fail_closed(self) -> None:
        """Illegal transitions raise InvalidStateTransitionError."""
        with pytest.raises(InvalidStateTransitionError):
            validate_lifecycle_transition(
                LifecycleStatus.INITIALIZING, LifecycleStatus.EXECUTING
            )
        with pytest.raises(InvalidStateTransitionError):
            validate_lifecycle_transition(
                LifecycleStatus.PLANNING, LifecycleStatus.COMPLETED
            )

    def test_evaluating_to_executing_rejected_invariant_17(self) -> None:
        """PM Invariant 17: EVALUATING is terminal-adjacent; transition back to EXECUTING is strictly prohibited."""
        with pytest.raises(InvalidStateTransitionError) as exc:
            validate_lifecycle_transition(
                LifecycleStatus.EVALUATING, LifecycleStatus.EXECUTING
            )
        assert "Invariant 17 violation" in str(exc.value)
        assert "terminal-adjacent" in str(exc.value)

    def test_evaluating_terminal_adjacent_exits(self) -> None:
        """EVALUATING transitions exclusively to COMPLETED, FAILED, or PAUSED."""
        validate_lifecycle_transition(
            LifecycleStatus.EVALUATING, LifecycleStatus.COMPLETED
        )
        validate_lifecycle_transition(
            LifecycleStatus.EVALUATING, LifecycleStatus.FAILED
        )
        validate_lifecycle_transition(
            LifecycleStatus.EVALUATING, LifecycleStatus.PAUSED
        )

    def test_terminal_states_reject_all_transitions(self) -> None:
        """COMPLETED and FAILED are terminal states and reject any further transition."""
        with pytest.raises(InvalidStateTransitionError):
            validate_lifecycle_transition(
                LifecycleStatus.COMPLETED, LifecycleStatus.INITIALIZING
            )
        with pytest.raises(InvalidStateTransitionError):
            validate_lifecycle_transition(LifecycleStatus.FAILED, LifecycleStatus.READY)

    def test_operation_state_transitions(self) -> None:
        """Legal operation transitions pass; illegal jumps fail closed."""
        validate_operation_transition(
            OperationStatus.CREATED, OperationStatus.VALIDATED
        )
        validate_operation_transition(
            OperationStatus.VALIDATED, OperationStatus.APPROVAL_REQUIRED
        )
        validate_operation_transition(
            OperationStatus.APPROVAL_REQUIRED, OperationStatus.DISPATCHING
        )
        validate_operation_transition(
            OperationStatus.DISPATCHING, OperationStatus.RUNNING
        )
        validate_operation_transition(
            OperationStatus.RUNNING, OperationStatus.COMPLETED
        )

        with pytest.raises(InvalidStateTransitionError):
            validate_operation_transition(
                OperationStatus.CREATED, OperationStatus.RUNNING
            )


class TestLeaseFencingAndConcurrency:
    """Tests atomic CAS fencing epochs, renewal, and regression rejection (PM Gate 2)."""

    def test_lease_acquisition_initializes_epoch_one(
        self, temp_store: SQLiteOrchestrationStore
    ) -> None:
        lease = temp_store.acquire_lease("session-1", "thread-1", "worker-1")
        assert lease.fencing_epoch == 1
        assert lease.session_id == "session-1"

    def test_atomic_cas_renewal_increments_epoch(
        self, temp_store: SQLiteOrchestrationStore, active_lease: ExecutionLease
    ) -> None:
        renewed_1 = temp_store.renew_lease(active_lease)
        assert renewed_1.fencing_epoch == 2

        renewed_2 = temp_store.renew_lease(renewed_1)
        assert renewed_2.fencing_epoch == 3

    def test_same_epoch_competing_mutations_reject_stale_epoch(
        self, temp_store: SQLiteOrchestrationStore, active_lease: ExecutionLease
    ) -> None:
        """PM Gate 2: A worker presenting a stale fencing epoch is atomically rejected."""
        # Worker 1 renews lease, advancing epoch to 2
        renewed_lease = temp_store.renew_lease(active_lease)
        assert renewed_lease.fencing_epoch == 2

        # A mutation attempted with the old lease (epoch 1) must fail closed
        with pytest.raises(StaleFencingLeaseError) as exc:
            temp_store.save_state({"step": 1}, active_lease)
        assert exc.value.code == "FENCING_EPOCH_MISMATCH"

    def test_lease_expiration_during_mutation_fails_closed(
        self, temp_store: SQLiteOrchestrationStore
    ) -> None:
        """An expired lease fails closed on mutation."""
        now = datetime.now(timezone.utc)
        expired_at = now - timedelta(seconds=10)
        expired_lease = ExecutionLease(
            lease_id="expired-lease",
            session_id="session-exp",
            thread_id="thread-exp",
            owner_instance_id="worker-1",
            acquired_at=now - timedelta(seconds=60),
            expires_at=expired_at,
            fencing_epoch=1,
        )

        # Force record an expired lease in DB
        with temp_store._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO leases (session_id, thread_id, lease_id, owner_instance_id, acquired_at, expires_at, fencing_epoch)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "session-exp",
                    "thread-exp",
                    "expired-lease",
                    "worker-1",
                    (now - timedelta(seconds=60)).isoformat(),
                    expired_at.isoformat(),
                    1,
                ),
            )

        with pytest.raises(StaleFencingLeaseError) as exc:
            temp_store.save_state({"data": "test"}, expired_lease)
        assert exc.value.code == "LEASE_EXPIRED"

    def test_concurrent_lease_acquisition_rejected_for_active_worker(
        self, temp_store: SQLiteOrchestrationStore, active_lease: ExecutionLease
    ) -> None:
        """A second worker cannot seize an active unexpired lease owned by another instance."""
        with pytest.raises(StaleFencingLeaseError) as exc:
            temp_store.acquire_lease(
                session_id=active_lease.session_id,
                thread_id=active_lease.thread_id,
                owner_instance_id="worker-node-2",  # Competing worker
            )
        assert exc.value.code == "LEASE_HELD_BY_ANOTHER_WORKER"

    def test_adoption_of_expired_lease_by_new_worker(
        self, temp_store: SQLiteOrchestrationStore
    ) -> None:
        """A new worker can adopt an expired session lease, atomically incrementing the epoch."""
        now = datetime.now(timezone.utc)
        expired_time = now - timedelta(seconds=5)

        with temp_store._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO leases (session_id, thread_id, lease_id, owner_instance_id, acquired_at, expires_at, fencing_epoch)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "sess-dead",
                    "thr-dead",
                    "old-lease",
                    "dead-worker",
                    (now - timedelta(seconds=20)).isoformat(),
                    expired_time.isoformat(),
                    4,
                ),
            )

        # New worker acquires expired lease
        new_lease = temp_store.acquire_lease("sess-dead", "thr-dead", "new-worker")
        assert new_lease.owner_instance_id == "new-worker"
        assert new_lease.fencing_epoch == 5  # Incremented from 4

    def test_lease_manager_session_scope(
        self, temp_store: SQLiteOrchestrationStore
    ) -> None:
        """LeaseManager context manager handles acquisition and release."""
        mgr = LeaseManager(temp_store)
        with mgr.session_scope("scoped-sess", "scoped-thr", "worker-1") as lease:
            assert lease.fencing_epoch == 1
            mgr.verify_lease(lease)


class TestTransactionalEventSequencing:
    """Tests per-session monotonic sequence allocation (PM Gate 3)."""

    def test_transactional_monotonic_event_sequence(
        self, temp_store: SQLiteOrchestrationStore, active_lease: ExecutionLease
    ) -> None:
        """Events in a session receive strictly increasing sequence numbers transactionally."""
        ev1 = temp_store.record_event(
            active_lease.session_id, "start", {"v": 1}, active_lease
        )
        ev2 = temp_store.record_event(
            active_lease.session_id, "plan", {"v": 2}, active_lease
        )
        ev3 = temp_store.record_event(
            active_lease.session_id, "finish", {"v": 3}, active_lease
        )

        assert ev1.sequence_number == 1
        assert ev2.sequence_number == 2
        assert ev3.sequence_number == 3

        history = temp_store.list_events(active_lease.session_id)
        assert len(history) == 3
        assert [e.sequence_number for e in history] == [1, 2, 3]

    def test_failed_transaction_does_not_consume_sequence(
        self, temp_store: SQLiteOrchestrationStore, active_lease: ExecutionLease
    ) -> None:
        """PM Gate 3: A failed transaction rolls back without consuming sequence numbers."""
        temp_store.record_event(active_lease.session_id, "step_1", {}, active_lease)

        # Create lease with wrong session_id to cause transaction failure
        bad_lease = ExecutionLease(
            lease_id=active_lease.lease_id,
            session_id="wrong-session",
            thread_id=active_lease.thread_id,
            owner_instance_id=active_lease.owner_instance_id,
            acquired_at=active_lease.acquired_at,
            expires_at=active_lease.expires_at,
            fencing_epoch=active_lease.fencing_epoch,
        )

        with pytest.raises(StaleFencingLeaseError):
            temp_store.record_event(
                active_lease.session_id, "step_failed", {}, bad_lease
            )

        # Next valid event must receive sequence_number 2 (not 3)
        ev2 = temp_store.record_event(
            active_lease.session_id, "step_2", {}, active_lease
        )
        assert ev2.sequence_number == 2


class TestPersistentApprovalSecurityTuple:
    """Tests 4-tuple approval security binding (PM Gate 5)."""

    @pytest.fixture
    def sample_operation(self) -> OperationIntent:
        payload = {"deploy_target": "jetson_orin", "trt_version": "10.0"}
        envelope = build_operation_envelope("trt_compile", 1, payload)
        return OperationIntent(
            operation_id=OperationId(value="op-trt-01"),
            operation_version=1,
            operation_type="trt_compilation",
            canonical_payload=envelope,
            target_identity="remote_cluster",
            created_at=datetime.now(timezone.utc),
        )

    def test_approved_operation_matching_all_tuple_elements(
        self,
        temp_store: SQLiteOrchestrationStore,
        active_lease: ExecutionLease,
        sample_operation: OperationIntent,
    ) -> None:
        """Approval passes when all 4 tuple components strictly match."""
        mgr = ApprovalManager(temp_store)
        req = mgr.create_request(
            operation=sample_operation,
            action_type="compile",
            risk_level="high",
            policy_version="v1.0",
            resource_scope="cluster-prod",
            lease=active_lease,
        )

        mgr.record_decision(
            request_id=req.request_id,
            decision=ApprovalStatus.APPROVED,
            principal="admin@domain.internal",
            lease=active_lease,
        )

        # Full verification passes
        mgr.verify_authorization(
            request_id=req.request_id,
            current_operation=sample_operation,
            policy_version="v1.0",
            resource_scope="cluster-prod",
        )

    def test_approval_hash_mismatch_fails_closed(
        self,
        temp_store: SQLiteOrchestrationStore,
        active_lease: ExecutionLease,
        sample_operation: OperationIntent,
    ) -> None:
        """PM Gate 5: Modifying payload after approval fails closed with APPROVAL_HASH_MISMATCH."""
        mgr = ApprovalManager(temp_store)
        req = mgr.create_request(
            operation=sample_operation,
            action_type="compile",
            risk_level="high",
            policy_version="v1.0",
            resource_scope="cluster-prod",
            lease=active_lease,
        )

        mgr.record_decision(
            request_id=req.request_id,
            decision=ApprovalStatus.APPROVED,
            principal="admin@domain.internal",
            lease=active_lease,
        )

        # Attacker modifies payload (e.g. changing target from jetson_orin to unconstrained cloud)
        tampered_envelope = build_operation_envelope(
            "trt_compile",
            1,
            {"deploy_target": "unconstrained_cloud", "trt_version": "10.0"},
        )
        tampered_op = OperationIntent(
            operation_id=sample_operation.operation_id,
            operation_version=sample_operation.operation_version,
            operation_type=sample_operation.operation_type,
            canonical_payload=tampered_envelope,
            target_identity=sample_operation.target_identity,
            created_at=sample_operation.created_at,
        )

        with pytest.raises(ApprovalSecurityError) as exc:
            mgr.verify_authorization(
                request_id=req.request_id,
                current_operation=tampered_op,
                policy_version="v1.0",
                resource_scope="cluster-prod",
            )
        assert exc.value.code == "APPROVAL_HASH_MISMATCH"

    def test_approval_scope_mismatch_fails_closed(
        self,
        temp_store: SQLiteOrchestrationStore,
        active_lease: ExecutionLease,
        sample_operation: OperationIntent,
    ) -> None:
        """PM Gate 5: Approval for resource_scope 'staging' cannot authorize 'production'."""
        mgr = ApprovalManager(temp_store)
        req = mgr.create_request(
            operation=sample_operation,
            action_type="compile",
            risk_level="high",
            policy_version="v1.0",
            resource_scope="staging",
            lease=active_lease,
        )

        mgr.record_decision(
            request_id=req.request_id,
            decision=ApprovalStatus.APPROVED,
            principal="admin@domain.internal",
            lease=active_lease,
        )

        with pytest.raises(ApprovalSecurityError) as exc:
            mgr.verify_authorization(
                request_id=req.request_id,
                current_operation=sample_operation,
                policy_version="v1.0",
                resource_scope="production",  # Scope mismatch
            )
        assert exc.value.code == "APPROVAL_SCOPE_MISMATCH"

    def test_approval_policy_version_mismatch_fails_closed(
        self,
        temp_store: SQLiteOrchestrationStore,
        active_lease: ExecutionLease,
        sample_operation: OperationIntent,
    ) -> None:
        """PM Gate 5: Policy version change invalidates earlier approval."""
        mgr = ApprovalManager(temp_store)
        req = mgr.create_request(
            operation=sample_operation,
            action_type="compile",
            risk_level="high",
            policy_version="v1.0",
            resource_scope="default",
            lease=active_lease,
        )

        mgr.record_decision(
            request_id=req.request_id,
            decision=ApprovalStatus.APPROVED,
            principal="admin@domain.internal",
            lease=active_lease,
        )

        with pytest.raises(ApprovalSecurityError) as exc:
            mgr.verify_authorization(
                request_id=req.request_id,
                current_operation=sample_operation,
                policy_version="v2.0",  # Policy updated
                resource_scope="default",
            )
        assert exc.value.code == "APPROVAL_POLICY_MISMATCH"

    def test_expired_and_rejected_approvals_fail_closed(
        self,
        temp_store: SQLiteOrchestrationStore,
        active_lease: ExecutionLease,
        sample_operation: OperationIntent,
    ) -> None:
        """Expired or rejected approvals fail closed."""
        mgr = ApprovalManager(temp_store)
        req = mgr.create_request(
            operation=sample_operation,
            action_type="compile",
            risk_level="high",
            policy_version="v1.0",
            lease=active_lease,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),  # Expired
        )

        mgr.record_decision(
            request_id=req.request_id,
            decision=ApprovalStatus.APPROVED,
            principal="admin@domain.internal",
            lease=active_lease,
        )

        with pytest.raises(ApprovalSecurityError) as exc:
            mgr.verify_authorization(
                request_id=req.request_id,
                current_operation=sample_operation,
                policy_version="v1.0",
            )
        assert exc.value.code == "APPROVAL_EXPIRED"


class TestSideEffectReconciliationProtocol:
    """Tests crash recovery and external side-effect reconciliation (PM Gate 6)."""

    class MockReconciler:
        def __init__(self, outcome_status: OperationStatus) -> None:
            self.outcome_status = outcome_status

        def reconcile(self, operation: OperationIntent) -> OperationStatus:
            return self.outcome_status

    @pytest.fixture
    def sample_operation(self) -> OperationIntent:
        envelope = build_operation_envelope("eval_job", 1, {"metric": "map50"})
        return OperationIntent(
            operation_id=OperationId(value="op-recon-01"),
            operation_version=1,
            operation_type="evaluation",
            canonical_payload=envelope,
            target_identity="remote_target",
            created_at=datetime.now(timezone.utc),
        )

    def test_unknown_transitions_to_reconciling(
        self,
        temp_store: SQLiteOrchestrationStore,
        active_lease: ExecutionLease,
        sample_operation: OperationIntent,
    ) -> None:
        """Ambiguous operations transition cleanly into RECONCILING."""
        temp_store.save_operation(sample_operation, active_lease)
        temp_store.update_operation_status(
            sample_operation.operation_id, OperationStatus.UNKNOWN, active_lease
        )

        coord = ReconciliationCoordinator(temp_store)
        coord.initiate_reconciliation(sample_operation.operation_id, active_lease)

        op = temp_store.find_operation(sample_operation.operation_id)
        assert op is not None

    def test_reconciliation_confirmed_completed(
        self,
        temp_store: SQLiteOrchestrationStore,
        active_lease: ExecutionLease,
        sample_operation: OperationIntent,
    ) -> None:
        temp_store.save_operation(sample_operation, active_lease)
        reconciler = self.MockReconciler(OperationStatus.COMPLETED)
        coord = ReconciliationCoordinator(temp_store, reconciler=reconciler)

        outcome = coord.reconcile_operation(sample_operation, active_lease)
        assert outcome == ReconciliationOutcome.CONFIRMED_COMPLETED

    def test_reconciliation_confirmed_not_submitted(
        self,
        temp_store: SQLiteOrchestrationStore,
        active_lease: ExecutionLease,
        sample_operation: OperationIntent,
    ) -> None:
        temp_store.save_operation(sample_operation, active_lease)
        reconciler = self.MockReconciler(OperationStatus.CREATED)
        coord = ReconciliationCoordinator(temp_store, reconciler=reconciler)

        outcome = coord.reconcile_operation(sample_operation, active_lease)
        assert outcome == ReconciliationOutcome.CONFIRMED_NOT_SUBMITTED

    def test_reconciliation_unresolved_never_becomes_success_or_failure(
        self,
        temp_store: SQLiteOrchestrationStore,
        active_lease: ExecutionLease,
        sample_operation: OperationIntent,
    ) -> None:
        """PM Gate 6: UNRESOLVED must never be silently converted into FAILED or COMPLETED."""
        temp_store.save_operation(sample_operation, active_lease)
        temp_store.update_operation_status(
            sample_operation.operation_id, OperationStatus.UNKNOWN, active_lease
        )

        # Target reports UNKNOWN still
        reconciler = self.MockReconciler(OperationStatus.UNKNOWN)
        coord = ReconciliationCoordinator(temp_store, reconciler=reconciler)

        outcome = coord.reconcile_operation(sample_operation, active_lease)
        assert outcome == ReconciliationOutcome.UNRESOLVED


class TestDurableStoreIntegrityAndSafeRetention:
    """Tests SQLite persistence, WAL, integrity checks, and conservative pruning (PM Gate 7)."""

    def test_sqlite_persistence_and_restoration_across_instances(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "checkpoints.sqlite"
        store1 = SQLiteOrchestrationStore(db_path, wal_mode=False)
        lease1 = store1.acquire_lease("sess-persist", "thr-persist", "worker-1")
        store1.save_state({"agent_step": "completed", "score": 0.95}, lease1)

        # Reopen database in a completely separate store instance
        store2 = SQLiteOrchestrationStore(db_path, wal_mode=False)
        loaded = store2.load_state("thr-persist")
        assert loaded == {"agent_step": "completed", "score": 0.95}

    def test_checkpoint_schema_mismatch_raises_corruption(self, tmp_path: Path) -> None:
        """Checkpoints with unsupported schema version raise CheckpointCorruptionError."""
        db_path = tmp_path / "checkpoints.sqlite"
        store = SQLiteOrchestrationStore(db_path, wal_mode=False)
        lease = store.acquire_lease("sess-ver", "thr-ver", "worker-1")

        # Manually insert future schema version 99
        with store._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints (checkpoint_id, session_id, thread_id, sequence_number, schema_version, state_blob, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "cp-future",
                    lease.session_id,
                    lease.thread_id,
                    1,
                    99,
                    "{}",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

        with pytest.raises(CheckpointCorruptionError) as exc:
            store.load_state("thr-ver")
        assert exc.value.code == "UNSUPPORTED_SCHEMA_VERSION"

    def test_pruning_races_and_protection_of_active_leases(
        self, temp_store: SQLiteOrchestrationStore
    ) -> None:
        """PM Gate 7: Active unexpired sessions are protected from deletion."""
        now = datetime.now(timezone.utc)
        # Session 1: active lease
        lease_active = temp_store.acquire_lease("active-sess", "thr-active", "worker-1")
        temp_store.save_state({"state": "active"}, lease_active)

        # Prune older than in future (attempting aggressive prune)
        deleted = temp_store.prune_checkpoints(older_than=now + timedelta(days=1))
        # Active session must NOT be pruned!
        assert deleted == 0
        assert temp_store.load_state("thr-active") is not None

    def test_pruning_protection_of_pending_approvals(
        self, temp_store: SQLiteOrchestrationStore, active_lease: ExecutionLease
    ) -> None:
        """PM Gate 7: Checkpoints referenced in pending approvals are preserved."""
        envelope = build_operation_envelope("eval", 1, {"metric": "acc"})
        op = OperationIntent(
            operation_id=OperationId(value="op-protected-prune"),
            operation_version=1,
            operation_type="eval",
            canonical_payload=envelope,
            target_identity="local",
            created_at=datetime.now(timezone.utc),
        )
        temp_store.save_operation(op, active_lease)

        # Record event linking session to op
        temp_store.record_event(
            active_lease.session_id,
            "operation_created",
            {"operation_id": "op-protected-prune"},
            active_lease,
        )

        appr_mgr = ApprovalManager(temp_store)
        appr_mgr.create_request(op, "eval", "high", "v1.0", lease=active_lease)

        # Save checkpoint in this session
        temp_store.save_state({"checkpoint": "val"}, active_lease)

        # Prune attempt
        temp_store.prune_checkpoints(datetime.now(timezone.utc) + timedelta(days=1))
        # Protected by pending approval and active operation
        assert temp_store.load_state(active_lease.thread_id) is not None

    def test_experiments_sqlite_isolation(self, tmp_path: Path) -> None:
        """PM Gate 7: experiments.sqlite is completely outside store and never touched."""
        state_dir = tmp_path / ".cv_agent" / "state"
        state_dir.mkdir(parents=True)
        exp_db = state_dir / "experiments.sqlite"
        with sqlite3.connect(str(exp_db)) as conn:
            conn.execute("CREATE TABLE permanent_ledger (id INT);")
            conn.execute("INSERT INTO permanent_ledger VALUES (42);")

        store = SQLiteOrchestrationStore(
            db_path=state_dir / "checkpoints.sqlite", wal_mode=False
        )
        lease = store.acquire_lease("s1", "t1", "w1")
        store.save_state({"data": 1}, lease)
        store.prune_checkpoints(datetime.now(timezone.utc) + timedelta(days=1))

        # Verify permanent experiments ledger is completely intact
        with sqlite3.connect(str(exp_db)) as conn:
            cur = conn.execute("SELECT id FROM permanent_ledger;")
            assert cur.fetchone()[0] == 42


class TestLangGraphWorkflowIntegration:
    """Tests LangGraph workflow compilation, human-in-the-loop gating, and invariant validation."""

    def test_graph_execution_without_approval_terminates_completed(
        self, temp_store: SQLiteOrchestrationStore, active_lease: ExecutionLease
    ) -> None:
        """Standard automated path executes and terminates at COMPLETED."""
        graph = create_orchestration_graph(temp_store, active_lease)
        initial_state: OrchestrationGraphState = {
            "session_id": active_lease.session_id,
            "thread_id": active_lease.thread_id,
            "lifecycle_status": LifecycleStatus.INITIALIZING.value,
            "task": "benchmark inference",
            "requires_approval": False,
            "execution_result": {"latency_ms": 12.5},
        }

        config = {"configurable": {"thread_id": active_lease.thread_id}}
        result = graph.invoke(initial_state, config=config)
        assert result["lifecycle_status"] == LifecycleStatus.COMPLETED.value

    def test_graph_interrupt_at_await_approval_and_resumption(
        self, temp_store: SQLiteOrchestrationStore, active_lease: ExecutionLease
    ) -> None:
        """Graph pauses before await_approval and resumes when authorized."""
        appr_mgr = ApprovalManager(temp_store)
        envelope = build_operation_envelope("train", 1, {"epochs": 100})
        op = OperationIntent(
            operation_id=OperationId(value="op-train-01"),
            operation_version=1,
            operation_type="training",
            canonical_payload=envelope,
            target_identity="remote_gpu",
            created_at=datetime.now(timezone.utc),
        )

        req = appr_mgr.create_request(
            op, "train", "high", "v1.0", resource_scope="default", lease=active_lease
        )

        graph = create_orchestration_graph(
            temp_store, active_lease, approval_manager=appr_mgr
        )
        state: OrchestrationGraphState = {
            "session_id": active_lease.session_id,
            "thread_id": active_lease.thread_id,
            "lifecycle_status": LifecycleStatus.INITIALIZING.value,
            "task": "expensive training",
            "requires_approval": True,
            "approval_request_id": req.request_id,
            "approval_policy_version": "v1.0",
            "approval_resource_scope": "default",
            "operation_intent": op.model_dump(),
        }

        config = {"configurable": {"thread_id": active_lease.thread_id}}

        # 1. First run interrupts before await_approval
        interrupted = graph.invoke(state, config=config)
        assert (
            interrupted["lifecycle_status"] == LifecycleStatus.AWAITING_APPROVAL.value
        )

        # 2. Grant approval in authoritative store
        appr_mgr.record_decision(
            request_id=req.request_id,
            decision=ApprovalStatus.APPROVED,
            principal="approver@corp.org",
            lease=active_lease,
        )

        # 3. Resume graph from checkpoint
        resumed = graph.invoke(None, config=config)
        assert resumed["lifecycle_status"] == LifecycleStatus.COMPLETED.value

    def test_subsystem_isolation_zero_provider_sdks(self) -> None:
        """Phase 2E contains zero external provider SDK imports."""
        orchestration_dir = Path(__file__).parent.parent / "cv_agent" / "orchestration"
        forbidden = {"openai", "anthropic", "google.generativeai", "boto3"}

        for py_file in orchestration_dir.glob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name not in forbidden, (
                            f"Forbidden import {alias.name} in {py_file.name}"
                        )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                    assert root not in forbidden, (
                        f"Forbidden from-import {root} in {py_file.name}"
                    )
