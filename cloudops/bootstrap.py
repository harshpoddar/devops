"""Pre-flight dependency check for the scripts.

Deliberately imports nothing outside the stdlib, so it works even when the
package's third-party dependencies are absent — the whole point is to turn an
ImportError traceback into an actionable instruction (run ./install.sh, or use
the venv interpreter) that an agent can relay to the user verbatim.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# `vastai` is the official Vast.ai CLI the Vast backend shells out to; it lives in
# this skill's venv so the whole thing is self-contained. `requests` is no longer a
# direct dependency (it arrives transitively via vastai) so it is not checked here.
REQUIRED = ("boto3", "rich", "vastai")
EXIT_NOT_INSTALLED = 4


def require_deps() -> None:
    missing = [m for m in REQUIRED if importlib.util.find_spec(m) is None]
    if not missing:
        return
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        msg = (
            f"Missing dependencies in this interpreter ({sys.executable}): {', '.join(missing)}.\n"
            f"The skill's virtualenv already exists — re-run this script with:\n"
            f"  {venv_python} {' '.join(sys.argv)}"
        )
    else:
        msg = (
            f"The cloud-devops skill is not installed yet (missing: {', '.join(missing)}).\n"
            f"Run the installer first:\n"
            f"  cd {ROOT} && ./install.sh\n"
            f"then re-run this script with {venv_python}"
        )
    sys.stderr.write(msg + "\n")
    sys.exit(EXIT_NOT_INSTALLED)
