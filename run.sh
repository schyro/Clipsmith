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

# With an NVIDIA GPU, also install the CUDA libraries for GPU transcription.
# Set CLIPSMITH_WHISPER_DEVICE=cpu to skip this and transcribe on the CPU.
if command -v nvidia-smi >/dev/null 2>&1 && [ "${CLIPSMITH_WHISPER_DEVICE:-}" != cpu ]; then
    gpu_hash=$(.venv/bin/python -c "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('requirements-gpu.txt').read_bytes()).hexdigest())")
    gpu_stamp=.venv/shorts-gpu-requirements.sha256
    if [ ! -f "$gpu_stamp" ] || [ "$(tr -d '[:space:]' < "$gpu_stamp")" != "$gpu_hash" ]; then
        echo 'NVIDIA GPU found: installing the CUDA libraries for GPU transcription (about 1.3 GB, once).'
        if .venv/bin/python -m pip install -r requirements-gpu.txt; then
            echo "$gpu_hash" > "$gpu_stamp"
        else
            echo 'Could not install the CUDA libraries; transcription will use the CPU.' >&2
        fi
    fi
    # CTranslate2 loads cuBLAS and cuDNN through the dynamic loader path.
    cuda_libs=$(.venv/bin/python -c "import glob, os, nvidia; print(':'.join(sorted({p for base in nvidia.__path__ for p in glob.glob(os.path.join(base, '*', 'lib'))})))" 2>/dev/null || true)
    if [ -n "$cuda_libs" ]; then
        export LD_LIBRARY_PATH="$cuda_libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    fi
fi

export HF_HUB_DISABLE_SYMLINKS_WARNING=1
export PYTHONIOENCODING=utf-8
exec .venv/bin/python -u shorts.py "$@"
