import os
import tempfile


# The module-level ASGI app is created during import. Keep its test history out
# of the user's real Application Support directory.
os.environ.setdefault("EASY_PRD2_DATA_DIR", tempfile.mkdtemp(prefix="easy-prd2-tests-"))

