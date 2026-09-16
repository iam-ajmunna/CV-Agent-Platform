"""cv_agent.evaluation.benchmarks — Systems benchmarking coordination for ADR-0011."""

from __future__ import annotations

import asyncio
import inspect
import math
import statistics
from collections.abc import Mapping
from typing import Any, Protocol, cast, runtime_checkable

from cv_agent.evaluation.contracts import (
    BenchmarkConfiguration,
    BenchmarkResult,
    ModelArtifactReference,
)
from cv_agent.evaluation.errors import BenchmarkExecutionError
from cv_agent.evaluation.types import HardwareClass
from cv_agent.tools import (
    ExecutionId,
    TargetId,
    ToolExecutionStatus,
    ToolExecutor,
    ToolId,
    ToolInvocation,
    ToolResult,
)


def _is_accelerator_target(
    config: BenchmarkConfiguration, observation: Mapping[str, Any]
) -> bool:
    """Determine whether benchmark target is an asynchronous accelerator per ADR-0011 §3.13."""
    req = config.target_requirements
    if (
        req.required_hardware_class is not None
        and req.required_hardware_class != HardwareClass.CPU
    ):
        return True
    if req.min_cuda_compute_capability is not None:
        return True
    hw_cls = str(observation.get("hardware_class", "")).lower()
    if hw_cls in {"nvidia_gpu", "jetson", "other_accelerator", "gpu", "accelerator"}:
        return True
    dev_name = str(observation.get("device_name", "")).lower()
    gpu_keywords = (
        "nvidia",
        "cuda",
        "geforce",
        "rtx",
        "gtx",
        "tesla",
        "a100",
        "h100",
        "v100",
        "t4",
        "jetson",
        "radeon",
        "rocm",
        "metal",
        "apple m",
    )
    return any(k in dev_name for k in gpu_keywords)


@runtime_checkable
class BenchmarkRunner(Protocol):
    """Protocol for executing hardware-aware latency and throughput benchmarks."""

    def run_benchmark(
        self,
        model_artifact: ModelArtifactReference,
        config: BenchmarkConfiguration,
        target_id: TargetId,
        tool_executor: ToolExecutor,
        tool_id: ToolId,
        *,
        approval_authorized: bool = False,
        timeout_seconds: int = 3600,
    ) -> tuple[BenchmarkResult, ...]: ...

    def compute_result_from_observation(
        self,
        config: BenchmarkConfiguration,
        target_id: TargetId,
        batch_size: int,
        observation: Mapping[str, Any],
    ) -> BenchmarkResult: ...


