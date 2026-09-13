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
from typing import Any

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
    ReproducibilityMetadataError,
    SQLiteExperimentLedger,
    SystemMetrics,
    WorkloadType,
    validate_metric_completeness,
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
    hypothesis: str = "YOLOv8 baseline test",
    workload_type: WorkloadType = WorkloadType.TRAINING,
    target: ExecutionTarget = ExecutionTarget.LOCAL_GPU,
    model_name: str = "yolov8",
    model_variant: str = "yolov8n",
    commit_sha: str = "a" * 40,
    dataset_version: str = "ds-v1.0.0",
    seed: int = 42,
    batch_size: int = 16,
    input_resolution: tuple[int, int] = (640, 640),
    precision: str = "FP16",
    config_uri: str = "configs/baseline.yaml",
    config_hash: str = "b" * 64,
    hyperparameters: Any = None,
    parent_exp_id: str | None = None,
    baseline_id: str | None = None,
    approval_ref: str | None = None,
    accuracy_metric_requirements: tuple[str, ...] | None = None,
) -> ExperimentSpec:
    if accuracy_metric_requirements is None:
        if workload_type == WorkloadType.TRAINING:
            reqs = ("map_50", "map_50_95", "precision", "recall")
        elif workload_type == WorkloadType.OPTIMIZATION:
            reqs = ("map_50", "map_50_95")
        elif workload_type == WorkloadType.EVALUATION:
            reqs = ("map_50", "precision", "recall")
        elif workload_type == WorkloadType.BENCHMARK:
            reqs = ("precision",)
        else:
            reqs = ()
    else:
        reqs = accuracy_metric_requirements

    return ExperimentSpec(
        name=name,
        hypothesis=hypothesis,
        workload_type=workload_type,
        target=target,
        model_name=model_name,
        model_variant=model_variant,
        commit_sha=commit_sha,
        dataset_version=dataset_version,
        seed=seed,
        batch_size=batch_size,
        input_resolution=input_resolution,
        precision=precision,
        config_uri=config_uri,
        config_hash=config_hash,
        hyperparameters=(
            hyperparameters
            if hyperparameters is not None
            else {"lr": 0.001, "optimizer": "adamw"}
        ),
        parent_exp_id=parent_exp_id,
        baseline_id=baseline_id,
        approval_ref=approval_ref,
        accuracy_metric_requirements=reqs,
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
        # YAML syntax error
        mem.hardware_constraints_path.write_text(
            "invalid_yaml: [missing_target_device", encoding="utf-8"
        )
        with pytest.raises(ProjectMemoryFormatError):
            mem.get_hardware_constraints()

        # Unexpected keys rejected
        mem.hardware_constraints_path.write_text(
            """
            target_device: "nvidia_jetson"
            max_vram_mb: 6144
            max_ram_mb: 7168
            power_budget_watts: 15.0
            latency_ceiling_ms: 33.3
            supported_precisions: ["FP16"]
            unexpected_extension_key: "forbidden"
            """,
            encoding="utf-8",
        )
        with pytest.raises(ProjectMemoryFormatError) as exc:
            mem.get_hardware_constraints()
        assert "Unexpected keys" in str(exc.value)

        # Missing required keys rejected
        mem.hardware_constraints_path.write_text(
            """
            target_device: "nvidia_jetson"
            max_vram_mb: 6144
            """,
            encoding="utf-8",
        )
        with pytest.raises(ProjectMemoryFormatError) as exc:
            mem.get_hardware_constraints()
        assert "Missing required keys" in str(exc.value)

        # Type coercion rejected (string instead of int for vram)
        mem.hardware_constraints_path.write_text(
            """
            target_device: "nvidia_jetson"
            max_vram_mb: "6144"
            max_ram_mb: 7168
            power_budget_watts: 15.0
            latency_ceiling_ms: 33.3
            supported_precisions: ["FP16"]
            """,
            encoding="utf-8",
        )
        with pytest.raises(ProjectMemoryFormatError) as exc:
            mem.get_hardware_constraints()
        assert "max_vram_mb must be a positive integer" in str(exc.value)

        # Negative value rejected
        mem.hardware_constraints_path.write_text(
            """
            target_device: "nvidia_jetson"
            max_vram_mb: -1024
            max_ram_mb: 7168
            power_budget_watts: 15.0
            latency_ceiling_ms: 33.3
            supported_precisions: ["FP16"]
            """,
            encoding="utf-8",
        )
        with pytest.raises(ProjectMemoryFormatError) as exc:
            mem.get_hardware_constraints()
        assert "max_vram_mb must be a positive integer" in str(exc.value)

        # Empty device rejected
        mem.hardware_constraints_path.write_text(
            """
            target_device: "   "
            max_vram_mb: 6144
            max_ram_mb: 7168
            power_budget_watts: 15.0
            latency_ceiling_ms: 33.3
            supported_precisions: ["FP16"]
            """,
            encoding="utf-8",
        )
        with pytest.raises(ProjectMemoryFormatError) as exc:
            mem.get_hardware_constraints()
        assert "target_device must be a non-empty string" in str(exc.value)

        # Empty precision list rejected
        mem.hardware_constraints_path.write_text(
            """
            target_device: "nvidia_jetson"
            max_vram_mb: 6144
            max_ram_mb: 7168
            power_budget_watts: 15.0
            latency_ceiling_ms: 33.3
            supported_precisions: []
            """,
            encoding="utf-8",
        )
        with pytest.raises(ProjectMemoryFormatError) as exc:
            mem.get_hardware_constraints()
        assert "supported_precisions must be a non-empty sequence" in str(exc.value)

    def test_strict_architectural_decisions_markdown_schema(
        self, tmp_path: Path
    ) -> None:
        mem = GitProjectMemory(tmp_path)
        d1 = ArchitecturalDecision(
            decision_id="AD-001",
            timestamp="2026-09-13T12:00:00Z",
            title="Adopt YOLOv8 detector",
            rationale="Superior accuracy/latency Pareto frontier.",
            linked_experiment_ids=("EXP-20260913-0001",),
        )
        mem.record_architectural_decision(d1)

        # Duplicate ID rejection
        with pytest.raises(ProjectMemoryFormatError) as exc:
            mem.record_architectural_decision(d1)
        assert "already exists" in str(exc.value)

        # Malformed block missing decision_id
        mem.architectural_choices_path.write_text(
            """# Architectural Choices

## No ID Decision
- **timestamp:** 2026-09-13T12:00:00Z

Some rationale without an ID.
""",
            encoding="utf-8",
        )
        with pytest.raises(ProjectMemoryFormatError) as exc:
            mem.get_architectural_decisions()
        assert "missing required 'decision_id'" in str(exc.value)

        # Malformed block missing timestamp
        mem.architectural_choices_path.write_text(
            """# Architectural Choices

## AD-002 — Decision Title
- **decision_id:** AD-002

Rationale without timestamp.
""",
            encoding="utf-8",
        )
        with pytest.raises(ProjectMemoryFormatError) as exc:
            mem.get_architectural_decisions()
        assert "missing required 'timestamp'" in str(exc.value)

        # Malformed block missing rationale
        mem.architectural_choices_path.write_text(
            """# Architectural Choices

## AD-003 — Decision Title
- **decision_id:** AD-003
- **timestamp:** 2026-09-13T12:00:00Z
""",
            encoding="utf-8",
        )
        with pytest.raises(ProjectMemoryFormatError) as exc:
            mem.get_architectural_decisions()
        assert "missing required 'rationale'" in str(exc.value)

    def test_strict_learned_heuristics_markdown_schema(self, tmp_path: Path) -> None:
        mem = GitProjectMemory(tmp_path)
        h1 = LearnedHeuristic(
            heuristic_id="LH-001",
            context="Nighttime infrared",
            observation="Augmentation X degrades edges",
            recommendation="Disable on IR pipelines",
            evidence_experiment_ids=("EXP-20260913-0001",),
        )
        mem.append_learned_heuristic(h1)

        # Duplicate ID rejection
        with pytest.raises(ProjectMemoryFormatError) as exc:
            mem.append_learned_heuristic(h1)
        assert "already exists" in str(exc.value)

        # Malformed block missing context
        mem.learned_heuristics_path.write_text(
            """# Learned Heuristics

## LH-002
- **heuristic_id:** LH-002
- **observation:** Obs
- **recommendation:** Rec
""",
            encoding="utf-8",
        )
        with pytest.raises(ProjectMemoryFormatError) as exc:
            mem.get_learned_heuristics()
        assert "missing required 'context'" in str(exc.value)

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
        assert rec.spec.config_uri == "configs/baseline.yaml"
        assert rec.spec.config_hash == "b" * 64

        queried = ledger.get_experiment(rec.exp_id)
        assert queried is not None
        assert queried.exp_id == rec.exp_id
        assert queried.spec.hyperparameters == {"lr": 0.001, "optimizer": "adamw"}

    def test_reproducibility_validation_independent_failures(
        self, tmp_path: Path
    ) -> None:
        """PM Finding 3: Test each reproducibility validation failure independently."""
        ledger = SQLiteExperimentLedger(tmp_path)

        # 1. Invalid commit_sha (not 40 hex chars)
        with pytest.raises(ReproducibilityMetadataError) as exc:
            ledger.create_experiment(_sample_spec(commit_sha="invalid_sha"))
        assert "Invalid commit_sha" in str(exc.value)

        # 2. Empty dataset_version
        with pytest.raises(ReproducibilityMetadataError) as exc:
            ledger.create_experiment(_sample_spec(dataset_version="   "))
        assert "dataset_version cannot be empty" in str(exc.value)

        # 3. Invalid seed (negative or bool)
        with pytest.raises(ReproducibilityMetadataError) as exc:
            ledger.create_experiment(_sample_spec(seed=-1))
        assert "Invalid seed" in str(exc.value)

        # 4. Empty config_uri
        with pytest.raises(ReproducibilityMetadataError) as exc:
            ledger.create_experiment(_sample_spec(config_uri=""))
        assert "config_uri cannot be empty" in str(exc.value)

        # 5. Empty config_hash
        with pytest.raises(ReproducibilityMetadataError) as exc:
            ledger.create_experiment(_sample_spec(config_hash=""))
        assert "config_hash cannot be empty" in str(exc.value)

        # 6. Empty hyperparameters
        with pytest.raises(ReproducibilityMetadataError) as exc:
            ledger.create_experiment(_sample_spec(hyperparameters={}))
        assert "hyperparameters mapping cannot be empty" in str(exc.value)

        # 7. Invalid input_resolution (not pair of positive ints)
        with pytest.raises(ReproducibilityMetadataError) as exc:
            ledger.create_experiment(_sample_spec(input_resolution=(0, 640)))
        assert "Invalid input_resolution" in str(exc.value)

        # 8. Invalid batch_size (not positive int)
        with pytest.raises(ReproducibilityMetadataError) as exc:
            ledger.create_experiment(_sample_spec(batch_size=0))
        assert "Invalid batch_size" in str(exc.value)

        # 9. Empty accuracy_metric_requirements for evaluation
        with pytest.raises(ReproducibilityMetadataError) as exc:
            ledger.create_experiment(
                _sample_spec(
                    workload_type=WorkloadType.EVALUATION,
                    accuracy_metric_requirements=(),
                )
            )
        assert "accuracy_metric_requirements cannot be empty" in str(exc.value)

        # 10. Empty accuracy_metric_requirements for optimization
        with pytest.raises(ReproducibilityMetadataError) as exc:
            ledger.create_experiment(
                _sample_spec(
                    workload_type=WorkloadType.OPTIMIZATION,
                    accuracy_metric_requirements=(),
                )
            )
        assert "accuracy_metric_requirements cannot be empty" in str(exc.value)

        # 11. Empty accuracy_metric_requirements for benchmark
        with pytest.raises(ReproducibilityMetadataError) as exc:
            ledger.create_experiment(
                _sample_spec(
                    workload_type=WorkloadType.BENCHMARK,
                    accuracy_metric_requirements=(),
                )
            )
        assert "accuracy_metric_requirements cannot be empty" in str(exc.value)

        # 12. Training missing one of 4 mandatory metrics
        with pytest.raises(ReproducibilityMetadataError) as exc:
            ledger.create_experiment(
                _sample_spec(
                    workload_type=WorkloadType.TRAINING,
                    accuracy_metric_requirements=("map_50", "precision", "recall"),
                )
            )
        assert (
            "Training workload requires accuracy_metric_requirements to contain"
            in str(exc.value)
        )

    def test_approval_ref_format_validation(self, tmp_path: Path) -> None:
        ledger = SQLiteExperimentLedger(tmp_path)
        valid_spec = _sample_spec(approval_ref="op-1234:req-5678")
        rec = ledger.create_experiment(valid_spec)
        assert rec.spec.approval_ref == "op-1234:req-5678"

        invalid_spec = _sample_spec(approval_ref="invalid_single_token")
        with pytest.raises(ApprovalReferenceFormatError):
            ledger.create_experiment(invalid_spec)

    def test_lifecycle_transitions_enforce_matrix(self, tmp_path: Path) -> None:
        ledger = SQLiteExperimentLedger(tmp_path)
        rec = ledger.create_experiment(_sample_spec())

        rec = ledger.transition_status(rec.exp_id, ExperimentStatus.QUEUED)
        assert rec.status == ExperimentStatus.QUEUED

        rec = ledger.transition_status(
            rec.exp_id, ExperimentStatus.RUNNING, job_id="job-999"
        )
        assert rec.status == ExperimentStatus.RUNNING
        assert rec.job_id == "job-999"
        assert rec.started_at is not None

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
            peak_vram_mb=4096.0,
            peak_ram_mb=8192.0,
            gpu_hours=1.5,
            training_time_seconds=3600.0,
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

    def test_terminal_child_record_immutability_sqlite_boundary(
        self, tmp_path: Path
    ) -> None:
        """PM Finding 2: Direct SQLite mutation of child rows on terminal experiment must be rejected by triggers."""
        ledger = SQLiteExperimentLedger(tmp_path)
        rec = ledger.create_experiment(
            _sample_spec(workload_type=WorkloadType.TRAINING)
        )
        ledger.transition_status(rec.exp_id, ExperimentStatus.RUNNING)

        acc = AccuracyMetrics(map_50=0.85, map_50_95=0.65, precision=0.88, recall=0.82)
        sys = SystemMetrics(
            peak_vram_mb=4096.0,
            peak_ram_mb=8192.0,
            gpu_hours=1.5,
            training_time_seconds=3600.0,
        )
        art = ArtifactManifest(weights_uri="weights.pt")
        ledger.complete_experiment(rec.exp_id, acc, sys, art)

        # Verify child records cannot be modified or deleted via direct SQL
        with ledger._get_connection() as conn:
            # 1. accuracy_metrics DELETE
            with pytest.raises(sqlite3.DatabaseError) as exc:
                conn.execute(
                    "DELETE FROM accuracy_metrics WHERE exp_id = ?", (rec.exp_id,)
                )
            assert "Cannot mutate accuracy_metrics" in str(exc.value)

            # 2. accuracy_metrics UPDATE
            with pytest.raises(sqlite3.DatabaseError) as exc:
                conn.execute(
                    "UPDATE accuracy_metrics SET metric_value = 0.99 WHERE exp_id = ?",
                    (rec.exp_id,),
                )
            assert "Cannot mutate accuracy_metrics" in str(exc.value)

            # 3. accuracy_metrics INSERT
            with pytest.raises(sqlite3.DatabaseError) as exc:
                conn.execute(
                    "INSERT INTO accuracy_metrics (exp_id, metric_key, metric_value) VALUES (?, 'extra', 0.99)",
                    (rec.exp_id,),
                )
            assert "Cannot insert accuracy_metrics" in str(exc.value)

            # 4. hyperparameters DELETE
            with pytest.raises(sqlite3.DatabaseError) as exc:
                conn.execute(
                    "DELETE FROM hyperparameters WHERE exp_id = ?", (rec.exp_id,)
                )
            assert "Cannot mutate hyperparameters" in str(exc.value)

            # 5. hyperparameters UPDATE
            with pytest.raises(sqlite3.DatabaseError) as exc:
                conn.execute(
                    "UPDATE hyperparameters SET param_value = 'tampered' WHERE exp_id = ?",
                    (rec.exp_id,),
                )
            assert "Cannot mutate hyperparameters" in str(exc.value)

            # 6. hyperparameters INSERT
            with pytest.raises(sqlite3.DatabaseError) as exc:
                conn.execute(
                    "INSERT INTO hyperparameters (exp_id, param_key, param_value) VALUES (?, 'tampered', 'v')",
                    (rec.exp_id,),
                )
            assert "Cannot insert hyperparameters" in str(exc.value)

            # 7. system_metrics DELETE
            with pytest.raises(sqlite3.DatabaseError) as exc:
                conn.execute(
                    "DELETE FROM system_metrics WHERE exp_id = ?", (rec.exp_id,)
                )
            assert "Cannot mutate system_metrics" in str(exc.value)

            # 8. artifacts DELETE
            with pytest.raises(sqlite3.DatabaseError) as exc:
                conn.execute("DELETE FROM artifacts WHERE exp_id = ?", (rec.exp_id,))
            assert "Cannot mutate artifacts" in str(exc.value)

            # 9. experiments table DELETE
            with pytest.raises(sqlite3.DatabaseError) as exc:
                conn.execute("DELETE FROM experiments WHERE exp_id = ?", (rec.exp_id,))
            assert "Cannot delete terminal experiment" in str(exc.value)

            # 10. experiments table UPDATE
            with pytest.raises(sqlite3.DatabaseError) as exc:
                conn.execute(
                    "UPDATE experiments SET notes = 'tampered' WHERE exp_id = ?",
                    (rec.exp_id,),
                )
            assert "Cannot update terminal experiment" in str(exc.value)

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

    def test_metric_contract_negative_tests_every_omitted_metric(
        self, tmp_path: Path
    ) -> None:
        """PM Finding 1: Negative tests for every omitted required metric independently across workloads."""
        ledger = SQLiteExperimentLedger(tmp_path)
        art = ArtifactManifest(weights_uri="weights.pt", engine_uri="engine.trt")

        # --- WorkloadType.TRAINING ---
        # Baseline complete metrics
        full_train_acc = AccuracyMetrics(
            map_50=0.85, map_50_95=0.65, precision=0.88, recall=0.82
        )
        full_train_sys = SystemMetrics(
            peak_vram_mb=4096.0,
            peak_ram_mb=8192.0,
            gpu_hours=1.5,
            training_time_seconds=3600.0,
        )

        def _assert_train_fails(acc: AccuracyMetrics, sys: SystemMetrics, token: str):
            r = ledger.create_experiment(
                _sample_spec(workload_type=WorkloadType.TRAINING)
            )
            ledger.transition_status(r.exp_id, ExperimentStatus.RUNNING)
            with pytest.raises(IncompleteMetricsError) as ex:
                ledger.complete_experiment(r.exp_id, acc, sys, art)
            assert token in str(ex.value)

        # 1. Missing map_50
        _assert_train_fails(
            AccuracyMetrics(map_50_95=0.65, precision=0.88, recall=0.82),
            full_train_sys,
            "accuracy.map_50",
        )
        # 2. Missing map_50_95
        _assert_train_fails(
            AccuracyMetrics(map_50=0.85, precision=0.88, recall=0.82),
            full_train_sys,
            "accuracy.map_50_95",
        )
        # 3. Missing precision
        _assert_train_fails(
            AccuracyMetrics(map_50=0.85, map_50_95=0.65, recall=0.82),
            full_train_sys,
            "accuracy.precision",
        )
        # 4. Missing recall
        _assert_train_fails(
            AccuracyMetrics(map_50=0.85, map_50_95=0.65, precision=0.88),
            full_train_sys,
            "accuracy.recall",
        )
        # 5. Missing gpu_hours (No OR!)
        _assert_train_fails(
            full_train_acc,
            SystemMetrics(
                peak_vram_mb=4096.0, peak_ram_mb=8192.0, training_time_seconds=3600.0
            ),
            "system.gpu_hours",
        )
        # 6. Missing training_time_seconds (No OR!)
        _assert_train_fails(
            full_train_acc,
            SystemMetrics(peak_vram_mb=4096.0, peak_ram_mb=8192.0, gpu_hours=1.5),
            "system.training_time_seconds",
        )
        # 7. Missing peak_vram_mb
        _assert_train_fails(
            full_train_acc,
            SystemMetrics(
                peak_ram_mb=8192.0, gpu_hours=1.5, training_time_seconds=3600.0
            ),
            "system.peak_vram_mb",
        )

        # --- WorkloadType.OPTIMIZATION ---
        opt_spec = _sample_spec(
            workload_type=WorkloadType.OPTIMIZATION,
            accuracy_metric_requirements=("map_50", "map_50_95"),
        )
        full_opt_acc = AccuracyMetrics(map_50=0.84, map_50_95=0.64)
        full_opt_sys = SystemMetrics(
            peak_vram_mb=2048.0,
            peak_ram_mb=4096.0,
            latency_inference_ms=12.5,
            latency_e2e_ms=18.0,
            throughput_fps=80.0,
            power_draw_watts=12.0,
        )

        def _assert_opt_fails(acc: AccuracyMetrics, sys: SystemMetrics, token: str):
            r = ledger.create_experiment(opt_spec)
            ledger.transition_status(r.exp_id, ExperimentStatus.RUNNING)
            with pytest.raises(IncompleteMetricsError) as ex:
                ledger.complete_experiment(r.exp_id, acc, sys, art)
            assert token in str(ex.value)

        # 8. Missing map_50
        _assert_opt_fails(
            AccuracyMetrics(map_50_95=0.64), full_opt_sys, "accuracy.map_50"
        )
        # 9. Missing map_50_95 (No arbitrary OR!)
        _assert_opt_fails(
            AccuracyMetrics(map_50=0.84), full_opt_sys, "accuracy.map_50_95"
        )
        # 10. Missing latency_inference_ms (No OR!)
        _assert_opt_fails(
            full_opt_acc,
            SystemMetrics(
                peak_vram_mb=2048.0,
                peak_ram_mb=4096.0,
                latency_e2e_ms=18.0,
                throughput_fps=80.0,
                power_draw_watts=12.0,
            ),
            "system.latency_inference_ms",
        )
        # 11. Missing latency_e2e_ms (No OR!)
        _assert_opt_fails(
            full_opt_acc,
            SystemMetrics(
                peak_vram_mb=2048.0,
                peak_ram_mb=4096.0,
                latency_inference_ms=12.5,
                throughput_fps=80.0,
                power_draw_watts=12.0,
            ),
            "system.latency_e2e_ms",
        )
        # 12. Missing throughput_fps
        _assert_opt_fails(
            full_opt_acc,
            SystemMetrics(
                peak_vram_mb=2048.0,
                peak_ram_mb=4096.0,
                latency_inference_ms=12.5,
                latency_e2e_ms=18.0,
                power_draw_watts=12.0,
            ),
            "system.throughput_fps",
        )
        # 13. Missing peak_vram_mb
        _assert_opt_fails(
            full_opt_acc,
            SystemMetrics(
                peak_ram_mb=4096.0,
                latency_inference_ms=12.5,
                latency_e2e_ms=18.0,
                throughput_fps=80.0,
                power_draw_watts=12.0,
            ),
            "system.peak_vram_mb",
        )
        # 14. Missing power_draw_watts
        _assert_opt_fails(
            full_opt_acc,
            SystemMetrics(
                peak_vram_mb=2048.0,
                peak_ram_mb=4096.0,
                latency_inference_ms=12.5,
                latency_e2e_ms=18.0,
                throughput_fps=80.0,
            ),
            "system.power_draw_watts",
        )

        # --- WorkloadType.BENCHMARK ---
        bench_spec = _sample_spec(
            workload_type=WorkloadType.BENCHMARK,
            accuracy_metric_requirements=("precision", "recall"),
        )
        full_bench_acc = AccuracyMetrics(precision=0.88, recall=0.84)
        full_bench_sys = SystemMetrics(
            peak_vram_mb=2048.0,
            peak_ram_mb=4096.0,
            latency_inference_ms=12.5,
            latency_e2e_ms=18.0,
            throughput_fps=80.0,
            power_draw_watts=12.0,
        )

        def _assert_bench_fails(acc: AccuracyMetrics, sys: SystemMetrics, token: str):
            r = ledger.create_experiment(bench_spec)
            ledger.transition_status(r.exp_id, ExperimentStatus.RUNNING)
            with pytest.raises(IncompleteMetricsError) as ex:
                ledger.complete_experiment(r.exp_id, acc, sys, art)
            assert token in str(ex.value)

        # 15. Missing recall
        _assert_bench_fails(
            AccuracyMetrics(precision=0.88), full_bench_sys, "accuracy.recall"
        )
        # 16. Missing precision
        _assert_bench_fails(
            AccuracyMetrics(recall=0.84), full_bench_sys, "accuracy.precision"
        )
        # 17. Missing benchmark latency_inference_ms
        _assert_bench_fails(
            full_bench_acc,
            SystemMetrics(
                peak_vram_mb=2048.0,
                latency_e2e_ms=18.0,
                throughput_fps=80.0,
                power_draw_watts=12.0,
            ),
            "system.latency_inference_ms",
        )
        # 18. Missing benchmark latency_e2e_ms
        _assert_bench_fails(
            full_bench_acc,
            SystemMetrics(
                peak_vram_mb=2048.0,
                latency_inference_ms=12.5,
                throughput_fps=80.0,
                power_draw_watts=12.0,
            ),
            "system.latency_e2e_ms",
        )

        # --- WorkloadType.EVALUATION ---
        eval_spec = _sample_spec(
            workload_type=WorkloadType.EVALUATION,
            accuracy_metric_requirements=("map_50", "precision", "recall"),
        )
        full_eval_acc = AccuracyMetrics(map_50=0.82, precision=0.85, recall=0.80)
        full_eval_sys = SystemMetrics(
            throughput_fps=60.0, latency_inference_ms=15.0, peak_vram_mb=2048.0
        )

        def _assert_eval_fails(acc: AccuracyMetrics, sys: SystemMetrics, token: str):
            r = ledger.create_experiment(eval_spec)
            ledger.transition_status(r.exp_id, ExperimentStatus.RUNNING)
            with pytest.raises(IncompleteMetricsError) as ex:
                ledger.complete_experiment(r.exp_id, acc, sys, art)
            assert token in str(ex.value)

        # 19. Missing precision in evaluation
        _assert_eval_fails(
            AccuracyMetrics(map_50=0.82, recall=0.80),
            full_eval_sys,
            "accuracy.precision",
        )
        # 20. Missing recall in evaluation
        _assert_eval_fails(
            AccuracyMetrics(map_50=0.82, precision=0.85),
            full_eval_sys,
            "accuracy.recall",
        )
        # 21. Missing throughput_fps in deployability evaluation
        _assert_eval_fails(
            full_eval_acc,
            SystemMetrics(latency_inference_ms=15.0, peak_vram_mb=2048.0),
            "system.throughput_fps",
        )
        # 22. Missing latency_inference_ms in deployability evaluation
        _assert_eval_fails(
            full_eval_acc,
            SystemMetrics(throughput_fps=60.0, peak_vram_mb=2048.0),
            "system.latency_inference_ms",
        )
        # 23. Missing peak_vram_mb in deployability evaluation
        _assert_eval_fails(
            full_eval_acc,
            SystemMetrics(throughput_fps=60.0, latency_inference_ms=15.0),
            "system.peak_vram_mb",
        )

    def test_arbitrary_single_metric_cannot_satisfy_multi_metric_requirements(
        self, tmp_path: Path
    ) -> None:
        """PM Ruling: Arbitrary single populated metric cannot satisfy multi-metric requirements (zero arbitrary OR)."""
        ledger = SQLiteExperimentLedger(tmp_path)
        art = ArtifactManifest(weights_uri="weights.pt", engine_uri="engine.trt")

        # 1. EVALUATION with multi-metric requirements: ("map_50", "precision", "recall")
        eval_spec = _sample_spec(
            workload_type=WorkloadType.EVALUATION,
            accuracy_metric_requirements=("map_50", "precision", "recall"),
        )
        r_eval = ledger.create_experiment(eval_spec)
        ledger.transition_status(r_eval.exp_id, ExperimentStatus.RUNNING)

        eval_sys = SystemMetrics(
            throughput_fps=60.0, latency_inference_ms=15.0, peak_vram_mb=2048.0
        )
        # Providing ONLY map_50 fails (precision and recall still missing)
        with pytest.raises(IncompleteMetricsError) as ex:
            ledger.complete_experiment(
                r_eval.exp_id, AccuracyMetrics(map_50=0.82), eval_sys, art
            )
        assert "accuracy.precision" in str(ex.value)
        assert "accuracy.recall" in str(ex.value)

        # 2. OPTIMIZATION with multi-metric requirements: ("map_50", "accuracy_drop_tolerance")
        opt_spec = _sample_spec(
            workload_type=WorkloadType.OPTIMIZATION,
            accuracy_metric_requirements=("map_50", "accuracy_drop_tolerance"),
        )
        r_opt = ledger.create_experiment(opt_spec)
        ledger.transition_status(r_opt.exp_id, ExperimentStatus.RUNNING)

        opt_sys = SystemMetrics(
            peak_vram_mb=2048.0,
            peak_ram_mb=4096.0,
            latency_inference_ms=12.5,
            latency_e2e_ms=18.0,
            throughput_fps=80.0,
            power_draw_watts=12.0,
        )
        # Providing ONLY map_50 fails (requires tolerance too!)
        with pytest.raises(IncompleteMetricsError) as ex:
            ledger.complete_experiment(
                r_opt.exp_id, AccuracyMetrics(map_50=0.82), opt_sys, art
            )
        assert "accuracy.accuracy_drop_tolerance" in str(ex.value)

        # Providing both succeeds
        completed_opt = ledger.complete_experiment(
            r_opt.exp_id,
            AccuracyMetrics(
                map_50=0.82,
                additional_metrics={"accuracy_drop_tolerance": 0.008},
            ),
            opt_sys,
            art,
        )
        assert completed_opt.status == ExperimentStatus.COMPLETED

        # 3. Direct validate_metric_completeness call with empty accuracy_metric_requirements fails closed
        with pytest.raises(IncompleteMetricsError) as ex_eval:
            validate_metric_completeness(
                WorkloadType.EVALUATION,
                AccuracyMetrics(map_50=0.90),
                eval_sys,
                accuracy_metric_requirements=(),
            )
        assert "No task-specific accuracy evidence declared" in str(ex_eval.value)

        with pytest.raises(IncompleteMetricsError) as ex_opt:
            validate_metric_completeness(
                WorkloadType.OPTIMIZATION,
                AccuracyMetrics(map_50=0.90),
                opt_sys,
                accuracy_metric_requirements=(),
            )
        assert "No post-optimization accuracy/tolerance evidence declared" in str(
            ex_opt.value
        )

        with pytest.raises(IncompleteMetricsError) as ex_bench:
            validate_metric_completeness(
                WorkloadType.BENCHMARK,
                AccuracyMetrics(precision=0.90),
                opt_sys,
                accuracy_metric_requirements=(),
            )
        assert "No relevant accuracy verification evidence declared" in str(
            ex_bench.value
        )

    def test_missing_vs_zero_system_metrics(self, tmp_path: Path) -> None:
        """PM Finding 4: Absence of persisted system metric must remain None, not fabricated 0.0."""
        ledger = SQLiteExperimentLedger(tmp_path)
        rec = ledger.create_experiment(
            _sample_spec(
                workload_type=WorkloadType.OPTIMIZATION,
                accuracy_metric_requirements=("map_50",),
            )
        )
        ledger.transition_status(rec.exp_id, ExperimentStatus.RUNNING)

        acc = AccuracyMetrics(map_50=0.85)
        # peak_ram_mb and gpu_hours omitted (None)
        sys = SystemMetrics(
            peak_vram_mb=2048.0,
            peak_ram_mb=None,
            latency_inference_ms=10.0,
            latency_e2e_ms=15.0,
            throughput_fps=100.0,
            power_draw_watts=10.0,
        )
        art = ArtifactManifest(engine_uri="engine.trt")
        completed = ledger.complete_experiment(rec.exp_id, acc, sys, art)

        queried = ledger.get_experiment(completed.exp_id)
        assert queried is not None
        assert queried.system_metrics is not None
        assert queried.system_metrics.peak_vram_mb == 2048.0
        # Crucial invariant: missing evidence is None, not 0.0
        assert queried.system_metrics.peak_ram_mb is None
        assert queried.system_metrics.gpu_hours is None

        # Verify in SQLite directly that no fabricated row exists
        with ledger._get_connection() as conn:
            cur = conn.execute(
                "SELECT metric_value FROM system_metrics WHERE exp_id = ? AND metric_key = 'peak_ram_mb'",
                (completed.exp_id,),
            )
            assert cur.fetchone() is None

    def test_relational_lineage_and_foreign_keys(self, tmp_path: Path) -> None:
        ledger = SQLiteExperimentLedger(tmp_path)
        root = ledger.create_experiment(_sample_spec())
        assert root.spec.parent_exp_id is None
        assert root.spec.baseline_id is None

        child = ledger.create_experiment(_sample_spec(parent_exp_id=root.exp_id))
        assert child.spec.parent_exp_id == root.exp_id

        with pytest.raises(RelationalIntegrityError):
            ledger.create_experiment(_sample_spec(parent_exp_id="EXP-NON-EXISTENT"))

        with (
            ledger._get_connection() as conn,
            pytest.raises(sqlite3.IntegrityError),
        ):
            conn.execute("DELETE FROM experiments WHERE exp_id = ?", (root.exp_id,))

    def test_active_baseline_registry_isolation_and_database_protection(
        self, tmp_path: Path
    ) -> None:
        """PM Finding 5: Baseline invariants protection at both ledger API and SQLite trigger levels."""
        ledger = SQLiteExperimentLedger(tmp_path)
        with pytest.raises(RelationalIntegrityError):
            ledger.set_active_baseline("EXP-DOES-NOT-EXIST", WorkloadType.TRAINING)

        uncompleted = ledger.create_experiment(
            _sample_spec(workload_type=WorkloadType.TRAINING)
        )
        with pytest.raises(RelationalIntegrityError):
            ledger.set_active_baseline(uncompleted.exp_id, WorkloadType.TRAINING)

        # Direct SQL insertion with uncompleted experiment must be aborted by trigger
        with (
            ledger._get_connection() as conn,
            pytest.raises(sqlite3.DatabaseError) as exc,
        ):
            conn.execute(
                "INSERT INTO active_baselines (workload_type, exp_id, updated_at) VALUES ('training', ?, 'now')",
                (uncompleted.exp_id,),
            )
        assert "Active baseline experiment must have status completed" in str(exc.value)

        # Complete training experiment
        ledger.transition_status(uncompleted.exp_id, ExperimentStatus.RUNNING)
        acc = AccuracyMetrics(map_50=0.85, map_50_95=0.65, precision=0.88, recall=0.82)
        sys = SystemMetrics(
            peak_vram_mb=4096.0,
            peak_ram_mb=8192.0,
            gpu_hours=1.5,
            training_time_seconds=3600.0,
        )
        art = ArtifactManifest(weights_uri="weights.pt")
        completed = ledger.complete_experiment(uncompleted.exp_id, acc, sys, art)

        # Ledger API workload mismatch rejection
        with pytest.raises(RelationalIntegrityError):
            ledger.set_active_baseline(completed.exp_id, WorkloadType.OPTIMIZATION)

        # Direct SQL insertion with workload mismatch must be aborted by trigger
        with (
            ledger._get_connection() as conn,
            pytest.raises(sqlite3.DatabaseError) as exc,
        ):
            conn.execute(
                "INSERT INTO active_baselines (workload_type, exp_id, updated_at) VALUES ('optimization', ?, 'now')",
                (completed.exp_id,),
            )
        assert "Active baseline experiment workload_type mismatch" in str(exc.value)

        # Valid baseline designation
        ledger.set_active_baseline(completed.exp_id, WorkloadType.TRAINING)
        baseline = ledger.get_baseline(WorkloadType.TRAINING)
        assert baseline is not None
        assert baseline.exp_id == completed.exp_id

    def test_concurrency_strong_sequence_and_uniqueness(self, tmp_path: Path) -> None:
        """PM Finding 6: Concurrent creation under WAL verifying N records, sequence contiguity, and zero collision."""
        ledger = SQLiteExperimentLedger(tmp_path)
        total_workers = 20

        def worker_create(index: int) -> str:
            spec = _sample_spec(name=f"concurrent_run_{index}")
            rec = ledger.create_experiment(spec)
            return rec.exp_id

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(worker_create, i) for i in range(total_workers)]
            exp_ids = [f.result() for f in futures]

        assert len(exp_ids) == total_workers
        assert len(set(exp_ids)) == total_workers

        # Verify contiguous numeric suffix sequence with no gaps or collisions
        sequences = sorted(int(eid.split("-")[-1]) for eid in exp_ids)
        expected = list(range(1, total_workers + 1))
        assert sequences == expected, (
            f"Sequences {sequences} do not match expected {expected}"
        )

        # Verify all records exist in DB
        all_exps = ledger.list_experiments()
        assert len(all_exps) == total_workers

    def test_cross_backend_isolation(self, tmp_path: Path) -> None:
        """PM Finding: experiments.sqlite != checkpoints.sqlite cross-backend isolation."""
        exp_ledger = SQLiteExperimentLedger(tmp_path)
        orch_store = SQLiteOrchestrationStore(
            tmp_path / ".cv_agent" / "state" / "checkpoints.sqlite", wal_mode=False
        )

        assert exp_ledger.db_path != orch_store.db_path
        assert exp_ledger.db_path.name == "experiments.sqlite"
        assert orch_store.db_path.name == "checkpoints.sqlite"

        exp_ledger.create_experiment(_sample_spec())

        with orch_store._get_connection() as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='experiments';"
            )
            assert cur.fetchone() is None

        with exp_ledger._get_connection() as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='checkpoints';"
            )
            assert cur.fetchone() is None
            cur_sess = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions';"
            )
            assert cur_sess.fetchone() is None
