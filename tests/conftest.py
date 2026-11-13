import os
import sys
from pathlib import Path

# Add tests/stubs and src to sys.path and PYTHONPATH so subprocesses and in-process tests resolve auragrid and aurasprout
TESTS_DIR = Path(__file__).resolve().parent
STUBS_DIR = TESTS_DIR / "stubs"
SPROUT_SRC = TESTS_DIR.parent / "src"
SEED_DIR = TESTS_DIR.parent.parent / "auraseed"

for p in (STUBS_DIR, SPROUT_SRC, SEED_DIR):
    if p.exists():
        p_str = str(p)
        if p_str not in sys.path:
            sys.path.insert(0, p_str)

# Prepend to os.environ["PYTHONPATH"] for any subprocesses spawned in tests
current_pythonpath = os.environ.get("PYTHONPATH", "")
paths_to_add = [str(p) for p in (STUBS_DIR, SPROUT_SRC, SEED_DIR) if p.exists()]
if current_pythonpath:
    paths_to_add.append(current_pythonpath)
os.environ["PYTHONPATH"] = os.pathsep.join(paths_to_add)

# If auragrid is not installed, import stub into sys.modules
try:
    import auragrid  # noqa: F401
except ImportError:
    import tests.stubs.auragrid as auragrid_stub
    sys.modules["auragrid"] = auragrid_stub
