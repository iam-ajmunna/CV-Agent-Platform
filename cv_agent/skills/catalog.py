"""cv_agent.skills.catalog — Authoritative declarative NVIDIA and internal skill catalog.

Implements ADR-0007 §3.4 declarative external skill inventory per [P§15] and [P§29.9].
All skills bind strictly to registered ADR-0001 capabilities per Blocker B1 resolution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cv_agent.domain.identifiers import CapabilityId, SkillId, ToolId

if TYPE_CHECKING:
    from cv_agent.capabilities.registry import CapabilityRegistry
from cv_agent.skills.contracts import (
    HardwareClass,
    SkillDescriptor,
    SkillProvenance,
    TargetRequirements,
)
from cv_agent.skills.registry import DefaultSkillRegistry
from cv_agent.tools.contracts import TargetType

# 1. TensorRT Inference Optimization
TENSORRT_OPTIMIZE_SKILL = SkillDescriptor(
    skill_id=SkillId("nvidia.tensorrt.inference_optimization"),
    name="TensorRT Inference Optimization",
    description="Build and optimize TensorRT execution engine from ONNX model artifact.",
    version="1.0.0",
    provenance=SkillProvenance.EXTERNAL,
    vendor="nvidia",
    satisfies_capabilities=(CapabilityId("cv.deployment.optimization"),),
    required_tools=(ToolId("tool.nvidia.trtexec"),),
    target_requirements=TargetRequirements(
        allowed_targets=(TargetType.REMOTE,),
        required_hardware=HardwareClass.NVIDIA_GPU,
        min_cuda_compute="7.0",
    ),
    parameter_schema={
        "type": "object",
        "required": ["model_path", "precision"],
        "properties": {
            "model_path": {"type": "string"},
            "precision": {"type": "string"},
        },
    },
    procedural_recipe="Run trtexec --onnx={model_path} --saveEngine={output_path} --fp16={use_fp16}",
    timeout_seconds=900,
    requires_approval=False,
)

# 2. NVIDIA Model Optimizer PTQ
MODEL_OPTIMIZER_PTQ_SKILL = SkillDescriptor(
    skill_id=SkillId("nvidia.model_optimizer.ptq"),
    name="NVIDIA Model Optimizer Post-Training Quantization",
    description="Perform calibration-driven Post-Training Quantization (INT8/FP8) via NVIDIA ModelOpt.",
    version="1.0.0",
    provenance=SkillProvenance.EXTERNAL,
    vendor="nvidia",
    satisfies_capabilities=(CapabilityId("cv.deployment.optimization"),),
    required_tools=(ToolId("tool.nvidia.modelopt"),),
    target_requirements=TargetRequirements(
        allowed_targets=(TargetType.REMOTE,),
        required_hardware=HardwareClass.NVIDIA_GPU,
        min_cuda_compute="8.0",
    ),
    parameter_schema={
        "type": "object",
        "required": ["model_path", "calibration_data"],
        "properties": {
            "model_path": {"type": "string"},
            "calibration_data": {"type": "string"},
        },
    },
    procedural_recipe="Run modelopt.torch.quantize(model, quant_cfg=INT8_DEFAULT) with calibration dataset",
    timeout_seconds=1200,
    requires_approval=False,
)

# 3. NVIDIA Model Optimizer QAT
MODEL_OPTIMIZER_QAT_SKILL = SkillDescriptor(
    skill_id=SkillId("nvidia.model_optimizer.qat"),
    name="NVIDIA Model Optimizer Quantization-Aware Training",
    description="Apply Quantization-Aware Training fine-tuning via NVIDIA ModelOpt to recover accuracy.",
    version="1.0.0",
    provenance=SkillProvenance.EXTERNAL,
    vendor="nvidia",
    satisfies_capabilities=(CapabilityId("cv.deployment.optimization"),),
    required_tools=(ToolId("tool.nvidia.modelopt"),),
    target_requirements=TargetRequirements(
        allowed_targets=(TargetType.REMOTE,),
        required_hardware=HardwareClass.NVIDIA_GPU,
        min_cuda_compute="8.0",
    ),
    parameter_schema={
        "type": "object",
        "required": ["model_path", "training_data", "epochs"],
        "properties": {
            "model_path": {"type": "string"},
            "training_data": {"type": "string"},
            "epochs": {"type": "integer"},
        },
    },
    procedural_recipe="Run modelopt.torch.quantize(model, quant_cfg=QAT_CONFIG) and fine-tune",
    timeout_seconds=3600,
    requires_approval=True,  # Fine-tuning consumes substantial GPU resources
)

# 4. NVIDIA TAO Transfer Learning
TAO_TRANSFER_LEARNING_SKILL = SkillDescriptor(
    skill_id=SkillId("nvidia.tao.transfer_learning"),
    name="NVIDIA TAO Transfer Learning Toolkit",
    description="Fine-tune pre-trained vision models on custom datasets using NVIDIA TAO toolkit.",
    version="1.0.0",
    provenance=SkillProvenance.EXTERNAL,
    vendor="nvidia",
    satisfies_capabilities=(CapabilityId("cv.training.design"),),
    required_tools=(ToolId("tool.nvidia.tao"),),
    target_requirements=TargetRequirements(
        allowed_targets=(TargetType.REMOTE,),
        required_hardware=HardwareClass.NVIDIA_GPU,
        min_cuda_compute="7.0",
    ),
    parameter_schema={
        "type": "object",
        "required": ["spec_file", "dataset_path"],
        "properties": {
            "spec_file": {"type": "string"},
            "dataset_path": {"type": "string"},
        },
    },
    procedural_recipe="Invoke tao model <network> train -e {spec_file} -r {output_dir}",
    prerequisites=("docker", "nvidia-container-toolkit"),
    timeout_seconds=7200,
    requires_approval=True,  # Heavy compute action
)

# 5. NVIDIA DeepStream Pipeline
DEEPSTREAM_PIPELINE_SKILL = SkillDescriptor(
    skill_id=SkillId("nvidia.deepstream.pipeline"),
    name="NVIDIA DeepStream Pipeline Deployment",
    description="Configure and launch hardware-accelerated GStreamer video analytics pipeline.",
    version="1.0.0",
    provenance=SkillProvenance.EXTERNAL,
    vendor="nvidia",
    satisfies_capabilities=(CapabilityId("cv.deployment.validation"),),
    required_tools=(ToolId("tool.nvidia.deepstream_app"),),
    target_requirements=TargetRequirements(
        allowed_targets=(TargetType.REMOTE,),
        required_hardware=HardwareClass.NVIDIA_GPU,  # Jetson or dGPU per ADR-0007 §3.4
    ),
    parameter_schema={
        "type": "object",
        "required": ["config_file"],
        "properties": {
            "config_file": {"type": "string"},
        },
    },
    procedural_recipe="Execute deepstream-app -c {config_file}",
    prerequisites=("gstreamer", "deepstream_sdk"),
    timeout_seconds=600,
    requires_approval=False,
)

# 6. NVIDIA DALI Data Pipeline
DALI_DATA_PIPELINE_SKILL = SkillDescriptor(
    skill_id=SkillId("nvidia.dali.data_pipeline"),
    name="NVIDIA DALI Data Loading Pipeline",
    description="High-performance GPU-accelerated image decoding and augmentation pipeline.",
    version="1.0.0",
    provenance=SkillProvenance.EXTERNAL,
    vendor="nvidia",
    satisfies_capabilities=(CapabilityId("cv.dataset.audit"),),
    required_tools=(ToolId("tool.nvidia.dali"),),
    target_requirements=TargetRequirements(
        allowed_targets=(TargetType.REMOTE,),
        required_hardware=HardwareClass.NVIDIA_GPU,
        min_cuda_compute="7.0",
    ),
    parameter_schema={
        "type": "object",
        "required": ["batch_size", "image_dir"],
        "properties": {
            "batch_size": {"type": "integer"},
            "image_dir": {"type": "string"},
        },
    },
    procedural_recipe="Construct nvidia.dali.pipeline.Pipeline and execute GPU batch loader",
    timeout_seconds=600,
    requires_approval=False,
)

# 7. NVIDIA Triton Model Serving
TRITON_SERVING_SKILL = SkillDescriptor(
    skill_id=SkillId("nvidia.triton.model_serving"),
    name="NVIDIA Triton Model Serving",
    description="Deploy and serve optimized model engines with dynamic batching on Triton Server.",
    version="1.0.0",
    provenance=SkillProvenance.EXTERNAL,
    vendor="nvidia",
    satisfies_capabilities=(CapabilityId("cv.deployment.validation"),),
    required_tools=(ToolId("tool.nvidia.tritonserver"),),
    target_requirements=TargetRequirements(
        allowed_targets=(TargetType.REMOTE, TargetType.MCP_ENDPOINT),
        required_hardware=HardwareClass.NVIDIA_GPU,
    ),
    parameter_schema={
        "type": "object",
        "required": ["model_repository", "model_name"],
        "properties": {
            "model_repository": {"type": "string"},
            "model_name": {"type": "string"},
        },
    },
    procedural_recipe="Launch tritonserver --model-repository={model_repository} --strict-model-config=true",
    timeout_seconds=1800,
    requires_approval=True,  # Deploying a network service
)

# 8. NVIDIA CUDA Profiling via Nsight Systems
CUDA_PROFILING_SKILL = SkillDescriptor(
    skill_id=SkillId("nvidia.cuda_agent.kernel_profiling"),
    name="NVIDIA Nsight Systems Kernel Profiling",
    description="Profile CUDA kernel timelines, memory throughput, and host-device transfer bottlenecks.",
    version="1.0.0",
    provenance=SkillProvenance.EXTERNAL,
    vendor="nvidia",
    satisfies_capabilities=(CapabilityId("platform.optimization"),),
    required_tools=(ToolId("tool.nvidia.nsys"),),
    target_requirements=TargetRequirements(
        allowed_targets=(TargetType.REMOTE,),
        required_hardware=HardwareClass.NVIDIA_GPU,
        min_cuda_compute="7.0",
    ),
    parameter_schema={
        "type": "object",
        "required": ["command"],
        "properties": {
            "command": {"type": "string"},
        },
    },
    procedural_recipe="Execute nsys profile --trace=cuda,nvtx,osrt --output={output_file} {command}",
    timeout_seconds=900,
    requires_approval=False,
)

# 9. NVIDIA Jetson Hardware Profiling via Tegrastats
JETSON_PROFILING_SKILL = SkillDescriptor(
    skill_id=SkillId("nvidia.jetson.hardware_profiling"),
    name="NVIDIA Jetson Power & Thermal Profiling",
    description="Capture tegrastats power consumption, thermal sensor readings, and EMC frequency.",
    version="1.0.0",
    provenance=SkillProvenance.EXTERNAL,
    vendor="nvidia",
    satisfies_capabilities=(CapabilityId("platform.optimization"),),
    required_tools=(ToolId("tool.nvidia.tegrastats"),),
    target_requirements=TargetRequirements(
        allowed_targets=(TargetType.REMOTE,),
        required_hardware=HardwareClass.JETSON,
    ),
    parameter_schema={
        "type": "object",
        "required": ["interval_ms"],
        "properties": {
            "interval_ms": {"type": "integer"},
        },
    },
    procedural_recipe="Execute tegrastats --interval {interval_ms} --logfile {log_path}",
    timeout_seconds=600,
    requires_approval=False,
)

# 10. Model Architecture Visualization & Inspection
MODEL_INSPECTION_VISUALIZE_SKILL = SkillDescriptor(
    skill_id=SkillId("nvidia.model_inspection.visualize"),
    name="Model Architecture Visualization & Inspection",
    description="Inspect ONNX graph nodes, tensor shapes, and operators for architecture verification.",
    version="1.0.0",
    provenance=SkillProvenance.EXTERNAL,
    vendor="nvidia",
    satisfies_capabilities=(CapabilityId("cv.model.inspection"),),
    required_tools=(ToolId("tool.netron"), ToolId("tool.onnx")),
    target_requirements=TargetRequirements(
        allowed_targets=(TargetType.LOCAL, TargetType.REMOTE),
        required_hardware=HardwareClass.ANY,
    ),
    parameter_schema={
        "type": "object",
        "required": ["model_path"],
        "properties": {
            "model_path": {"type": "string"},
        },
    },
    procedural_recipe="Load model using onnx.load, inspect nodes and generate Netron visualization spec",
    timeout_seconds=300,
    requires_approval=False,
)

# Internal baseline skill for model inspection
INTERNAL_ONNX_CHECK_SKILL = SkillDescriptor(
    skill_id=SkillId("internal.model.onnx_validate"),
    name="Internal ONNX Model Graph Validation",
    description="Perform local structural validation and op-set compliance check on an ONNX model.",
    version="1.0.0",
    provenance=SkillProvenance.INTERNAL,
    satisfies_capabilities=(CapabilityId("cv.model.inspection"),),
    required_tools=(ToolId("tool.onnx"),),
    target_requirements=TargetRequirements(
        allowed_targets=(TargetType.LOCAL, TargetType.REMOTE),
        required_hardware=HardwareClass.ANY,
    ),
    parameter_schema={
        "type": "object",
        "required": ["model_path"],
        "properties": {
            "model_path": {"type": "string"},
        },
    },
    procedural_recipe="Validate model graph integrity via onnx.checker.check_model()",
    timeout_seconds=120,
    requires_approval=False,
)

AUTHORITATIVE_NVIDIA_SKILLS: tuple[SkillDescriptor, ...] = (
    TENSORRT_OPTIMIZE_SKILL,
    MODEL_OPTIMIZER_PTQ_SKILL,
    MODEL_OPTIMIZER_QAT_SKILL,
    TAO_TRANSFER_LEARNING_SKILL,
    DEEPSTREAM_PIPELINE_SKILL,
    DALI_DATA_PIPELINE_SKILL,
    TRITON_SERVING_SKILL,
    CUDA_PROFILING_SKILL,
    JETSON_PROFILING_SKILL,
    MODEL_INSPECTION_VISUALIZE_SKILL,
)

ALL_AUTHORITATIVE_SKILLS: tuple[SkillDescriptor, ...] = (
    *AUTHORITATIVE_NVIDIA_SKILLS,
    INTERNAL_ONNX_CHECK_SKILL,
)


def register_all_authoritative_skills(registry: DefaultSkillRegistry) -> None:
    """Register all 10 authoritative NVIDIA skills and internal skills into a DefaultSkillRegistry."""
    for skill in ALL_AUTHORITATIVE_SKILLS:
        registry.register_skill(skill)


def load_default_skill_registry(
    capability_registry: CapabilityRegistry | None = None,
) -> DefaultSkillRegistry:
    """Instantiate a DefaultSkillRegistry pre-populated with all authoritative skills."""
    registry = DefaultSkillRegistry(capability_registry=capability_registry)
    register_all_authoritative_skills(registry)
    return registry
