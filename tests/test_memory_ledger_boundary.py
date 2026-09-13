"""tests.test_memory_ledger_boundary — Contract and regression tests for Phase 2F (ADR-0004).

Verifies:
1. Dual-layer workspace scoping (.cv_agent/memory/ vs .cv_agent/state/experiments.sqlite).
2. Strict terminal result immutability [P§25].
3. Formal lifecycle transition matrix (§2.4.B).
4. Relational integrity & foreign keys (PRAGMA foreign_keys = ON; ON DELETE RESTRICT).
5. Workload-aware metric completeness (§2.4.C, [P§29.4]).
6. Active baseline mutability isolation without modifying historical rows [P§29.2].
7. Complete reproducibility metadata enforcement [P§25], [P§29.5].
8. Decoupled approval dereferencing ({operation_id}:{request_id}).
9. Concurrency safety (competing writers under WAL).
10. Cross-backend isolation (checkpoints.sqlite != experiments.sqlite).
11. Symlink-aware workspace containment.
12. Canonical memory markdown/YAML serialization.
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
from pathlib import Path

import pytest

from cv_agent.experiments import (
    AccuracyMetrics,
    ApprovalReferenceFormatError,
    ArtifactManifest,
    ExecutionTarget,
    ExperimentImmutabilityError,
    ExperimentSpec,
    ExperimentStatus,
    IncompleteMetricsError,
    InvalidExperimentTransitionError,
    LedgerWorkspaceContainmentError,
    RelationalIntegrityError,
    SQLiteExperimentLedger,
    SystemMetrics,
    WorkloadType,
)
from cv_agent.memory import (
    ArchitecturalDecision,
    GitProjectMemory,
    HardwareConstraints,
    LearnedHeuristic,
    ProjectMemoryFormatError,
    WorkspaceContainmentError,
)
from cv_agent.orchestration.store import SQLiteOrchestrationStore


def _sample_spec(
    name: str = "yolo_baseline",
    workload_type: WorkloadType = WorkloadType.TRAINING,
    parent_exp_id: str | None = None,
    baseline_id: str | None = None,
    approval_ref: str | None = None,
) -> ExperimentSpec:
    return ExperimentSpec(
        name=name,
        hypothesis="YOLOv8 baseline test",
        workload_type=workload_type,
        target=ExecutionTarget.LOCAL_GPU,
        model_name="yolov8",
        model_variant="yolov8n",
        commit_sha="a" * 40,
        dataset_version="ds-v1.0.0",
        seed=42,
        batch_size=16,
        input_resolution=(640, 640),
        precision="FP16",
        config_uri="configs/baseline.yaml",
        config_hash="b" * 64,
        parent_exp_id=parent_exp_id,
        baseline_id=baseline_id,
        approval_ref=approval_ref,
        hyperparameters={"lr": 0.001, "optimizer": "adamw"},
    )


class TestProjectMemoryBoundary:
    """Tests Git-tracked structured project memory (.cv_agent/memory/)."""

    def test_initialization_and_workspace_scoping(self, tmp_path: Path) -> None:
        mem = GitProjectMemory(tmp_path)
        assert mem.memory_dir == (tmp_path / ".cv_agent" / "memory").resolve()
        assert (mem.memory_dir / "project_spec.md").exists()
        assert (mem.memory_dir / "dataset_lineage.md").exists()

    def test_symlink_aware_workspace_containment(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        mem = GitProjectMemory(workspace)
        symlink_path = workspace / ".cv_agent" / "memory" / "evil_symlink"
        symlink_path.symlink_to(outside_dir)

        with pytest.raises(WorkspaceContainmentError):
            mem._validate_containment(symlink_path)

    def test_hardware_constraints_roundtrip(self, tmp_path: Path) -> None:
        mem = GitProjectMemory(tmp_path)
        constraints = HardwareConstraints(
            target_device="nvidia_jetson_orin_nano_8gb",
            max_vram_mb=6144,
            max_ram_mb=7168,
            power_budget_watts=15.0,
            latency_ceiling_ms=33.3,
            supported_precisions=("FP32", "FP16", "INT8"),
        )
        mem.update_hardware_constraints(constraints)
        loaded = mem.get_hardware_constraints()
        assert loaded == constraints
        assert (
            "target_device: nvidia_jetson_orin_nano_8gb"
            in mem.hardware_constraints_path.read_text()
        )

    def test_invalid_hardware_constraints_schema_fails(self, tmp_path: Path) -> None:
        mem = GitProjectMemory(tmp_path)
        mem.hardware_constraints_path.write_text(
            "invalid_yaml: [missing_target_device", encoding="utf-8"
        )
        with pytest.raises(ProjectMemoryFormatError):
            mem.get_hardware_constraints()

    def test_architectural_decisions_append_and_retrieve(self, tmp_path: Path) -> None:
        mem = GitProjectMemory(tmp_path)
        d1 = ArchitecturalDecision(
            decision_id="AD-001",
            timestamp="2026-09-13T12:00:00Z",
            title="Adopt YOLOv8 detector",
            rationale="Superior accuracy/latency Pareto frontier on target Jetson.",
            linked_experiment_ids=("EXP-20260913-0001",),
        )
        mem.record_architectural_decision(d1)
        decisions = mem.get_architectural_decisions()
        assert len(decisions) == 1
        assert decisions[0].decision_id == "AD-001"
        assert decisions[0].linked_experiment_ids == ("EXP-20260913-0001",)
        assert "Superior accuracy/latency" in decisions[0].rationale

    def test_learned_heuristics_append_and_retrieve(self, tmp_path: Path) -> None:
        mem = GitProjectMemory(tmp_path)
        h1 = LearnedHeuristic(
            heuristic_id="LH-001",
            context="Nighttime infrared camera stream",
            observation="Augmentation technique X degrades edge detection.",
            recommendation="Disable augmentation X on IR camera pipelines.",
            evidence_experiment_ids=("EXP-20260913-0002", "EXP-20260913-0003"),
        )
        mem.append_learned_heuristic(h1)
        heuristics = mem.get_learned_heuristics()
        assert len(heuristics) == 1
        assert heuristics[0].heuristic_id == "LH-001"
        assert heuristics[0].evidence_experiment_ids == (
            "EXP-20260913-0002",
            "EXP-20260913-0003",
        )


class TestExperimentLedgerBoundary:
    """Tests SQLite permanent experiment ledger (.cv_agent/state/experiments.sqlite)."""

    def test_initialization_and_containment(self, tmp_path: Path) -> None:
        ledger = SQLiteExperimentLedger(tmp_path)
        assert ledger.db_path.exists()
        assert (
            ledger.db_path
            == (tmp_path / ".cv_agent" / "state" / "experiments.sqlite").resolve()
        )

    def test_symlink_containment_violation_fails(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace_ledger"
        workspace.mkdir()
        outside_dir = tmp_path / "outside_ledger"
        outside_dir.mkdir()
        with pytest.raises(LedgerWorkspaceContainmentError):
            SQLiteExperimentLedger(workspace, db_path=outside_dir / "evil.sqlite")

    def test_create_experiment_reproducibility_fields_and_query(
        self, tmp_path: Path
    ) -> None:
        ledger = SQLiteExperimentLedger(tmp_path)
        spec = _sample_spec()
        rec = ledger.create_experiment(spec)
        assert rec.exp_id.startswith("EXP-")
        assert rec.status == ExperimentStatus.PROPOSED
        assert rec.spec.commit_sha == "a" * 40
        assert rec.spec.dataset_version == "ds-v1.0.0"
        assert rec.spec.seed == 42
        assert rec.spec.target == ExecutionTarget.LOCAL_GPU

        queried = ledger.get_experiment(rec.exp_id)
        assert queried is not None
        assert queried.exp_id == rec.exp_id
        assert queried.spec.hyperparameters == {"lr": 0.001, "optimizer": "adamw"}

    def test_approval_ref_format_validation(self, tmp_path: Path) -> None:
        ledger = SQLiteExperimentLedger(tmp_path)
        # Valid format
        valid_spec = _sample_spec(approval_ref="op-1234:req-5678")
        rec = ledger.create_experiment(valid_spec)
        assert rec.spec.approval_ref == "op-1234:req-5678"

        # Invalid format
        invalid_spec = _sample_spec(approval_ref="invalid_single_token")
        with pytest.raises(ApprovalReferenceFormatError):
            ledger.create_experiment(invalid_spec)

    def test_lifecycle_transitions_enforce_matrix(self, tmp_path: Path) -> None:
        ledger = SQLiteExperimentLedger(tmp_path)
        rec = ledger.create_experiment(_sample_spec())

        # proposed -> queued
        rec = ledger.transition_status(rec.exp_id, ExperimentStatus.QUEUED)
        assert rec.status == ExperimentStatus.QUEUED

        # queued -> running
        rec = ledger.transition_status(
            rec.exp_id, ExperimentStatus.RUNNING, job_id="job-999"
        )
        assert rec.status == ExperimentStatus.RUNNING
        assert rec.job_id == "job-999"
        assert rec.started_at is not None

        # Illegal jump: running -> proposed
        with pytest.raises(InvalidExperimentTransitionError):
            ledger.transition_status(rec.exp_id, ExperimentStatus.PROPOSED)

    def test_terminal_immutability_completed(self, tmp_path: Path) -> None:
        ledger = SQLiteExperimentLedger(tmp_path)
        rec = ledger.create_experiment(
            _sample_spec(workload_type=WorkloadType.TRAINING)
        )
        ledger.transition_status(rec.exp_id, ExperimentStatus.RUNNING)

        acc = AccuracyMetrics(map_50=0.85, map_50_95=0.65, precision=0.88, recall=0.82)
        sys = SystemMetrics(
            peak_vram_mb=4096.0, peak_ram_mb=8192.0, training_time_seconds=3600.0
        )
        art = ArtifactManifest(weights_uri="s3://models/weights.pt")

        completed = ledger.complete_experiment(
            rec.exp_id, acc, sys, art, notes="Final run"
        )
        assert completed.status == ExperimentStatus.COMPLETED
        assert completed.is_terminal is True
        assert completed.completed_at is not None

        # Attempt to modify completed experiment raises ExperimentImmutabilityError
        with pytest.raises(ExperimentImmutabilityError):
            ledger.transition_status(rec.exp_id, ExperimentStatus.RUNNING)

        with pytest.raises(ExperimentImmutabilityError):
            ledger.complete_experiment(rec.exp_id, acc, sys, art)

        with pytest.raises(ExperimentImmutabilityError):
            ledger.fail_experiment(rec.exp_id, "crash")

    def test_terminal_immutability_failed_and_cancelled(self, tmp_path: Path) -> None:
        ledger = SQLiteExperimentLedger(tmp_path)
        rec1 = ledger.create_experiment(_sample_spec())
        ledger.fail_experiment(rec1.exp_id, "CUDA out of memory")
        with pytest.raises(ExperimentImmutabilityError):
            ledger.transition_status(rec1.exp_id, ExperimentStatus.RUNNING)

        rec2 = ledger.create_experiment(_sample_spec())
        ledger.cancel_experiment(rec2.exp_id, "User requested cancellation")
        with pytest.raises(ExperimentImmutabilityError):
            ledger.transition_status(rec2.exp_id, ExperimentStatus.RUNNING)

    def test_metric_completeness_training_workload(self, tmp_path: Path) -> None:
        ledger = SQLiteExperimentLedger(tmp_path)
        rec = ledger.create_experiment(
            _sample_spec(workload_type=WorkloadType.TRAINING)
        )
        ledger.transition_status(rec.exp_id, ExperimentStatus.RUNNING)

        # Incomplete accuracy: missing recall
        incomplete_acc = AccuracyMetrics(map_50=0.85, map_50_95=0.65, precision=0.88)
        sys = SystemMetrics(
            peak_vram_mb=4096.0, peak_ram_mb=8192.0, training_time_seconds=3600.0
        )
        art = ArtifactManifest(weights_uri="weights.pt")

        with pytest.raises(IncompleteMetricsError) as exc:
            ledger.complete_experiment(rec.exp_id, incomplete_acc, sys, art)
        assert "accuracy.recall" in str(exc.value)

        # Incomplete system: missing peak_vram_mb
        complete_acc = AccuracyMetrics(
            map_50=0.85, map_50_95=0.65, precision=0.88, recall=0.82
        )
        incomplete_sys = SystemMetrics(
            peak_vram_mb=0.0, peak_ram_mb=8192.0, training_time_seconds=3600.0
        )
        with pytest.raises(IncompleteMetricsError) as exc:
            ledger.complete_experiment(rec.exp_id, complete_acc, incomplete_sys, art)
        assert "system.peak_vram_mb" in str(exc.value)

    def test_metric_completeness_optimization_workload(self, tmp_path: Path) -> None:
        ledger = SQLiteExperimentLedger(tmp_path)
        rec = ledger.create_experiment(
            _sample_spec(workload_type=WorkloadType.OPTIMIZATION)
        )
        ledger.transition_status(rec.exp_id, ExperimentStatus.RUNNING)

        acc = AccuracyMetrics(map_50=0.84)
        # Missing throughput_fps and power_draw_watts
        incomplete_sys = SystemMetrics(
            peak_vram_mb=2048.0, peak_ram_mb=4096.0, latency_inference_ms=12.5
        )
        art = ArtifactManifest(engine_uri="engine.trt")

        with pytest.raises(IncompleteMetricsError) as exc:
            ledger.complete_experiment(rec.exp_id, acc, incomplete_sys, art)
        assert "throughput_fps" in str(exc.value)

        complete_sys = SystemMetrics(
            peak_vram_mb=2048.0,
            peak_ram_mb=4096.0,
            latency_inference_ms=12.5,
            throughput_fps=80.0,
            power_draw_watts=12.0,
        )
        completed = ledger.complete_experiment(rec.exp_id, acc, complete_sys, art)
        assert completed.status == ExperimentStatus.COMPLETED

    def test_relational_lineage_and_foreign_keys(self, tmp_path: Path) -> None:
        ledger = SQLiteExperimentLedger(tmp_path)
        # Root experiment (no parent, no baseline)
        root = ledger.create_experiment(_sample_spec())
        assert root.spec.parent_exp_id is None
        assert root.spec.baseline_id is None

        # Child experiment referencing root
        child = ledger.create_experiment(_sample_spec(parent_exp_id=root.exp_id))
        assert child.spec.parent_exp_id == root.exp_id

        # Non-existent parent fails closed
        with pytest.raises(RelationalIntegrityError):
            ledger.create_experiment(_sample_spec(parent_exp_id="EXP-NON-EXISTENT"))

        # SQLite foreign key deletes restricted
        with (
            ledger._get_connection() as conn,
            pytest.raises(sqlite3.IntegrityError),
        ):
            conn.execute("DELETE FROM experiments WHERE exp_id = ?", (root.exp_id,))

    def test_active_baseline_registry_isolation(self, tmp_path: Path) -> None:
        ledger = SQLiteExperimentLedger(tmp_path)
        # Cannot set non-existent baseline
        with pytest.raises(RelationalIntegrityError):
            ledger.set_active_baseline("EXP-DOES-NOT-EXIST", WorkloadType.TRAINING)

        # Cannot set non-completed experiment as baseline
        uncompleted = ledger.create_experiment(
            _sample_spec(workload_type=WorkloadType.TRAINING)
        )
        with pytest.raises(RelationalIntegrityError):
            ledger.set_active_baseline(uncompleted.exp_id, WorkloadType.TRAINING)

        # Complete experiment and designate as baseline
        ledger.transition_status(uncompleted.exp_id, ExperimentStatus.RUNNING)
        acc = AccuracyMetrics(map_50=0.85, map_50_95=0.65, precision=0.88, recall=0.82)
        sys = SystemMetrics(
            peak_vram_mb=4096.0, peak_ram_mb=8192.0, training_time_seconds=3600.0
        )
        art = ArtifactManifest(weights_uri="weights.pt")
        completed = ledger.complete_experiment(uncompleted.exp_id, acc, sys, art)

        # Cannot designate when workload type mismatches
        with pytest.raises(RelationalIntegrityError):
            ledger.set_active_baseline(completed.exp_id, WorkloadType.OPTIMIZATION)

        # Successfully designate active baseline
        ledger.set_active_baseline(completed.exp_id, WorkloadType.TRAINING)
        baseline = ledger.get_baseline(WorkloadType.TRAINING)
        assert baseline is not None
        assert baseline.exp_id == completed.exp_id

        # Designating a new baseline updates registry without modifying historical row
        exp2 = ledger.create_experiment(
            _sample_spec(workload_type=WorkloadType.TRAINING)
        )
        ledger.transition_status(exp2.exp_id, ExperimentStatus.RUNNING)
        completed2 = ledger.complete_experiment(exp2.exp_id, acc, sys, art)
        ledger.set_active_baseline(completed2.exp_id, WorkloadType.TRAINING)

        active = ledger.get_baseline(WorkloadType.TRAINING)
        assert active is not None
        assert active.exp_id == completed2.exp_id

        # Prior baseline is still intact and immutable
        prior = ledger.get_experiment(completed.exp_id)
        assert prior is not None
        assert prior.status == ExperimentStatus.COMPLETED

    def test_concurrency_competing_writers(self, tmp_path: Path) -> None:
        """PM Finding 10: Multi-writer concurrency test against SQLiteExperimentLedger."""
        ledger = SQLiteExperimentLedger(tmp_path)

        def worker_create(index: int) -> str:
            spec = _sample_spec(name=f"worker_run_{index}")
            rec = ledger.create_experiment(spec)
            return rec.exp_id

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker_create, i) for i in range(10)]
            exp_ids = [f.result() for f in futures]

        assert len(exp_ids) == 10
        assert len(set(exp_ids)) == 10
        all_exps = ledger.list_experiments()
        assert len(all_exps) == 10

    def test_cross_backend_isolation(self, tmp_path: Path) -> None:
        """PM Finding: experiments.sqlite != checkpoints.sqlite cross-backend isolation."""
        exp_ledger = SQLiteExperimentLedger(tmp_path)
        orch_store = SQLiteOrchestrationStore(
            tmp_path / ".cv_agent" / "state" / "checkpoints.sqlite", wal_mode=False
        )

        assert exp_ledger.db_path != orch_store.db_path
        assert exp_ledger.db_path.name == "experiments.sqlite"
        assert orch_store.db_path.name == "checkpoints.sqlite"

        # Create experiment in ledger
        exp_ledger.create_experiment(_sample_spec())

        # Verify orchestration store knows nothing of experiments table
        with orch_store._get_connection() as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='experiments';"
            )
            assert cur.fetchone() is None

        # Verify experiment ledger knows nothing of orchestration checkpoints table
        with exp_ledger._get_connection() as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='checkpoints';"
            )
            assert cur.fetchone() is None
            cur_sess = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions';"
            )
            assert cur_sess.fetchone() is None
