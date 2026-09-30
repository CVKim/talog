@echo off
rem talog resident monitor without the console (for auto start / schtasks).
rem Settings: talog.yaml next to this file.
pushd "%~dp0"
set "EXE="
if exist "%~dp0talog.exe" set "EXE=%~dp0talog.exe"
if not defined EXE if exist "%~dp0dist\talog.exe" set "EXE=%~dp0dist\talog.exe"
set "CFG=%~dp0talog.yaml"
if not exist "%CFG%" if exist "%~dp0watch.yaml" set "CFG=%~dp0watch.yaml"
if defined EXE ( "%EXE%" run --config "%CFG%" ) else ( python -m talog run --config "%CFG%" )
pause
popd
