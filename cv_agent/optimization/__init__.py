"""cv_agent.optimization — Optimization and Deployment Subsystem for ADR-0012."""

from __future__ import annotations

from cv_agent.optimization.canonicalization import (
    compute_artifact_lineage_hash,
    compute_deployment_payload_hash,
    compute_optimization_payload_hash,
    extract_deployment_operation_payload,
    extract_optimization_operation_payload,
)
from cv_agent.optimization.contracts import (
    CalibrationDatasetBinding,
    DeploymentPlan,
    DeploymentRecord,
    DeploymentTarget,
    DynamicShapeProfile,
    HealthCheckSpec,
    OptimizationPlan,
    OptimizationResult,
    OptimizedModelArtifact,
    ReadinessProbeResult,
)
from cv_agent.optimization.deployment import (
    DefaultDeploymentManager,
    DeploymentManager,
)
from cv_agent.optimization.errors import (
    CalibrationLeakageError,
    DeploymentApprovalRequiredError,
    DeploymentValidationError,
    HardwareMismatchError,
    OptimizationError,
    OptimizationExecutionError,
    OptimizationValidationError,
    OptimizationVerificationError,
    ReadinessProbeFailureError,
    RollbackExecutionError,
)
from cv_agent.optimization.optimizer import (
    DefaultModelOptimizer,
    ModelOptimizer,
)
from cv_agent.optimization.types import (
    CalibrationAlgorithm,
    DeploymentId,
    DeploymentRuntime,
    DeploymentStatus,
    OptimizationPlanId,
    OptimizationPrecision,
    OptimizationTechnique,
    OptimizedArtifactId,
    validate_deployment_id,
    validate_optimization_plan_id,
    validate_optimized_artifact_id,
)
from cv_agent.optimization.verifier import (
    DefaultOptimizationVerifier,
    OptimizationVerifier,
)

__all__ = [
    "CalibrationAlgorithm",
    "CalibrationDatasetBinding",
    "CalibrationLeakageError",
    "DefaultDeploymentManager",
    "DefaultModelOptimizer",
    "DefaultOptimizationVerifier",
    "DeploymentApprovalRequiredError",
    "DeploymentId",
    "DeploymentManager",
    "DeploymentPlan",
    "DeploymentRecord",
    "DeploymentRuntime",
    "DeploymentStatus",
    "DeploymentTarget",
    "DeploymentValidationError",
    # Contracts
    "DynamicShapeProfile",
    "HardwareMismatchError",
    "HealthCheckSpec",
    # Subsystem Engines & Protocols
    "ModelOptimizer",
    # Errors
    "OptimizationError",
    "OptimizationExecutionError",
    "OptimizationPlan",
    # Identifiers & Enums
    "OptimizationPlanId",
    "OptimizationPrecision",
    "OptimizationResult",
    "OptimizationTechnique",
    "OptimizationValidationError",
    "OptimizationVerificationError",
    "OptimizationVerifier",
    "OptimizedArtifactId",
    "OptimizedModelArtifact",
    "ReadinessProbeFailureError",
    "ReadinessProbeResult",
    "RollbackExecutionError",
    "compute_artifact_lineage_hash",
    "compute_deployment_payload_hash",
    "compute_optimization_payload_hash",
    "extract_deployment_operation_payload",
    # Canonicalization
    "extract_optimization_operation_payload",
    "validate_deployment_id",
    "validate_optimization_plan_id",
    "validate_optimized_artifact_id",
]
