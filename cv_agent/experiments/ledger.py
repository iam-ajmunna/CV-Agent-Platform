"""cv_agent.experiments.ledger — SQLite-backed experiment ledger implementation.

Implements ADR-0004:
- Database at <workspace_root>/.cv_agent/state/experiments.sqlite.
- PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON; enforced on every connection.
- Relational tables: experiments, hyperparameters, accuracy_metrics, system_metrics, artifacts, active_baselines.
- Single atomic transaction for terminal completion.
- Relational lineage (parent_exp_id) and baseline integrity (baseline_id).
- Dedicated active_baselines registry preserving historical experiment immutability.
- Symlink-aware workspace containment.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cv_agent.experiments.interfaces import (
    AccuracyMetrics,
    ArtifactManifest,
    ExecutionTarget,
    ExperimentLedger,
    ExperimentRecord,
    ExperimentSpec,
    ExperimentStatus,
    SystemMetrics,
    WorkloadType,
)
from cv_agent.experiments.metrics_validator import validate_metric_completeness
from cv_agent.experiments.state_machine import (
    ExperimentImmutabilityError,
    validate_experiment_transition,
)


class LedgerError(Exception):
    """Base exception for ledger operations."""


class LedgerWorkspaceContainmentError(LedgerError):
    """Raised when ledger path violates workspace containment."""


class RelationalIntegrityError(LedgerError):
    """Raised when foreign key or baseline integrity is violated."""


class ApprovalReferenceFormatError(LedgerError):
    """Raised when approval_ref does not match {operation_id}:{request_id}."""


class ReproducibilityMetadataError(LedgerError):
    """Raised when experiment initialization specification lacks mandatory reproducibility metadata."""


APPROVAL_REF_REGEX = re.compile(r"^[a-zA-Z0-9_.-]+:[a-zA-Z0-9_.-]+$")
COMMIT_SHA_REGEX = re.compile(r"^[0-9a-fA-F]{40}$")


class SQLiteExperimentLedger(ExperimentLedger):
    """Authoritative SQLite-backed implementation of ExperimentLedger."""

    def __init__(
        self,
        workspace_root: Path | str,
        db_path: Path | str | None = None,
        wal_mode: bool = True,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        if db_path is None:
            self.db_path = (
                self.workspace_root / ".cv_agent" / "state" / "experiments.sqlite"
            ).resolve()
        else:
            self.db_path = Path(db_path).resolve()

        self._validate_containment(self.db_path)
        self.wal_mode = wal_mode
        self._init_db()

    def _validate_containment(self, target_path: Path) -> Path:
        """Enforces symlink-aware containment within workspace_root."""
        resolved = target_path.resolve()
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError as err:
            raise LedgerWorkspaceContainmentError(
                f"Path {target_path} resolves outside workspace root {self.workspace_root}"
            ) from err
        return resolved

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        """Yields a SQLite connection with WAL mode and foreign keys strictly enforced."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            if self.wal_mode:
                conn.execute("PRAGMA journal_mode = WAL;")
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Initializes relational schema per ADR-0004 §3."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS experiments (
                    exp_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    hypothesis TEXT NOT NULL,
                    workload_type TEXT NOT NULL,
                    target TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    model_variant TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    dataset_version TEXT NOT NULL,
                    seed INTEGER NOT NULL,
                    batch_size INTEGER NOT NULL,
                    input_resolution_w INTEGER NOT NULL,
                    input_resolution_h INTEGER NOT NULL,
                    precision TEXT NOT NULL,
                    config_uri TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    accuracy_metric_requirements TEXT NOT NULL,
                    parent_exp_id TEXT,
                    baseline_id TEXT,
                    approval_ref TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    job_id TEXT,
                    error_message TEXT,
                    notes TEXT,
                    FOREIGN KEY (parent_exp_id) REFERENCES experiments(exp_id) ON DELETE RESTRICT,
                    FOREIGN KEY (baseline_id) REFERENCES experiments(exp_id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS hyperparameters (
                    exp_id TEXT NOT NULL,
                    param_key TEXT NOT NULL,
                    param_value TEXT NOT NULL,
                    PRIMARY KEY (exp_id, param_key),
                    FOREIGN KEY (exp_id) REFERENCES experiments(exp_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS accuracy_metrics (
                    exp_id TEXT NOT NULL,
                    metric_key TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    PRIMARY KEY (exp_id, metric_key),
                    FOREIGN KEY (exp_id) REFERENCES experiments(exp_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS system_metrics (
                    exp_id TEXT NOT NULL,
                    metric_key TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    PRIMARY KEY (exp_id, metric_key),
                    FOREIGN KEY (exp_id) REFERENCES experiments(exp_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    exp_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    checksum TEXT,
                    PRIMARY KEY (exp_id, artifact_type),
                    FOREIGN KEY (exp_id) REFERENCES experiments(exp_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS active_baselines (
                    workload_type TEXT PRIMARY KEY,
                    exp_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (exp_id) REFERENCES experiments(exp_id) ON DELETE RESTRICT
                );

                -- Terminal immutability triggers for experiments table
                CREATE TRIGGER IF NOT EXISTS trg_prevent_terminal_experiment_update
                BEFORE UPDATE ON experiments
                FOR EACH ROW
                WHEN OLD.status IN ('completed', 'failed', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot update terminal experiment');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_prevent_terminal_experiment_delete
                BEFORE DELETE ON experiments
                FOR EACH ROW
                WHEN OLD.status IN ('completed', 'failed', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot delete terminal experiment');
                END;

                -- Terminal immutability triggers for accuracy_metrics table
                CREATE TRIGGER IF NOT EXISTS trg_prevent_accuracy_metrics_update
                BEFORE UPDATE ON accuracy_metrics
                FOR EACH ROW
                WHEN (SELECT status FROM experiments WHERE exp_id = OLD.exp_id) IN ('completed', 'failed', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot mutate accuracy_metrics for terminal experiment');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_prevent_accuracy_metrics_delete
                BEFORE DELETE ON accuracy_metrics
                FOR EACH ROW
                WHEN (SELECT status FROM experiments WHERE exp_id = OLD.exp_id) IN ('completed', 'failed', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot mutate accuracy_metrics for terminal experiment');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_prevent_accuracy_metrics_insert
                BEFORE INSERT ON accuracy_metrics
                FOR EACH ROW
                WHEN (SELECT status FROM experiments WHERE exp_id = NEW.exp_id) IN ('completed', 'failed', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot insert accuracy_metrics for terminal experiment');
                END;

                -- Terminal immutability triggers for system_metrics table
                CREATE TRIGGER IF NOT EXISTS trg_prevent_system_metrics_update
                BEFORE UPDATE ON system_metrics
                FOR EACH ROW
                WHEN (SELECT status FROM experiments WHERE exp_id = OLD.exp_id) IN ('completed', 'failed', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot mutate system_metrics for terminal experiment');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_prevent_system_metrics_delete
                BEFORE DELETE ON system_metrics
                FOR EACH ROW
                WHEN (SELECT status FROM experiments WHERE exp_id = OLD.exp_id) IN ('completed', 'failed', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot mutate system_metrics for terminal experiment');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_prevent_system_metrics_insert
                BEFORE INSERT ON system_metrics
                FOR EACH ROW
                WHEN (SELECT status FROM experiments WHERE exp_id = NEW.exp_id) IN ('completed', 'failed', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot insert system_metrics for terminal experiment');
                END;

                -- Terminal immutability triggers for hyperparameters table
                CREATE TRIGGER IF NOT EXISTS trg_prevent_hyperparameters_update
                BEFORE UPDATE ON hyperparameters
                FOR EACH ROW
                WHEN (SELECT status FROM experiments WHERE exp_id = OLD.exp_id) IN ('completed', 'failed', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot mutate hyperparameters for terminal experiment');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_prevent_hyperparameters_delete
                BEFORE DELETE ON hyperparameters
                FOR EACH ROW
                WHEN (SELECT status FROM experiments WHERE exp_id = OLD.exp_id) IN ('completed', 'failed', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot mutate hyperparameters for terminal experiment');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_prevent_hyperparameters_insert
                BEFORE INSERT ON hyperparameters
                FOR EACH ROW
                WHEN (SELECT status FROM experiments WHERE exp_id = NEW.exp_id) IN ('completed', 'failed', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot insert hyperparameters for terminal experiment');
                END;

                -- Terminal immutability triggers for artifacts table
                CREATE TRIGGER IF NOT EXISTS trg_prevent_artifacts_update
                BEFORE UPDATE ON artifacts
                FOR EACH ROW
                WHEN (SELECT status FROM experiments WHERE exp_id = OLD.exp_id) IN ('completed', 'failed', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot mutate artifacts for terminal experiment');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_prevent_artifacts_delete
                BEFORE DELETE ON artifacts
                FOR EACH ROW
                WHEN (SELECT status FROM experiments WHERE exp_id = OLD.exp_id) IN ('completed', 'failed', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot mutate artifacts for terminal experiment');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_prevent_artifacts_insert
                BEFORE INSERT ON artifacts
                FOR EACH ROW
                WHEN (SELECT status FROM experiments WHERE exp_id = NEW.exp_id) IN ('completed', 'failed', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'Cannot insert artifacts for terminal experiment');
                END;

                -- Database-level active baseline validation triggers
                CREATE TRIGGER IF NOT EXISTS trg_validate_active_baseline_insert
                BEFORE INSERT ON active_baselines
                FOR EACH ROW
                BEGIN
                    SELECT
                        CASE
                            WHEN (SELECT status FROM experiments WHERE exp_id = NEW.exp_id) != 'completed'
                            THEN RAISE(ABORT, 'Active baseline experiment must have status completed')
                            WHEN (SELECT workload_type FROM experiments WHERE exp_id = NEW.exp_id) != NEW.workload_type
                            THEN RAISE(ABORT, 'Active baseline experiment workload_type mismatch')
                        END;
                END;

                CREATE TRIGGER IF NOT EXISTS trg_validate_active_baseline_update
                BEFORE UPDATE ON active_baselines
                FOR EACH ROW
                BEGIN
                    SELECT
                        CASE
                            WHEN (SELECT status FROM experiments WHERE exp_id = NEW.exp_id) != 'completed'
                            THEN RAISE(ABORT, 'Active baseline experiment must have status completed')
                            WHEN (SELECT workload_type FROM experiments WHERE exp_id = NEW.exp_id) != NEW.workload_type
                            THEN RAISE(ABORT, 'Active baseline experiment workload_type mismatch')
                        END;
                END;
                """
            )

    def _generate_exp_id(self, conn: sqlite3.Connection) -> str:
        """Generates project-scoped primary identifier (e.g. EXP-YYYYMMDD-NN)."""
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y%m%d")
        prefix = f"EXP-{date_str}-"
        cur = conn.execute(
            "SELECT exp_id FROM experiments WHERE exp_id LIKE ? ORDER BY exp_id DESC LIMIT 1",
            (f"{prefix}%",),
        )
        row = cur.fetchone()
        if row:
            last_id = str(row["exp_id"])
            try:
                seq = int(last_id.split("-")[-1]) + 1
            except ValueError:
                seq = 1
        else:
            seq = 1
        return f"{prefix}{seq:04d}"

    def _validate_spec_reproducibility(self, spec: ExperimentSpec) -> None:
        """Enforces mandatory reproducibility metadata invariants per ADR-0004 §2.4.E/F [P§25], [P§29.5]."""
        if not isinstance(spec.commit_sha, str) or not COMMIT_SHA_REGEX.match(
            spec.commit_sha
        ):
            raise ReproducibilityMetadataError(
                f"Invalid commit_sha {spec.commit_sha!r}: must be a 40-character hexadecimal Git commit hash."
            )
        if (
            not isinstance(spec.dataset_version, str)
            or not spec.dataset_version.strip()
        ):
            raise ReproducibilityMetadataError("dataset_version cannot be empty.")
        if (
            not isinstance(spec.seed, int)
            or isinstance(spec.seed, bool)
            or spec.seed < 0
        ):
            raise ReproducibilityMetadataError(
                f"Invalid seed {spec.seed!r}: must be a non-negative integer."
            )
        if not isinstance(spec.config_uri, str) or not spec.config_uri.strip():
            raise ReproducibilityMetadataError("config_uri cannot be empty.")
        if not isinstance(spec.config_hash, str) or not spec.config_hash.strip():
            raise ReproducibilityMetadataError("config_hash cannot be empty.")
        if (
            not isinstance(spec.hyperparameters, Mapping)
            or len(spec.hyperparameters) == 0
        ):
            raise ReproducibilityMetadataError(
                "hyperparameters mapping cannot be empty."
            )
        if (
            not isinstance(spec.input_resolution, (tuple, list))
            or len(spec.input_resolution) != 2
            or not isinstance(spec.input_resolution[0], int)
            or isinstance(spec.input_resolution[0], bool)
            or not isinstance(spec.input_resolution[1], int)
            or isinstance(spec.input_resolution[1], bool)
            or spec.input_resolution[0] <= 0
            or spec.input_resolution[1] <= 0
        ):
            raise ReproducibilityMetadataError(
                f"Invalid input_resolution {spec.input_resolution!r}: must be a pair of positive integers (width, height)."
            )
        if (
            not isinstance(spec.batch_size, int)
            or isinstance(spec.batch_size, bool)
            or spec.batch_size <= 0
        ):
            raise ReproducibilityMetadataError(
                f"Invalid batch_size {spec.batch_size!r}: must be a positive integer."
            )
        if not isinstance(spec.target, ExecutionTarget):
            raise ReproducibilityMetadataError(
                f"Invalid execution target {spec.target!r}: must be an ExecutionTarget enum instance."
            )
        if not isinstance(spec.workload_type, WorkloadType):
            raise ReproducibilityMetadataError(
                f"Invalid workload_type {spec.workload_type!r}: must be a WorkloadType enum instance."
            )
        if not isinstance(spec.name, str) or not spec.name.strip():
            raise ReproducibilityMetadataError("name cannot be empty.")
        if not isinstance(spec.hypothesis, str) or not spec.hypothesis.strip():
            raise ReproducibilityMetadataError("hypothesis cannot be empty.")
        if not isinstance(spec.model_name, str) or not spec.model_name.strip():
            raise ReproducibilityMetadataError("model_name cannot be empty.")
        if not isinstance(spec.model_variant, str) or not spec.model_variant.strip():
            raise ReproducibilityMetadataError("model_variant cannot be empty.")
        if not isinstance(spec.precision, str) or not spec.precision.strip():
            raise ReproducibilityMetadataError("precision cannot be empty.")

        # Validate accuracy_metric_requirements
        if spec.workload_type in (
            WorkloadType.EVALUATION,
            WorkloadType.OPTIMIZATION,
            WorkloadType.BENCHMARK,
        ):
            if (
                not spec.accuracy_metric_requirements
                or len(spec.accuracy_metric_requirements) == 0
            ):
                raise ReproducibilityMetadataError(
                    f"accuracy_metric_requirements cannot be empty for workload {spec.workload_type.value!r}: "
                    "applicable accuracy evidence must be explicitly declared."
                )
            for req in spec.accuracy_metric_requirements:
                if not isinstance(req, str) or not req.strip():
                    raise ReproducibilityMetadataError(
                        f"Invalid metric requirement in accuracy_metric_requirements: {req!r}. "
                        "All declared metrics must be non-empty strings."
                    )
        elif spec.workload_type == WorkloadType.TRAINING:
            mand = {"map_50", "map_50_95", "precision", "recall"}
            if not mand.issubset(set(spec.accuracy_metric_requirements)):
                raise ReproducibilityMetadataError(
                    f"Training workload requires accuracy_metric_requirements to contain {sorted(mand)}, "
                    f"got {spec.accuracy_metric_requirements!r}."
                )

    def create_experiment(self, spec: ExperimentSpec) -> ExperimentRecord:
        """Create a new experiment run in PROPOSED status [P§25]."""
        self._validate_spec_reproducibility(spec)

        # Validate approval_ref format if present
        if spec.approval_ref and not APPROVAL_REF_REGEX.match(spec.approval_ref):
            raise ApprovalReferenceFormatError(
                f"approval_ref {spec.approval_ref!r} does not match format '{{operation_id}}:{{request_id}}'"
            )

        now_str = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # Validate parent_exp_id exists if specified
                if spec.parent_exp_id:
                    cur = conn.execute(
                        "SELECT exp_id FROM experiments WHERE exp_id = ?",
                        (spec.parent_exp_id,),
                    )
                    if not cur.fetchone():
                        raise RelationalIntegrityError(
                            f"Parent experiment {spec.parent_exp_id!r} does not exist."
                        )

                # Validate baseline_id exists if specified
                if spec.baseline_id:
                    cur = conn.execute(
                        "SELECT exp_id FROM experiments WHERE exp_id = ?",
                        (spec.baseline_id,),
                    )
                    if not cur.fetchone():
                        raise RelationalIntegrityError(
                            f"Baseline experiment {spec.baseline_id!r} does not exist."
                        )

                exp_id = self._generate_exp_id(conn)
                w, h = spec.input_resolution

                reqs_json = json.dumps(
                    list(spec.accuracy_metric_requirements), ensure_ascii=False
                )

                conn.execute(
                    """
                    INSERT INTO experiments (
                        exp_id, name, hypothesis, workload_type, target,
                        model_name, model_variant, commit_sha, dataset_version,
                        seed, batch_size, input_resolution_w, input_resolution_h,
                        precision, config_uri, config_hash, accuracy_metric_requirements,
                        parent_exp_id, baseline_id, approval_ref, status, created_at,
                        started_at, completed_at, job_id, error_message, notes
                    ) VALUES (
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        exp_id,
                        spec.name,
                        spec.hypothesis,
                        spec.workload_type.value,
                        spec.target.value,
                        spec.model_name,
                        spec.model_variant,
                        spec.commit_sha,
                        spec.dataset_version,
                        spec.seed,
                        spec.batch_size,
                        w,
                        h,
                        spec.precision,
                        spec.config_uri,
                        spec.config_hash,
                        reqs_json,
                        spec.parent_exp_id,
                        spec.baseline_id,
                        spec.approval_ref,
                        ExperimentStatus.PROPOSED.value,
                        now_str,
                        None,
                        None,
                        None,
                        None,
                        None,
                    ),
                )

                # Insert hyperparameters if present
                if spec.hyperparameters:
                    for k, v in spec.hyperparameters.items():
                        val_str = (
                            json.dumps(v, ensure_ascii=False)
                            if isinstance(v, (dict, list))
                            else str(v)
                        )
                        conn.execute(
                            "INSERT INTO hyperparameters (exp_id, param_key, param_value) VALUES (?, ?, ?)",
                            (exp_id, str(k), val_str),
                        )

                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        record = self.get_experiment(exp_id)
        if not record:
            raise LedgerError(f"Failed to retrieve newly created experiment {exp_id}")
        return record

    def transition_status(
        self,
        exp_id: str,
        new_status: ExperimentStatus,
        job_id: str | None = None,
        error_message: str | None = None,
    ) -> ExperimentRecord:
        """Transition experiment lifecycle status per ADR-0004 §2.4.B."""
        now_str = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cur = conn.execute(
                    "SELECT status, started_at, job_id FROM experiments WHERE exp_id = ?",
                    (exp_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise LedgerError(f"Experiment {exp_id!r} not found.")

                current_status = ExperimentStatus(row["status"])
                validate_experiment_transition(current_status, new_status)

                started_at = row["started_at"]
                if new_status == ExperimentStatus.RUNNING and not started_at:
                    started_at = now_str

                final_job_id = job_id or row["job_id"]

                conn.execute(
                    """
                    UPDATE experiments
                    SET status = ?, started_at = ?, job_id = ?, error_message = COALESCE(?, error_message)
                    WHERE exp_id = ?
                    """,
                    (new_status.value, started_at, final_job_id, error_message, exp_id),
                )
                conn.execute("COMMIT;")
            except sqlite3.DatabaseError as err:
                conn.execute("ROLLBACK;")
                if "terminal" in str(err).lower():
                    raise ExperimentImmutabilityError(str(err)) from err
                raise
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        record = self.get_experiment(exp_id)
        if not record:
            raise LedgerError(f"Failed to retrieve experiment {exp_id}")
        return record

    def complete_experiment(
        self,
        exp_id: str,
        accuracy: AccuracyMetrics,
        system: SystemMetrics,
        artifacts: ArtifactManifest,
        notes: str | None = None,
    ) -> ExperimentRecord:
        """Atomically record results and mark experiment as COMPLETED within a single transaction."""
        now_str = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cur = conn.execute(
                    "SELECT status, workload_type, accuracy_metric_requirements FROM experiments WHERE exp_id = ?",
                    (exp_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise LedgerError(f"Experiment {exp_id!r} not found.")

                current_status = ExperimentStatus(row["status"])
                validate_experiment_transition(
                    current_status, ExperimentStatus.COMPLETED
                )

                workload_type = WorkloadType(row["workload_type"])
                raw_reqs = row["accuracy_metric_requirements"]
                reqs = tuple(json.loads(raw_reqs)) if raw_reqs else ()
                # Validate metric completeness per ADR-0004 §2.4.C
                validate_metric_completeness(
                    workload_type,
                    accuracy,
                    system,
                    accuracy_metric_requirements=reqs,
                )

                # Persist accuracy metrics
                acc_dict: dict[str, float] = {}
                if accuracy.map_50 is not None:
                    acc_dict["map_50"] = accuracy.map_50
                if accuracy.map_50_95 is not None:
                    acc_dict["map_50_95"] = accuracy.map_50_95
                if accuracy.precision is not None:
                    acc_dict["precision"] = accuracy.precision
                if accuracy.recall is not None:
                    acc_dict["recall"] = accuracy.recall
                if accuracy.f1_score is not None:
                    acc_dict["f1_score"] = accuracy.f1_score
                if accuracy.additional_metrics:
                    acc_dict.update(accuracy.additional_metrics)

                for mk, mv in acc_dict.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO accuracy_metrics (exp_id, metric_key, metric_value) VALUES (?, ?, ?)",
                        (exp_id, mk, float(mv)),
                    )

                # Persist system metrics (distinguishing missing from zero per PM Finding 4)
                sys_dict: dict[str, float] = {}
                if system.peak_vram_mb is not None:
                    sys_dict["peak_vram_mb"] = system.peak_vram_mb
                if system.peak_ram_mb is not None:
                    sys_dict["peak_ram_mb"] = system.peak_ram_mb
                if system.gpu_hours is not None:
                    sys_dict["gpu_hours"] = system.gpu_hours
                if system.training_time_seconds is not None:
                    sys_dict["training_time_seconds"] = system.training_time_seconds
                if system.latency_e2e_ms is not None:
                    sys_dict["latency_e2e_ms"] = system.latency_e2e_ms
                if system.latency_inference_ms is not None:
                    sys_dict["latency_inference_ms"] = system.latency_inference_ms
                if system.throughput_fps is not None:
                    sys_dict["throughput_fps"] = system.throughput_fps
                if system.power_draw_watts is not None:
                    sys_dict["power_draw_watts"] = system.power_draw_watts

                for sk, sv in sys_dict.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO system_metrics (exp_id, metric_key, metric_value) VALUES (?, ?, ?)",
                        (exp_id, sk, float(sv)),
                    )

                # Persist artifacts
                art_items: list[tuple[str, str, str | None]] = []
                if artifacts.weights_uri:
                    art_items.append(("weights", artifacts.weights_uri, None))
                if artifacts.engine_uri:
                    art_items.append(("engine", artifacts.engine_uri, None))
                if artifacts.plots_uri:
                    art_items.append(("plots", artifacts.plots_uri, None))
                if artifacts.failure_analysis_ref:
                    art_items.append(
                        ("failure_analysis", artifacts.failure_analysis_ref, None)
                    )

                for atype, auri, _ in art_items:
                    chk = (
                        artifacts.checksums.get(atype) if artifacts.checksums else None
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO artifacts (exp_id, artifact_type, uri, checksum) VALUES (?, ?, ?, ?)",
                        (exp_id, atype, auri, chk),
                    )

                conn.execute(
                    """
                    UPDATE experiments
                    SET status = ?, completed_at = ?, notes = COALESCE(?, notes)
                    WHERE exp_id = ?
                    """,
                    (ExperimentStatus.COMPLETED.value, now_str, notes, exp_id),
                )
                conn.execute("COMMIT;")
            except sqlite3.DatabaseError as err:
                conn.execute("ROLLBACK;")
                if "terminal" in str(err).lower():
                    raise ExperimentImmutabilityError(str(err)) from err
                raise
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        record = self.get_experiment(exp_id)
        if not record:
            raise LedgerError(f"Failed to retrieve completed experiment {exp_id}")
        return record

    def fail_experiment(self, exp_id: str, error_message: str) -> ExperimentRecord:
        """Mark an experiment as FAILED with diagnostic error message (atomic & immutable thereafter)."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cur = conn.execute(
                    "SELECT status FROM experiments WHERE exp_id = ?", (exp_id,)
                )
                row = cur.fetchone()
                if not row:
                    raise LedgerError(f"Experiment {exp_id!r} not found.")

                current_status = ExperimentStatus(row["status"])
                validate_experiment_transition(current_status, ExperimentStatus.FAILED)

                conn.execute(
                    """
                    UPDATE experiments
                    SET status = ?, completed_at = ?, error_message = ?
                    WHERE exp_id = ?
                    """,
                    (ExperimentStatus.FAILED.value, now_str, error_message, exp_id),
                )
                conn.execute("COMMIT;")
            except sqlite3.DatabaseError as err:
                conn.execute("ROLLBACK;")
                if "terminal" in str(err).lower():
                    raise ExperimentImmutabilityError(str(err)) from err
                raise
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        record = self.get_experiment(exp_id)
        if not record:
            raise LedgerError(f"Failed to retrieve failed experiment {exp_id}")
        return record

    def cancel_experiment(self, exp_id: str, reason: str) -> ExperimentRecord:
        """Mark an experiment as CANCELLED with explanation (atomic & immutable thereafter)."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cur = conn.execute(
                    "SELECT status FROM experiments WHERE exp_id = ?", (exp_id,)
                )
                row = cur.fetchone()
                if not row:
                    raise LedgerError(f"Experiment {exp_id!r} not found.")

                current_status = ExperimentStatus(row["status"])
                validate_experiment_transition(
                    current_status, ExperimentStatus.CANCELLED
                )

                conn.execute(
                    """
                    UPDATE experiments
                    SET status = ?, completed_at = ?, error_message = ?
                    WHERE exp_id = ?
                    """,
                    (ExperimentStatus.CANCELLED.value, now_str, reason, exp_id),
                )
                conn.execute("COMMIT;")
            except sqlite3.DatabaseError as err:
                conn.execute("ROLLBACK;")
                if "terminal" in str(err).lower():
                    raise ExperimentImmutabilityError(str(err)) from err
                raise
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        record = self.get_experiment(exp_id)
        if not record:
            raise LedgerError(f"Failed to retrieve cancelled experiment {exp_id}")
        return record

    def set_active_baseline(self, exp_id: str, workload_type: WorkloadType) -> None:
        """Designates an experiment as the active baseline for a workload class.

        Precondition: exp_id must reference an existing COMPLETED experiment whose
        workload_type strictly matches the designated workload class.
        """
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cur = conn.execute(
                    "SELECT status, workload_type FROM experiments WHERE exp_id = ?",
                    (exp_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise RelationalIntegrityError(
                        f"Baseline candidate {exp_id!r} does not exist."
                    )

                status = ExperimentStatus(row["status"])
                if status != ExperimentStatus.COMPLETED:
                    raise RelationalIntegrityError(
                        f"Cannot set experiment {exp_id!r} in non-completed status {status.value!r} as active baseline."
                    )

                exp_workload = WorkloadType(row["workload_type"])
                if exp_workload != workload_type:
                    raise RelationalIntegrityError(
                        f"Experiment {exp_id!r} workload {exp_workload.value!r} does not match "
                        f"target baseline workload {workload_type.value!r}."
                    )

                conn.execute(
                    """
                    INSERT OR REPLACE INTO active_baselines (workload_type, exp_id, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (workload_type.value, exp_id, now_str),
                )
                conn.execute("COMMIT;")
            except sqlite3.DatabaseError as err:
                conn.execute("ROLLBACK;")
                if "active baseline" in str(err).lower():
                    raise RelationalIntegrityError(str(err)) from err
                raise
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def get_baseline(
        self, workload_type: WorkloadType | None = None
    ) -> ExperimentRecord | None:
        """Retrieve the designated active baseline record for the specified workload [P§29.2]."""
        with self._get_connection() as conn:
            if workload_type is not None:
                cur = conn.execute(
                    "SELECT exp_id FROM active_baselines WHERE workload_type = ?",
                    (workload_type.value,),
                )
            else:
                cur = conn.execute(
                    "SELECT exp_id FROM active_baselines ORDER BY updated_at DESC LIMIT 1"
                )
            row = cur.fetchone()
            if not row:
                return None
            return self.get_experiment(str(row["exp_id"]))

    def get_experiment(self, exp_id: str) -> ExperimentRecord | None:
        """Retrieve a single experiment record by ID."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM experiments WHERE exp_id = ?", (exp_id,))
            row = cur.fetchone()
            if not row:
                return None

            # Load hyperparameters
            h_cur = conn.execute(
                "SELECT param_key, param_value FROM hyperparameters WHERE exp_id = ?",
                (exp_id,),
            )
            h_dict: dict[str, Any] = {}
            for h in h_cur.fetchall():
                val = h["param_value"]
                try:
                    h_dict[h["param_key"]] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    h_dict[h["param_key"]] = val

            # Load accuracy metrics
            a_cur = conn.execute(
                "SELECT metric_key, metric_value FROM accuracy_metrics WHERE exp_id = ?",
                (exp_id,),
            )
            a_rows = {
                r["metric_key"]: float(r["metric_value"]) for r in a_cur.fetchall()
            }
            accuracy: AccuracyMetrics | None = None
            if a_rows:
                std_keys = {"map_50", "map_50_95", "precision", "recall", "f1_score"}
                add_metrics = {k: v for k, v in a_rows.items() if k not in std_keys}
                accuracy = AccuracyMetrics(
                    map_50=a_rows.get("map_50"),
                    map_50_95=a_rows.get("map_50_95"),
                    precision=a_rows.get("precision"),
                    recall=a_rows.get("recall"),
                    f1_score=a_rows.get("f1_score"),
                    additional_metrics=add_metrics if add_metrics else None,
                )

            # Load system metrics (preserving missing evidence as None per PM Finding 4)
            s_cur = conn.execute(
                "SELECT metric_key, metric_value FROM system_metrics WHERE exp_id = ?",
                (exp_id,),
            )
            s_rows = {
                r["metric_key"]: float(r["metric_value"]) for r in s_cur.fetchall()
            }
            system: SystemMetrics | None = None
            if s_rows:
                system = SystemMetrics(
                    peak_vram_mb=s_rows.get("peak_vram_mb"),
                    peak_ram_mb=s_rows.get("peak_ram_mb"),
                    gpu_hours=s_rows.get("gpu_hours"),
                    training_time_seconds=s_rows.get("training_time_seconds"),
                    latency_e2e_ms=s_rows.get("latency_e2e_ms"),
                    latency_inference_ms=s_rows.get("latency_inference_ms"),
                    throughput_fps=s_rows.get("throughput_fps"),
                    power_draw_watts=s_rows.get("power_draw_watts"),
                )

            # Load artifacts
            art_cur = conn.execute(
                "SELECT artifact_type, uri, checksum FROM artifacts WHERE exp_id = ?",
                (exp_id,),
            )
            art_rows = {
                r["artifact_type"]: (r["uri"], r["checksum"])
                for r in art_cur.fetchall()
            }
            artifacts: ArtifactManifest | None = None
            if art_rows:
                chk_map: dict[str, str] = {}
                for k, (_, chk) in art_rows.items():
                    if chk:
                        chk_map[k] = chk
                artifacts = ArtifactManifest(
                    weights_uri=art_rows.get("weights", (None, None))[0],
                    engine_uri=art_rows.get("engine", (None, None))[0],
                    plots_uri=art_rows.get("plots", (None, None))[0],
                    failure_analysis_ref=art_rows.get("failure_analysis", (None, None))[
                        0
                    ],
                    checksums=chk_map if chk_map else None,
                )

            raw_reqs = row["accuracy_metric_requirements"]
            reqs = tuple(json.loads(raw_reqs)) if raw_reqs else ()

            spec = ExperimentSpec(
                name=str(row["name"]),
                hypothesis=str(row["hypothesis"]),
                workload_type=WorkloadType(row["workload_type"]),
                target=ExecutionTarget(row["target"]),
                model_name=str(row["model_name"]),
                model_variant=str(row["model_variant"]),
                commit_sha=str(row["commit_sha"]),
                dataset_version=str(row["dataset_version"]),
                seed=int(row["seed"]),
                batch_size=int(row["batch_size"]),
                input_resolution=(
                    int(row["input_resolution_w"]),
                    int(row["input_resolution_h"]),
                ),
                precision=str(row["precision"]),
                config_uri=str(row["config_uri"]),
                config_hash=str(row["config_hash"]),
                accuracy_metric_requirements=reqs,
                parent_exp_id=row["parent_exp_id"],
                baseline_id=row["baseline_id"],
                approval_ref=row["approval_ref"],
                hyperparameters=h_dict,
            )

            return ExperimentRecord(
                exp_id=str(row["exp_id"]),
                spec=spec,
                status=ExperimentStatus(row["status"]),
                created_at=str(row["created_at"]),
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                job_id=row["job_id"],
                accuracy_metrics=accuracy,
                system_metrics=system,
                artifacts=artifacts,
                error_message=row["error_message"],
                notes=row["notes"],
            )

    def list_experiments(
        self,
        status: ExperimentStatus | None = None,
        workload_type: WorkloadType | None = None,
        model_name: str | None = None,
        dataset_version: str | None = None,
    ) -> tuple[ExperimentRecord, ...]:
        """Query experiment runs matching specified filter criteria."""
        query = "SELECT exp_id FROM experiments WHERE 1=1"
        params: list[Any] = []
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        if workload_type is not None:
            query += " AND workload_type = ?"
            params.append(workload_type.value)
        if model_name is not None:
            query += " AND model_name = ?"
            params.append(model_name)
        if dataset_version is not None:
            query += " AND dataset_version = ?"
            params.append(dataset_version)

        query += " ORDER BY created_at ASC"

        with self._get_connection() as conn:
            cur = conn.execute(query, params)
            exp_ids = [str(r["exp_id"]) for r in cur.fetchall()]

        records: list[ExperimentRecord] = []
        for eid in exp_ids:
            rec = self.get_experiment(eid)
            if rec:
                records.append(rec)
        return tuple(records)
