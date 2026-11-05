$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
foreach ($tool in @('python', 'ffmpeg', 'ffprobe')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool was not found. Install Python 3.12+ and FFmpeg, then try again."
    }
}
& python -c "import sys; sys.exit(sys.version_info < (3, 12))"
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 or newer is required; found $(& python --version)." }
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
}
$requirementsHash = & .venv/Scripts/python -c "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('requirements.txt').read_bytes()).hexdigest())"
if ($LASTEXITCODE -ne 0) { throw 'Could not read the requirements file.' }
$stamp = '.venv/shorts-requirements.sha256'
if (-not (Test-Path -LiteralPath $stamp) -or (Get-Content -LiteralPath $stamp -Raw).Trim() -ne $requirementsHash) {
    & .venv/Scripts/python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Could not install dependencies.' }
    Set-Content -LiteralPath $stamp -Value $requirementsHash
}
# With an NVIDIA GPU, also install the CUDA libraries for GPU transcription.
# Set CLIPSMITH_WHISPER_DEVICE=cpu to skip this and transcribe on the CPU.
if ((Get-Command nvidia-smi -ErrorAction SilentlyContinue) -and $env:CLIPSMITH_WHISPER_DEVICE -ne 'cpu') {
    $gpuHash = & .venv/Scripts/python -c "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('requirements-gpu.txt').read_bytes()).hexdigest())"
    $gpuStamp = '.venv/shorts-gpu-requirements.sha256'
    if (-not (Test-Path -LiteralPath $gpuStamp) -or (Get-Content -LiteralPath $gpuStamp -Raw).Trim() -ne $gpuHash) {
        Write-Host 'NVIDIA GPU found: installing the CUDA libraries for GPU transcription (about 1.25 GB, once).'
        & .venv/Scripts/python -m pip install -r requirements-gpu.txt
        if ($LASTEXITCODE -eq 0) { Set-Content -LiteralPath $gpuStamp -Value $gpuHash }
        else { Write-Warning 'Could not install the CUDA libraries; transcription will use the CPU.' }
    }
}
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
$env:PYTHONIOENCODING = 'utf-8'
& .venv/Scripts/python -u shorts.py @args
exit $LASTEXITCODE
