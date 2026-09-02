@echo off
setlocal
cd /d "%~dp0"

py -c "import tkinter" >nul 2>&1
if not errorlevel 1 (
    py -c "import PIL, send2trash, tkinterdnd2" >nul 2>&1
    if not errorlevel 1 (
        py "%~dp0outdoor_vision_app.py"
        goto :end
    )
)

python -c "import tkinter" >nul 2>&1
if not errorlevel 1 (
    python -c "import PIL, send2trash, tkinterdnd2" >nul 2>&1
    if not errorlevel 1 (
        python "%~dp0outdoor_vision_app.py"
        goto :end
    )
)

set "CODEX_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%CODEX_PYTHON%" (
    "%CODEX_PYTHON%" -c "import tkinter" >nul 2>&1
    if not errorlevel 1 (
        "%CODEX_PYTHON%" -c "import PIL, send2trash, tkinterdnd2" >nul 2>&1
        if not errorlevel 1 (
            "%CODEX_PYTHON%" "%~dp0outdoor_vision_app.py"
            goto :end
        )
    )
)

echo Outdoor Vision CV could not find Python with Tkinter and its required packages.
echo.
echo Install Python 3 from python.org, ensure Tcl/Tk is selected, then run:
echo     python -m pip install -r "%~dp0requirements.txt"
echo.
pause

:end
endlocal

