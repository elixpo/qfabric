from __future__ import annotations

import json
import os
import platform
import random
import subprocess
import time
from pathlib import Path
from typing import Any

from .profiling import percentile
from .runtime import _connect_bridge

BLOCK_BYTES = 256
FIR_TAPS = 32
# (task, passes): one trivial task, one byte-oriented kernel, and an FIR whose
# pass count sweeps compute intensity against the fixed boundary cost.
WORKLOADS = (
    ("add", 1),
    ("crc32", 1),
    ("fir", 1),
    ("fir", 16),
    ("fir", 64),
)
LOADS = ("idle", "cpu", "rt-contention")
POLICIES = ("other", "fifo")


def workload_name(task: str, passes: int) -> str:
    return task if task != "fir" else f"fir-x{passes}"


def make_block(seed: int, invocation_id: int) -> bytes:
    return random.Random(seed * 1_000_003 + invocation_id).randbytes(BLOCK_BYTES)


def reference_crc32(block: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in block:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 & -(crc & 1))
    return ~crc & 0xFFFFFFFF


def reference_fir(block: bytes, passes: int) -> int:
    samples = [
        int.from_bytes(block[index : index + 2], "little", signed=True)
        for index in range(0, BLOCK_BYTES, 2)
    ]
    taps = [(k + 1 if k < FIR_TAPS // 2 else FIR_TAPS - k) * 64 for k in range(FIR_TAPS)]
    total = 0
    for pass_index in range(passes):
        perturbation = pass_index & 0x7F
        for n in range(len(samples)):
            acc = sum(
                taps[k] * (samples[n - k] ^ perturbation) for k in range(min(FIR_TAPS, n + 1))
            )
            total += acc >> 15
    return total


def reference_result(task: str, passes: int, block: bytes) -> int:
    """Expected result modulo 2^32, the width both executors report."""
    if task == "add":
        value = 5
    elif task == "crc32":
        value = reference_crc32(block)
    elif task == "fir":
        value = reference_fir(block, passes)
    else:
        raise ValueError(f"unsupported Stage 12 task: {task}")
    return value & 0xFFFFFFFF


class LinuxExecutor:
    """Persistent Linux executor reached over a pipe, started once per campaign."""

    def __init__(self, artifact: Path) -> None:
        self._process = subprocess.Popen(
            [str(artifact)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def invoke(self, invocation_id: int, task: str, passes: int, block: bytes) -> tuple[int, int]:
        assert self._process.stdin is not None and self._process.stdout is not None
        self._process.stdin.write(f"{invocation_id} {task} {passes} {block.hex()}\n")
        self._process.stdin.flush()
        fields = self._process.stdout.readline().split()
        if len(fields) != 3 or fields[0] == "error":
            raise RuntimeError(f"Linux executor rejected the request: {' '.join(fields)}")
        observed_id, result, local_ns = (int(field, 10) for field in fields)
        if observed_id != invocation_id:
            raise RuntimeError(
                f"Linux correlation mismatch: expected {invocation_id}, received {observed_id}"
            )
        return result & 0xFFFFFFFF, local_ns

    def close(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()


class RtExecutor:
    def __init__(self, address: str, timeout: float) -> None:
        self._bridge: Any = _connect_bridge(address)
        self._timeout = timeout

    def invoke(self, invocation_id: int, task: str, passes: int, block: bytes) -> tuple[int, int]:
        del invocation_id
        response = self._bridge.call(
            "qf_stage12_invoke", f"{task} {passes} {block.hex()}", timeout=self._timeout
        )
        fields = str(response).split()
        if len(fields) != 2 or fields[0] == "error":
            raise RuntimeError(f"RT executor rejected the request: {response!r}")
        return int(fields[0], 10), int(fields[1], 10)

    def close(self) -> None:
        self._bridge.disconnect()


def summarize(values: list[int]) -> dict[str, float]:
    return {
        "p50_ns": percentile(values, 50),
        "p95_ns": percentile(values, 95),
        "p99_ns": percentile(values, 99),
        "maximum_ns": max(values),
    }


def _scheduler_policy() -> str:
    policy = os.sched_getscheduler(0)
    return {os.SCHED_OTHER: "SCHED_OTHER", os.SCHED_FIFO: "SCHED_FIFO"}.get(policy, str(policy))


def run_kernel_profile(
    root: Path,
    address: str,
    *,
    domain: str,
    load: str,
    policy: str,
    iterations: int,
    warmup: int,
    seed: int,
    timeout: float,
) -> dict[str, Any]:
    if domain not in {"linux", "rt"}:
        raise ValueError("domain must be linux or rt")
    if load not in LOADS or policy not in POLICIES:
        raise ValueError("unsupported load or policy label")
    if iterations < 100 or warmup < 0:
        raise ValueError("Stage 12 requires at least 100 iterations and non-negative warmup")
    observed_policy = _scheduler_policy()
    if (policy == "fifo") != (observed_policy == "SCHED_FIFO"):
        raise RuntimeError(f"policy label {policy} does not match {observed_policy}")
    if domain == "linux":
        artifact = root / "build/stage12/linux/qf-stage12-linux"
        if not artifact.is_file():
            raise RuntimeError(f"Stage 12 Linux artifact is missing: {artifact}")
        executor: LinuxExecutor | RtExecutor = LinuxExecutor(artifact)
    else:
        executor = RtExecutor(address, timeout)

    workloads = []
    invocation_id = 0
    try:
        for task, passes in WORKLOADS:
            samples = []
            for index in range(warmup + iterations):
                invocation_id += 1
                block = make_block(seed, invocation_id)
                expected = reference_result(task, passes, block)
                outcome = "ok"
                local_ns: int | None = None
                started_ns = time.perf_counter_ns()
                try:
                    result, local_ns = executor.invoke(invocation_id, task, passes, block)
                    if result != expected:
                        outcome = "wrong-result"
                except TimeoutError:
                    outcome = "timeout"
                except (OSError, RuntimeError, ValueError):
                    outcome = "error"
                finished_ns = time.perf_counter_ns()
                if index < warmup:
                    continue
                samples.append(
                    {
                        "invocation_id": invocation_id,
                        "outcome": outcome,
                        "end_to_end_ns": finished_ns - started_ns,
                        "local_execution_ns": local_ns,
                    }
                )
            ok = [sample["end_to_end_ns"] for sample in samples if sample["outcome"] == "ok"]
            local = [
                sample["local_execution_ns"]
                for sample in samples
                if sample["outcome"] == "ok" and sample["local_execution_ns"] is not None
            ]
            workloads.append(
                {
                    "workload": workload_name(task, passes),
                    "task": task,
                    "passes": passes,
                    "failures": len(samples) - len(ok),
                    "end_to_end": summarize(ok) if ok else None,
                    "local_execution": summarize(local) if local else None,
                    "samples": samples,
                }
            )
    finally:
        executor.close()

    return {
        "schema_version": 1,
        "stage": 12,
        "domain": domain,
        "load": load,
        "policy": policy,
        "scheduler": observed_policy,
        "iterations": iterations,
        "warmup": warmup,
        "seed": seed,
        "block_bytes": BLOCK_BYTES,
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "workloads": workloads,
    }


def write_kernel_profile(report: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
