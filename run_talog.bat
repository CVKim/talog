@echo off
rem talog launcher. Drag and drop a log folder onto this file.
pushd "%~dp0"

set "LOGS=%~1"
if "%LOGS%"=="" (
    echo.
    echo  [talog] Enter log folder path:
    set /p LOGS=  LOG DIR: 
)
if "%LOGS%"=="" ( echo No path given. & pause & popd & exit /b 1 )

echo.
echo  [talog] Recipe folder - optional. Press Enter to skip.
set /p RECIPE=  RECIPE DIR: 

set "EXE="
if exist "%~dp0talog.exe" set "EXE=%~dp0talog.exe"
if not defined EXE if exist "%~dp0dist\talog.exe" set "EXE=%~dp0dist\talog.exe"

if defined EXE goto run_exe

python -c "import talog" 2>nul
if errorlevel 1 (
    echo [ERROR] talog.exe not found in this folder and no Python talog package.
    echo         Put talog.exe next to this bat file.
    pause
    popd
    exit /b 1
)
if "%RECIPE%"=="" ( python -m talog "%LOGS%" --open ) else ( python -m talog "%LOGS%" --recipe "%RECIPE%" --open )
goto done

:run_exe
if "%RECIPE%"=="" ( "%EXE%" "%LOGS%" --open ) else ( "%EXE%" "%LOGS%" --recipe "%RECIPE%" --open )

:done
echo.
pause
popd