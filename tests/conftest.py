"""Pytest config.

The server module raises SystemExit at import if YOUTRACK_URL or a token source
isn't configured. The test suite sets fake env vars so imports succeed without
trying to talk to a real YouTrack instance. Tests that need to exercise the
HTTP layer should mock `urllib.request.urlopen` explicitly.
"""

import os
import sys
from pathlib import Path

# Set fake creds before any module-level import of server.py runs.
os.environ.setdefault("YOUTRACK_URL", "https://test.invalid")
os.environ.setdefault("YOUTRACK_TOKEN", "perm-fake.test.token")

# Make `src/` importable when running pytest from the project root.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
