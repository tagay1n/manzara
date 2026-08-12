"""In-memory task process state."""

from dataclasses import dataclass
import subprocess
from typing import Optional, TextIO

@dataclass
class ProcessHandle:
    """In-memory link between a run id and active process."""

    run_id: int
    task_id: str
    panel_id: str
    proc: subprocess.Popen[str]
    log_file: Optional[TextIO] = None
    log_path: Optional[str] = None