class DefaultBenchmarkRunner:
    """Authoritative runner for hardware-aware systems performance benchmarking.

    Enforces empirical on-target execution via ADR-0005 ToolExecutor, strictly validates
    warmup completion, derives latency percentiles and throughput directly from measured
    observation samples, and extracts hardware telemetry without synthetic defaults.
    """

    def __init__(self, tool_executor: ToolExecutor | None = None) -> None:
        self._tool_executor = tool_executor

    def run_benchmark(
        self,
        model_artifact: ModelArtifactReference,
        config: BenchmarkConfiguration,
        target_id: TargetId,
        tool_executor: ToolExecutor,
        tool_id: ToolId,
        *,
        approval_authorized: bool = False,
        timeout_seconds: int = 3600,
    ) -> tuple[BenchmarkResult, ...]:
        """Execute hardware benchmark sweep across configured batch sizes.

        Dispatches on-target execution via ADR-0005, verifies warmup iterations,
        collects empirical measured iteration samples, and computes latency/throughput
        statistics directly from the observations.
        """
        if tool_executor is None:
            raise BenchmarkExecutionError(
                "ToolExecutor is mandatory for empirical benchmark execution. "
                "Local simulation or fabrication of benchmark results is strictly prohibited."
            )
        if tool_id is None or not str(tool_id).strip():
            raise BenchmarkExecutionError(
                "ToolId is mandatory for empirical benchmark execution. "
                "Local synthesis of ToolId is strictly prohibited."
            )
        if not target_id or not str(target_id).strip():
            raise BenchmarkExecutionError(
                "TargetId is required for systems benchmarking"
            )

        if config.warmup_iterations < 5:
            raise BenchmarkExecutionError(
                f"warmup_iterations must be >= 5, got {config.warmup_iterations}"
            )
        if config.measured_iterations < 20:
            raise BenchmarkExecutionError(
                f"measured_iterations must be >= 20, got {config.measured_iterations}"
            )

        results: list[BenchmarkResult] = []

        for batch_size in config.batch_sizes:
            res = self._benchmark_single_batch(
                model_artifact=model_artifact,
                config=config,
                target_id=target_id,
                batch_size=batch_size,
                tool_executor=tool_executor,
                tool_id=tool_id,
                approval_authorized=approval_authorized,
                timeout_seconds=timeout_seconds,
            )
            results.append(res)

        return tuple(results)

    def _benchmark_single_batch(
        self,
        model_artifact: ModelArtifactReference,
        config: BenchmarkConfiguration,
        target_id: TargetId,
        batch_size: int,
        tool_executor: ToolExecutor,
        tool_id: ToolId,
        *,
        approval_authorized: bool = False,
        timeout_seconds: int = 3600,
    ) -> BenchmarkResult:
        """Benchmark a single batch configuration via authoritative ADR-0005 execution."""
        invocation = ToolInvocation(
            tool_id=tool_id,
            parameters={
                "model_artifact": {
                    "checkpoint_id": str(model_artifact.checkpoint_id),
                    "architecture_name": model_artifact.architecture_name,
                    "weights_hash": str(model_artifact.weights_hash),
                    "weights_uri": model_artifact.weights_uri,
                    "precision": model_artifact.precision.value,
                    "input_resolution": list(model_artifact.input_resolution),
                },
                "benchmark_id": str(config.benchmark_id),
                "batch_size": batch_size,
                "precision": config.precision.value,
                "warmup_iterations": config.warmup_iterations,
                "measured_iterations": config.measured_iterations,
                "measure_power": config.measure_power,
                "measure_memory": config.measure_memory,
            },
            target_id=target_id,
            execution_id=ExecutionId(f"exec-bench-{config.benchmark_id}-{batch_size}"),
            timeout_seconds=timeout_seconds,
            approval_authorized=approval_authorized,
        )

        result = self._dispatch_tool(tool_executor, invocation)

        if result.status != ToolExecutionStatus.SUCCESS:
            raise BenchmarkExecutionError(
                f"Benchmark tool execution failed for batch {batch_size}: {result.error_message}"
            )

        raw_output = result.structured_output or {}
        return self.compute_result_from_observation(
            config=config,
            target_id=target_id,
            batch_size=batch_size,
            observation=raw_output,
        )

    def compute_result_from_observation(
        self,
        config: BenchmarkConfiguration,
        target_id: TargetId,
        batch_size: int,
        observation: Mapping[str, Any],
    ) -> BenchmarkResult:
        """Compute typed BenchmarkResult from empirical on-target observation telemetry."""
        # 1. Warmup validation (must be genuinely executed on target)
        warmup_executed = int(observation.get("warmup_executed", 0))
        if warmup_executed < config.warmup_iterations:
            raise BenchmarkExecutionError(
                f"Warmup iterations incomplete on target {target_id}: "
                f"expected at least {config.warmup_iterations}, but target reported {warmup_executed}"
            )

        # 2. Measured iterations sample validation
        sample = observation.get("latencies_ms") or observation.get(
            "latency_samples_ms"
        )
        if not sample:
            raise BenchmarkExecutionError(
                f"Benchmark execution on target {target_id} returned no latency observations for batch {batch_size}"
            )
        if len(sample) < config.measured_iterations:
            raise BenchmarkExecutionError(
                f"Measured iterations sample size ({len(sample)}) is less than required ({config.measured_iterations})"
            )

        float_sample: list[float] = []
        for i, val in enumerate(sample):
            if (
                not isinstance(val, (int, float))
                or not math.isfinite(val)
                or val <= 0.0
            ):
                raise BenchmarkExecutionError(
                    f"Observation sample index {i} has invalid latency value {val!r}; must be a finite float > 0"
                )
            float_sample.append(float(val))

        # 3. Empirical latency statistics calculated directly from observation sample
        sorted_sample = sorted(float_sample)
        lat_min = sorted_sample[0]
        lat_max = sorted_sample[-1]
        lat_mean = statistics.mean(sorted_sample)
        lat_p50 = self._percentile(sorted_sample, 50.0)
        lat_p90 = self._percentile(sorted_sample, 90.0)
        lat_p95 = self._percentile(sorted_sample, 95.0)
        lat_p99 = self._percentile(sorted_sample, 99.0)

        # 4. Throughput calculated from measured work / elapsed time
        total_time_ms = sum(sorted_sample)
        if total_time_ms > 0:
            throughput_fps = (batch_size * len(sorted_sample)) / (
                total_time_ms / 1000.0
            )
        else:
            throughput_fps = (1000.0 / lat_mean) * batch_size

        # 5. Hardware device identity (from actual target telemetry, no fake default)
        device_name = observation.get("device_name")
        if not device_name or not str(device_name).strip():
            raise BenchmarkExecutionError(
                f"Missing device_name in target telemetry for target {target_id}. "
                f"Fabrication of default device names is strictly prohibited."
            )

        # 6. Hardware telemetry metrics (explicitly None if unavailable)
        peak_vram_gb = (
            float(observation["peak_vram_gb"])
            if observation.get("peak_vram_gb") is not None
            else None
        )
        cpu_util = (
            float(observation["cpu_utilization_pct"])
            if observation.get("cpu_utilization_pct") is not None
            else None
        )
        gpu_util = (
            float(observation["gpu_utilization_pct"])
            if observation.get("gpu_utilization_pct") is not None
            else None
        )
        power_watts = (
            float(observation["power_watts_avg"])
            if config.measure_power and observation.get("power_watts_avg") is not None
            else None
        )

        # 7. Hardware-synchronized timing validation (ADR-0011 §3.13)
        # Asynchronous accelerator measurements strictly require hardware-synchronized timing
        is_accelerator = _is_accelerator_target(config, observation)
        raw_timing_sync = observation.get("timing_synchronized")
        raw_timing_source = observation.get("timing_source")

        if is_accelerator:
            if raw_timing_sync is not True:
                raise BenchmarkExecutionError(
                    f"Hardware-synchronized timing is required for asynchronous accelerator measurements on target {target_id} "
                    f"per ADR-0011 §3.13, but timing_synchronized is {raw_timing_sync!r}. "
                    f"Target telemetry must attest timing_synchronized=True."
                )
            if not raw_timing_source or not str(raw_timing_source).strip():
                raise BenchmarkExecutionError(
                    f"Authoritative timing_source is required for accelerator measurements on target {target_id} "
                    f"per ADR-0011 §3.13, but got {raw_timing_source!r}. "
                    f"Valid synchronization source (e.g. 'cuda_event', 'cuda_stream_synchronize') is mandatory."
                )
            source_normalized = str(raw_timing_source).strip().lower()
            if source_normalized in {
                "unsynchronized",
                "wall_clock_async",
                "none",
                "unknown",
                "async_wall_clock",
            }:
                raise BenchmarkExecutionError(
                    f"Accelerator timing source {raw_timing_source!r} on target {target_id} is unsynchronized. "
                    f"ADR-0011 §3.13 requires hardware-synchronized timing (e.g. 'cuda_event')."
                )

        timing_synchronized = (
            bool(raw_timing_sync) if raw_timing_sync is not None else True
        )
        timing_source = (
            str(raw_timing_source).strip()
            if raw_timing_source is not None and str(raw_timing_source).strip()
            else ("cuda_event" if is_accelerator else "host_timer")
        )

        return BenchmarkResult(
            benchmark_id=config.benchmark_id,
            batch_size=batch_size,
            precision=config.precision,
            latency_min_ms=round(lat_min, 3),
            latency_mean_ms=round(lat_mean, 3),
            latency_p50_ms=round(lat_p50, 3),
            latency_p90_ms=round(lat_p90, 3),
            latency_p95_ms=round(lat_p95, 3),
            latency_p99_ms=round(lat_p99, 3),
            latency_max_ms=round(lat_max, 3),
            throughput_fps=round(throughput_fps, 2),
            peak_vram_gb=peak_vram_gb,
            cpu_utilization_pct=cpu_util,
            gpu_utilization_pct=gpu_util,
            power_watts_avg=power_watts,
            device_name=str(device_name),
            target_id=target_id,
            timing_synchronized=timing_synchronized,
            timing_source=timing_source,
        )

    @staticmethod
    def _percentile(sorted_data: list[float], p: float) -> float:
        """Calculate percentile using linear interpolation between nearest ranks."""
        k = (len(sorted_data) - 1) * (p / 100.0)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return sorted_data[int(k)]
        d0 = sorted_data[int(f)] * (c - k)
        d1 = sorted_data[int(c)] * (k - f)
        return d0 + d1

    def _dispatch_tool(
        self,
        tool_executor: ToolExecutor,
        invocation: ToolInvocation,
    ) -> ToolResult:
        """Dispatch ToolInvocation to ADR-0005 ToolExecutor synchronously or via async event loop."""
        try:
            coro_or_res = tool_executor.execute(invocation)
            if inspect.iscoroutine(coro_or_res):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        return pool.submit(asyncio.run, coro_or_res).result()
                else:
                    return asyncio.run(coro_or_res)
            return cast(ToolResult, coro_or_res)
        except Exception as exc:
            if isinstance(exc, BenchmarkExecutionError):
                raise
            raise BenchmarkExecutionError(
                f"Benchmark execution transport failed via ADR-0005: {exc}"
            ) from exc
