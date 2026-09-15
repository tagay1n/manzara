"""Evaluation imports must work without adding its runtime directory to sys.path."""

import os
import subprocess
import sys
from pathlib import Path


def test_evaluation_imports_use_the_repository_package(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; before = list(sys.path); from app.modules.library.runtime.metadata.evaluation import evaluate; assert callable(evaluate); assert sys.path == before; assert 'models' not in sys.modules; assert 'metadata' not in sys.modules",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
