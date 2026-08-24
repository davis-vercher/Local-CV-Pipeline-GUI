@echo off
setlocal
cd /d "%~dp0"

py -c "import tkinter" >nul 2>&1
if not errorlevel 1 (
    py "%~dp0outdoor_vision_app.py"
    goto :end
)

python -c "import tkinter" >nul 2>&1
if not errorlevel 1 (
    python "%~dp0outdoor_vision_app.py"
    goto :end
)

set "CODEX_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%CODEX_PYTHON%" (
    "%CODEX_PYTHON%" -c "import tkinter" >nul 2>&1
    if not errorlevel 1 (
        "%CODEX_PYTHON%" "%~dp0outdoor_vision_app.py"
        goto :end
    )
)

echo Outdoor Vision CV could not find Python with Tkinter.
echo.
echo Install Python 3 from python.org and ensure Tcl/Tk is selected.
echo.
pause

:end
endlocal

