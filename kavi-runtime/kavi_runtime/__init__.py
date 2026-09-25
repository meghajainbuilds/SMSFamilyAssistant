"""kavi-runtime: HomeOS Kavi v0.2 always-on Python service."""

__version__ = "0.2.0"

# Phase 4 (2026-06-02): make the sibling `capabilities/` directory importable
# as a top-level package. Per-capability code (e.g.,
# `capabilities.coordination.dispatch`) physically lives at
# `<repo_root>/capabilities/<name>/` (one level above this runtime package's
# parent). Without this insertion, `from capabilities.X import Y` fails when
# tests run with the runtime package's directory as the working dir.
import sys as _sys
from pathlib import Path as _Path

_homeos_root = _Path(__file__).resolve().parent.parent.parent
if str(_homeos_root) not in _sys.path:
    _sys.path.insert(0, str(_homeos_root))
