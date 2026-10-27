import shutil
import subprocess
import zlib
from pathlib import Path

import pytest

from qfabric import stage12

ROOT = Path(__file__).resolve().parents[1]


def test_reference_crc32_matches_zlib():
    block = stage12.make_block(3, 9)
    assert stage12.reference_crc32(block) == zlib.crc32(block)


def test_blocks_are_deterministic_and_bounded():
    assert stage12.make_block(1, 2) == stage12.make_block(1, 2)
    assert stage12.make_block(1, 2) != stage12.make_block(1, 3)
    assert len(stage12.make_block(1, 2)) == stage12.BLOCK_BYTES


def test_fir_passes_scale_work_and_perturb_result():
    block = stage12.make_block(5, 1)
    assert stage12.reference_result("fir", 2, block) != stage12.reference_result(
        "fir", 1, block
    )
    with pytest.raises(ValueError):
        stage12.reference_result("unknown", 1, block)


@pytest.mark.skipif(shutil.which("g++") is None, reason="host C++ compiler unavailable")
def test_linux_executor_matches_python_reference(tmp_path):
    artifact = tmp_path / "qf-stage12-host"
    subprocess.run(
        [
            "g++",
            "-std=c++20",
            "-O2",
            str(ROOT / "runtime/stage12/linux/qf_stage12_linux.cpp"),
            "-o",
            str(artifact),
        ],
        check=True,
    )
    executor = stage12.LinuxExecutor(artifact)
    try:
        for invocation_id, (task, passes) in enumerate(stage12.WORKLOADS, start=1):
            block = stage12.make_block(11, invocation_id)
            result, local_ns = executor.invoke(invocation_id, task, passes, block)
            assert result == stage12.reference_result(task, passes, block)
            assert local_ns >= 0
    finally:
        executor.close()
