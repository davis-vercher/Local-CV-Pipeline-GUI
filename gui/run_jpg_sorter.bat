@echo off
setlocal
cd /d "%~dp0"

py -c "import tkinter, PIL" >nul 2>&1
if not errorlevel 1 (
    py "%~dp0jpg_sorter.py"
    goto :end
)

python -c "import tkinter, PIL" >nul 2>&1
if not errorlevel 1 (
    python "%~dp0jpg_sorter.py"
    goto :end
)

set "CODEX_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%CODEX_PYTHON%" (
    "%CODEX_PYTHON%" -c "import tkinter, PIL" >nul 2>&1
    if not errorlevel 1 (
        "%CODEX_PYTHON%" "%~dp0jpg_sorter.py"
        goto :end
    )
)

echo JPEG Dataset Sorter could not find Python with Tkinter and Pillow.
echo.
echo Install Python 3 from python.org, then run:
echo     python -m pip install -r "%~dp0requirements.txt"
echo.
pause

:end
endlocal
