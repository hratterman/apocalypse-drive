@echo off
REM Apocalypse launcher - Windows
REM Starts kiwix-serve + llamafile, opens browser to the unified RAG UI.

setlocal enabledelayedexpansion

cd /d "%~dp0"
set "DRIVE=%cd%"
set "KIWIX_BIN=%DRIVE%\bin\win-x86_64\kiwix-serve.exe"
set "ZIM_DIR=%DRIVE%\kiwix\zim"

if not exist "%KIWIX_BIN%" (
  echo ERROR: kiwix-serve.exe not found at %KIWIX_BIN%
  pause
  exit /b 1
)

REM Detect RAM
for /f "skip=1" %%a in ('wmic computersystem get TotalPhysicalMemory') do (
  if not "%%a"=="" (
    set "RAM_BYTES=%%a"
    goto :ramdone
  )
)
:ramdone
set /a "RAM_GB=%RAM_BYTES:~0,-9%"

REM Pick model
set "MODEL="
set "MODEL_LABEL="
if !RAM_GB! GEQ 12 (
  if exist "%DRIVE%\llm\Meta-Llama-3.1-8B-Instruct.Q4_K_M.llamafile" (
    set "MODEL=%DRIVE%\llm\Meta-Llama-3.1-8B-Instruct.Q4_K_M.llamafile"
    set "MODEL_LABEL=Llama 3.1 8B"
  )
)
if "!MODEL!"=="" (
  if exist "%DRIVE%\llm\Llama-3.2-3B-Instruct.Q6_K.llamafile" (
    set "MODEL=%DRIVE%\llm\Llama-3.2-3B-Instruct.Q6_K.llamafile"
    set "MODEL_LABEL=Llama 3.2 3B"
  )
)
if "!MODEL!"=="" (
  echo ERROR: No llamafile in %DRIVE%\llm\
  pause
  exit /b 1
)

REM llamafile on Windows wants .exe extension
set "MODEL_EXE=!MODEL!.exe"
if not exist "!MODEL_EXE!" copy /b "!MODEL!" "!MODEL_EXE!" >nul

echo ===================================
echo   Apocalypse Offline Knowledge
echo ===================================
echo Drive: %DRIVE%
echo RAM:   !RAM_GB!GB
echo Model: !MODEL_LABEL!
echo.

echo Starting kiwix-serve at http://localhost:8888 ...
pushd "%DRIVE%\bin\win-x86_64"
start "Apocalypse Kiwix" /min "%KIWIX_BIN%" --port=8888 "%ZIM_DIR%\*.zim"
popd

echo Starting LLM server at http://localhost:8081 ...
start "Apocalypse LLM" /min "!MODEL_EXE!" --server --nobrowser --port 8081 --host 127.0.0.1 -ngl 999

echo Waiting for services ...
set /a "tries=0"
:waitsvc
set /a "tries+=1"
if !tries! GTR 60 goto :openui
curl -sS -o nul -w "%%{http_code}" http://localhost:8888/ 2>nul | findstr /R "200 302" >nul
if errorlevel 1 (
  timeout /t 2 /nobreak >nul
  goto :waitsvc
)
curl -sS -o nul -w "%%{http_code}" http://localhost:8081/v1/models 2>nul | findstr "200" >nul
if errorlevel 1 (
  timeout /t 2 /nobreak >nul
  goto :waitsvc
)
:openui
echo   ready.
echo.

echo Opening browser ...
start "" "%DRIVE%\Apocalypse.html"

echo.
echo Close this window to stop the services. (Background tasks must be ended manually.)
echo.
pause
