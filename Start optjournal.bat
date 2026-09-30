@echo off
rem Double-click this file to start optjournal. Keep the window open while you
rem use it; close it to stop optjournal.
rem
rem The first start installs uv (the tool that runs optjournal) and Python, which
rem takes a minute. Everything else happens in the page, including updates.

cd /d "%~dp0"
set "UV="
for /f "delims=" %%i in ('where uv 2^>nul') do if not defined UV set "UV=%%i"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV (
  echo First start: installing uv, the tool that runs optjournal...
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "UV=%USERPROFILE%\.local\bin\uv.exe"
)
if not exist "%UV%" (
  echo Could not install uv. Check your internet connection and try again.
  pause
  exit /b 1
)

rem ONE LINE, on purpose: cmd.exe re-reads a .bat file from disk after each
rem command, so an update that replaced this file mid-run would otherwise run
rem whatever now sits at that offset. A line is parsed whole before it runs.
"%UV%" run --no-project --python 3.12 launcher\app.py & (if errorlevel 1 pause) & exit /b
