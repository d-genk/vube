@echo off
setlocal EnableDelayedExpansion
rem ---------------------------------------------------------------------------
rem VubeCropper.cmd -- double-click launcher for vube_cropper.py.
rem
rem First run: builds a private .venv beside this file and installs
rem requirements.txt into it (a few minutes, one time only).
rem Every run after that: opens the window straight away.
rem
rem This replaces the old dist\VubeCropper.exe. There is no compiled binary
rem here -- it runs the .py source under the signed python.exe from python.org,
rem which is what lets it past the unsigned-executable policy.
rem ---------------------------------------------------------------------------

cd /d "%~dp0"

set "VENV=%~dp0.venv"
set "STAMP=%VENV%\.requirements-installed"
set "PYW=%VENV%\Scripts\pythonw.exe"
set "PY=%VENV%\Scripts\python.exe"

rem --- Locate a system Python to build the venv with -------------------------
if not exist "%PY%" (
    set "BOOTSTRAP="
    py -3 -c "import sys" >nul 2>&1 && set "BOOTSTRAP=py -3"
    if not defined BOOTSTRAP (
        python -c "import sys" >nul 2>&1 && set "BOOTSTRAP=python"
    )
    if not defined BOOTSTRAP (
        echo.
        echo   Python was not found on this computer.
        echo.
        echo   Install it from https://www.python.org/downloads/windows/
        echo   During setup, tick "Add python.exe to PATH".
        echo   Then double-click this file again.
        echo.
        pause
        exit /b 1
    )

    echo.
    echo   First-time setup. This takes a few minutes and only happens once.
    echo.
    echo   [1/3] Creating a private Python environment...
    !BOOTSTRAP! -m venv "%VENV%"
    if errorlevel 1 (
        echo.
        echo   Could not create the environment. Send Daniel the text above.
        pause
        exit /b 1
    )
)

rem --- Install / refresh dependencies ----------------------------------------
rem The stamp is compared against requirements.txt, so editing that file
rem re-installs on the next launch instead of silently drifting.
set "NEEDS_INSTALL=1"
if exist "%STAMP%" (
    for %%A in ("%~dp0requirements.txt") do set "REQ_DATE=%%~tA"
    set /p STAMP_DATE=<"%STAMP%"
    if "!REQ_DATE!"=="!STAMP_DATE!" set "NEEDS_INSTALL=0"
)

if "%NEEDS_INSTALL%"=="1" (
    echo   [2/3] Installing the imaging libraries...
    "%PY%" -m pip install --quiet --upgrade pip
    "%PY%" -m pip install --quiet -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo.
        echo   Could not install the libraries. This is usually the network
        echo   or a proxy. Send Daniel the text above.
        pause
        exit /b 1
    )
    for %%A in ("%~dp0requirements.txt") do echo %%~tA>"%STAMP%"
    echo   [3/3] Setup finished. Starting...
)

rem --- Launch ----------------------------------------------------------------
if not exist "%PYW%" (
    echo   Setup looks incomplete -- delete the .venv folder and try again.
    pause
    exit /b 1
)

rem With arguments (--cli and friends) we need console python, or the output
rem goes nowhere: pythonw.exe has no stdout. Run it in THIS console and keep
rem the exit code, so the launcher can be used from a script.
if not "%~1"=="" (
    rem !errorlevel! not %errorlevel% -- inside a parenthesised block the %-form
    rem expands at parse time, i.e. before python has run, and always reads 0.
    "%PY%" "%~dp0vube_cropper.py" %*
    exit /b !errorlevel!
)

rem No arguments: open the window. pythonw.exe means no console sits behind it
rem (this is what PyInstaller's --windowed used to do). `start ""` lets this
rem script exit immediately rather than holding a console open for the GUI.
start "" "%PYW%" "%~dp0vube_cropper.py"
exit /b 0
