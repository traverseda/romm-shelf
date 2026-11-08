@echo off
rem Install romm-shelf inside WSL2 (docs/wsl.md).
rem
rem   install-wsl.bat --plan      print every change; change nothing
rem   install-wsl.bat             install or update
rem   install-wsl.bat --ui        start the UI server and open it in Windows
rem   install-wsl.bat --verify    check an existing install
rem   install-wsl.bat --uninstall remove the AppImage and launcher
rem
rem Everything else (--appimage, --version, --packs, --no-userns, ...) goes to
rem install-wsl.sh inside the distribution.

setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0"
set "DISTRO=Ubuntu-24.04"
set "RAW=https://raw.githubusercontent.com/traverseda/romm-shelf/main/packaging/wsl/install-wsl.sh"
set "PLAN="
set "UI="
set "ACTION="
set "SHELL_ARGS="
set "ORIGINAL=%*"

:parse
if "%~1"=="" goto parsed
if /I "%~1"=="--plan" (set "PLAN=1" & shift & goto parse)
if /I "%~1"=="--ui" (set "UI=1" & shift & goto parse)
if /I "%~1"=="--uninstall" (set "ACTION=uninstall" & shift & goto parse)
if /I "%~1"=="--verify" (set "ACTION=verify" & shift & goto parse)
if /I "%~1"=="--help" goto usage
if /I "%~1"=="-h" goto usage
if /I "%~1"=="--distro" (
  if "%~2"=="" (echo error: --distro needs a name & exit /b 2)
  set "DISTRO=%~2"
  shift & shift & goto parse
)
rem Resolve a relative Windows AppImage path before self-elevation changes the
rem working directory. A path that starts with / or ~ is already a WSL path.
if /I "%~1"=="--appimage" (
  if "%~2"=="" (echo error: --appimage needs a file & exit /b 2)
  set "AV=%~2"
  if "!AV:~0,1!"=="/" (
    set SHELL_ARGS=!SHELL_ARGS! --appimage "!AV!"
  ) else if "!AV:~0,1!"=="~" (
    set SHELL_ARGS=!SHELL_ARGS! --appimage "!AV!"
  ) else (
    for %%f in ("!AV!") do set SHELL_ARGS=!SHELL_ARGS! --appimage "%%~ff"
  )
  shift & shift & goto parse
)
set SHELL_ARGS=!SHELL_ARGS! "%~1"
shift
goto parse

:parsed
rem Only the install path needs Windows administrator rights (wsl --update,
rem wsl --install, DISM). The UI, verify and uninstall do not.
set "NEED_ADMIN=1"
if "%PLAN%"=="1" set "NEED_ADMIN="
if "%UI%"=="1" set "NEED_ADMIN="
if not "%ACTION%"=="" set "NEED_ADMIN="
if defined NEED_ADMIN (
  fltmc >nul 2>&1
  if errorlevel 1 (
    echo Requesting administrator privileges...
    if "!ORIGINAL!"=="" (
      powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
      powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '!ORIGINAL!' -Verb RunAs"
    )
    exit /b
  )
)

for /f "usebackq delims=" %%v in (`powershell -NoProfile -Command "[Environment]::OSVersion.Version.Build"`) do set "BUILD=%%v"
if defined BUILD if %BUILD% LSS 19044 (
  echo error: Windows 10 build 19044 or newer is required for WSLg.
  exit /b 1
)

where wsl.exe >nul 2>&1
if errorlevel 1 goto no_wsl

if "%PLAN%"=="1" (
  echo   + wsl --update
  echo   + wsl --set-default-version 2
) else (
  wsl.exe --update
  if errorlevel 1 echo warning: wsl --update failed; install the Microsoft Store WSL if GUI apps are missing.
  wsl.exe --set-default-version 2
  if errorlevel 1 echo warning: could not select WSL 2; enable virtualization (SVM/VT-x) in the BIOS.
)

wsl.exe --version >nul 2>&1
if errorlevel 1 echo warning: this WSL build has no WSLg; install "Windows Subsystem for Linux" from the Microsoft Store.

wsl.exe -d %DISTRO% -- true >nul 2>&1
if errorlevel 1 goto install_distro
goto have_distro

