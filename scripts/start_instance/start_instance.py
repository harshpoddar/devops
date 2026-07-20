#!/usr/bin/env python3
"""Legacy shim — prefer `cloudops start`. Delegates to cloudops.commands.start_instance."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cloudops.bootstrap import require_deps

require_deps()  # exits 4 with install.sh instructions if deps are missing

from cloudops.commands.start_instance import main

if __name__ == "__main__":
    sys.exit(main())
