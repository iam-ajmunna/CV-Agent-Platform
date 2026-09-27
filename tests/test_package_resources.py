"""Tests that default runtime resources travel with the package."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent


def test_copied_package_loads_default_resources_outside_repository(
    tmp_path: Path,
) -> None:
    """Removing sibling repo directories must not degrade the default runtime."""
    source_package = _REPO_ROOT / "cv_agent"
    target_package = tmp_path / "cv_agent"
    shutil.copytree(source_package, target_package, ignore=shutil.ignore_patterns("__pycache__"))

    result = subprocess.run(
        [sys.executable, "-m", "cv_agent"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Runtime status  : OK" in result.stdout

    with open(_REPO_ROOT / "spec" / "capability_registry.json", encoding="utf-8") as fh:
        expected_count = len(json.load(fh)["capabilities"])
    assert f"Capabilities    : {expected_count} registered" in result.stdout


def test_packaged_registry_matches_source_of_truth() -> None:
    """
    cv_agent/resources/spec/capability_registry.json is a manually-kept-in-sync
    copy of spec/capability_registry.json (the source of truth per
    spec/10-capability-registry.md) — there is no automated sync step. This test
    catches drift between the two so a stale packaged copy cannot silently report
    different capability status (e.g. "available") than the source registry.
    """
    source = _REPO_ROOT / "spec" / "capability_registry.json"
    packaged = _REPO_ROOT / "cv_agent" / "resources" / "spec" / "capability_registry.json"
    with open(source, encoding="utf-8") as fh:
        source_data = json.load(fh)
    with open(packaged, encoding="utf-8") as fh:
        packaged_data = json.load(fh)
    assert source_data == packaged_data, (
        "cv_agent/resources/spec/capability_registry.json has drifted from "
        "spec/capability_registry.json — copy the source file over the packaged "
        "one before committing."
    )
