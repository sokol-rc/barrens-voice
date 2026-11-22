@echo off
rem Barrens Voice: prepares the Python environment once, then opens the launcher window.
rem Any switches go to the launcher: BarrensVoice.cmd --console runs the companion in a console,
rem BarrensVoice.cmd --help lists them all.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Creating the Python environment...
    python -m venv .venv || goto :fail
)
rem (re)install when requirements.txt differs from the one installed last (a new version of the program)
fc /b requirements.txt .venv\requirements.txt >nul 2>&1 && goto :run
echo Installing the libraries...
.venv\Scripts\python.exe -m pip install -q -r requirements.txt || goto :fail
rem RUAccent pulls in the CPU-only onnxruntime; the race voices want the video card's build of it: CUDA on
rem NVIDIA (the game keeps running smoothly; ~1.9 GB more), DirectML on other DirectX 12 cards
.venv\Scripts\python.exe -m pip uninstall -q -y onnxruntime onnxruntime-gpu onnxruntime-directml
where nvidia-smi >nul 2>&1 && (
    echo NVIDIA found: ONNX Runtime with CUDA...
    .venv\Scripts\python.exe -m pip install -q "onnxruntime-gpu[cuda,cudnn]==1.32.0" onnx==1.23.2 || goto :fail
) || (
    .venv\Scripts\python.exe -m pip install -q --force-reinstall --no-deps onnxruntime-directml==1.24.4 || goto :fail
)
copy /y requirements.txt .venv\requirements.txt >nul
rem the models now, while installing, not at the first start (~3 GB once; those in the cache are skipped)
echo Downloading the models...
.venv\Scripts\python.exe -m bv --fetch-models || echo The companion will download them at its first start.
:run
start "" .venv\Scripts\pythonw.exe BarrensVoice.pyw %*
exit /b
:fail
echo Setup failed.
pause
