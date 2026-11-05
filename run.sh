#!/usr/bin/env bash
# macOS/Linux launcher: creates .venv, installs pinned requirements when they change, then runs shorts.py.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
for tool in "$PYTHON" ffmpeg ffprobe; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "$tool was not found. Install Python 3.12+ and FFmpeg, then try again." >&2
        exit 1
    fi
done

if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
    echo "Python 3.12 or newer is required ($("$PYTHON" --version 2>&1) found). Set PYTHON=/path/to/python3.12 if needed." >&2
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    "$PYTHON" -m venv .venv || { echo 'Could not create the Python environment.' >&2; exit 1; }
fi

hash=$(.venv/bin/python -c "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('requirements.txt').read_bytes()).hexdigest())")
stamp=.venv/shorts-requirements.sha256
if [ ! -f "$stamp" ] || [ "$(tr -d '[:space:]' < "$stamp")" != "$hash" ]; then
    .venv/bin/python -m pip install -r requirements.txt || { echo 'Could not install dependencies.' >&2; exit 1; }
    echo "$hash" > "$stamp"
fi

export HF_HUB_DISABLE_SYMLINKS_WARNING=1
export PYTHONIOENCODING=utf-8
exec .venv/bin/python -u shorts.py "$@"