:install_distro
if "%PLAN%"=="1" (
  echo   + wsl --install -d %DISTRO%
  echo     ^(finish the Linux user prompt, then this batch continues; no distro is created by --plan^)
  goto plan_linux_missing
)
echo Installing %DISTRO%...
wsl.exe --install -d %DISTRO%
echo Finish creating the Linux username and password in the window that opened,
echo then press any key here.
pause >nul
wsl.exe -d %DISTRO% -- true >nul 2>&1
if errorlevel 1 (
  echo error: %DISTRO% did not start. Open it once from the Start menu, then run this again.
  exit /b 1
)

:have_distro
set "LSCRIPT=%ROOT%install-wsl.sh"
if not exist "!LSCRIPT!" (
  set "LSCRIPT=%TEMP%\romm-shelf-install-wsl.sh"
  if "%PLAN%"=="1" (
    echo   + download !RAW!
    goto plan_linux
  )
  echo Downloading the Linux installer...
  curl.exe -fsSL -o "!LSCRIPT!" "%RAW%"
  if errorlevel 1 goto no_script
)
set "WSCRIPT="
for /f "usebackq delims=" %%p in (`wsl.exe -d %DISTRO% -- wslpath -a "!LSCRIPT!" 2^>nul`) do set "WSCRIPT=%%p"
if not defined WSCRIPT goto no_script

set "PLANARG="
if "%PLAN%"=="1" set "PLANARG=--plan"
if /I "%ACTION%"=="uninstall" (
  wsl.exe -d %DISTRO% -- bash "!WSCRIPT!" --uninstall !PLANARG! !SHELL_ARGS!
  goto done
)
if /I "%ACTION%"=="verify" (
  wsl.exe -d %DISTRO% -- bash "!WSCRIPT!" --verify !PLANARG! !SHELL_ARGS!
  goto done
)
if "%PLAN%"=="1" goto plan_linux
if "%UI%"=="1" goto run_ui

echo Installing romm-shelf inside %DISTRO%...
wsl.exe -d %DISTRO% -- bash "!WSCRIPT!" !SHELL_ARGS!
if errorlevel 1 goto failed

echo Restarting WSL so systemd starts...
wsl.exe --terminate %DISTRO%
wsl.exe -d %DISTRO% -- bash "!WSCRIPT!" --finish !SHELL_ARGS!
if errorlevel 1 goto failed

echo.
echo Done. Open a WSL terminal with: wsl -d %DISTRO%
goto done

:plan_linux
echo.
if defined WSCRIPT (
  wsl.exe -d %DISTRO% -- bash "!WSCRIPT!" --plan
) else (
  echo Linux side: install-wsl.sh --plan  ^(inside %DISTRO%^)
)
goto done

:plan_linux_missing
echo.
echo Linux side will print its own plan once %DISTRO% exists.
goto done

:run_ui
wsl.exe -d %DISTRO% -- true >nul 2>&1
if errorlevel 1 (
  echo error: %DISTRO% is not installed. Run install-wsl.bat first.
  exit /b 1
)
echo Starting the romm-shelf UI server in a new window...
start "romm-shelf UI" wsl.exe -d %DISTRO% -- bash -lc "romm-shelf serve --ui"
timeout /t 3 >nul
start "" "http://127.0.0.1:8766"
goto done

:no_wsl
if "%PLAN%"=="1" (
  echo   + dism /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
  echo   + dism /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
  echo   + restart Windows, then run this again
  goto done
)
echo Enabling WSL...
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
echo Restart Windows, then run this batch again.
exit /b 0

:no_script
echo error: could not find or download install-wsl.sh.
echo Put install-wsl.sh beside this batch, or set it up by hand inside WSL.
exit /b 1

:usage
echo Install romm-shelf inside WSL2. See docs\wsl.md.
echo.
echo   install-wsl.bat --plan       print every change; change nothing
echo   install-wsl.bat              install or update
echo   install-wsl.bat --ui         start the UI server and open it in Windows
echo   install-wsl.bat --verify     check an existing install
echo   install-wsl.bat --uninstall  remove the AppImage and launcher
echo   install-wsl.bat --distro NAME
echo.
echo Everything else (--appimage, --version, --packs, --no-userns, ...) goes
echo to install-wsl.sh inside the distribution.
exit /b 0

:failed
endlocal
exit /b 1

:done
endlocal
exit /b 0
