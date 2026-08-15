from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def test_meta_evaluate_script_bootstraps_repo_imports() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    runner = (
        repo_root
        / "app"
        / "modules"
        / "library"
        / "runtime"
        / "run_meta_evaluate.py"
    )

    result = subprocess.run(
        [sys.executable, str(runner), "--help"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Run monocorpus metadata evaluate" in result.stdout
