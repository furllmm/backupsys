@echo off
REM run_gui.bat
REM ============
REM Double-click launcher for Windows. Runs run_gui.py using whichever
REM Python is on PATH, from this .bat file's own folder (so it works
REM regardless of what directory Explorer happens to launch it from).
REM
REM If double-clicking closes the window instantly on an error, run this
REM from a Command Prompt instead (cmd.exe, then drag this file in and
REM press Enter) so you can actually read the error message.

cd /d "%~dp0"
python run_gui.py
if errorlevel 1 (
    echo.
    echo run_gui.py exited with an error -- see above.
    pause
)
