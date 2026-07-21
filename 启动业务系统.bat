@echo off
setlocal EnableExtensions
chcp 65001 >nul
title APS 业务系统启动器

set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "PORT=8080"
set "ACTION=%~1"

if not defined ALGORITHM_BASE_URL set "ALGORITHM_BASE_URL=http://127.0.0.1:8000"
if not defined DATABASE_PATH set "DATABASE_PATH=%ROOT%data\business.db"
if not defined FACTORY_CODE set "FACTORY_CODE=FACTORY01"
if not defined SESSION_SECRET set "SESSION_SECRET=local-development-secret-please-change"

if "%ACTION%"=="" set "ACTION=restart"
if /I "%ACTION%"=="start" goto prepare
if /I "%ACTION%"=="restart" goto prepare
if /I "%ACTION%"=="stop" goto manage_process
goto usage

:prepare
cd /d "%ROOT%"
echo.
echo ========================================
echo   APS 生产排程业务系统
echo   操作: %ACTION%
echo   页面: http://127.0.0.1:%PORT%
echo   算法: %ALGORITHM_BASE_URL%
echo ========================================
echo.

if not exist "%PYTHON%" (
    echo [1/5] 未找到业务虚拟环境，正在创建 .venv ...
    python -m venv "%ROOT%.venv"
    if errorlevel 1 goto python_error
) else (
    echo [1/5] 已找到业务虚拟环境。
)

"%PYTHON%" -c "import fastapi, lunardate, uvicorn, pydantic, httpx" >nul 2>&1
if errorlevel 1 (
    echo [2/5] 依赖不完整，正在安装 requirements.txt ...
    "%PYTHON%" -m pip install -r "%ROOT%requirements.txt"
    if errorlevel 1 goto dependency_error
) else (
    echo [2/5] 业务依赖检查通过。
)

echo [3/5] 正在检查算法系统连接 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try {" ^
  "  $response = Invoke-RestMethod -Uri ($env:ALGORITHM_BASE_URL + '/health') -TimeoutSec 3;" ^
  "  if ($response.status -eq 'UP') { exit 0 } else { exit 1 }" ^
  "} catch { exit 1 }"

if errorlevel 1 (
    echo [警告] 当前无法连接算法系统，业务页面仍会启动，但不能完成排程任务。
    echo        请先运行项目根目录下的 启动算法系统.bat。
) else (
    echo       算法系统连接正常。
)

:manage_process
cd /d "%ROOT%"
if /I "%ACTION%"=="stop" (
    echo 正在停止业务系统 ...
) else if /I "%ACTION%"=="restart" (
    echo [4/5] 正在检查并停止旧业务服务 ...
) else (
    echo [4/5] 正在检查业务服务端口 ...
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$connections = @(Get-NetTCPConnection -State Listen -LocalPort %PORT% -ErrorAction SilentlyContinue);" ^
  "$processIds = @($connections | Select-Object -ExpandProperty OwningProcess -Unique);" ^
  "foreach ($processId in $processIds) {" ^
  "  $process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $processId);" ^
  "  if ($process.CommandLine -like '*business_app.main:app*') {" ^
  "    if ('%ACTION%' -eq 'start') { exit 10 };" ^
  "    Stop-Process -Id $processId -Force -ErrorAction Stop;" ^
  "  } else {" ^
  "    Write-Host ('端口 %PORT% 被其他程序占用: PID=' + $processId + ' ' + $process.CommandLine);" ^
  "    exit 20;" ^
  "  }" ^
  "}" ^
  "exit 0"

set "PROCESS_RESULT=%ERRORLEVEL%"
if "%PROCESS_RESULT%"=="20" goto port_error
if "%PROCESS_RESULT%"=="10" goto already_running
if not "%PROCESS_RESULT%"=="0" goto stop_error

if /I "%ACTION%"=="stop" (
    echo 业务系统已停止。
    goto success_exit
)

timeout /t 1 /nobreak >nul
echo [5/5] 正在启动业务 HTTP 服务 ...
start "APS Business System - Port %PORT%" /D "%ROOT%" "%PYTHON%" -m uvicorn business_app.main:app --host 127.0.0.1 --port %PORT%

echo 正在等待健康检查 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ok = $false;" ^
  "for ($index = 0; $index -lt 20; $index++) {" ^
  "  try {" ^
  "    $response = Invoke-RestMethod -Uri 'http://127.0.0.1:%PORT%/health' -TimeoutSec 2;" ^
  "    if ($response.status -eq 'UP') { $ok = $true; break }" ^
  "  } catch {}" ^
  "  Start-Sleep -Seconds 1;" ^
  "}" ^
  "if ($ok) { exit 0 } else { exit 1 }"

if errorlevel 1 goto health_error

echo.
echo 业务系统启动成功。
echo 访问页面: http://127.0.0.1:%PORT%
echo API 文档: http://127.0.0.1:%PORT%/docs
echo.
goto success_exit

:already_running
echo.
echo 业务系统已经运行，无需重复启动。
echo 如需强制重启，请双击本文件，或执行:
echo   "%~nx0" restart
echo.
goto success_exit

:usage
echo 用法:
echo   "%~nx0"          默认重启业务系统
echo   "%~nx0" restart  重启业务系统
echo   "%~nx0" start    仅在未运行时启动
echo   "%~nx0" stop     停止业务系统
goto error_exit

:python_error
echo.
echo [失败] 无法创建 Python 虚拟环境。
echo 请确认已经安装 Python 3.12，并且 python 命令可用。
goto error_exit

:dependency_error
echo.
echo [失败] 业务依赖安装失败，请检查网络和 requirements.txt。
goto error_exit

:port_error
echo.
echo [失败] 端口 %PORT% 被非业务系统程序占用，为避免误停其他程序，启动器已终止。
goto error_exit

:stop_error
echo.
echo [失败] 停止旧业务服务时发生错误。
goto error_exit

:health_error
echo.
echo [失败] 业务进程已启动，但健康检查未通过。
echo 请查看新打开的 "APS Business System" 窗口中的错误信息。
goto error_exit

:success_exit
echo 按任意键关闭启动器窗口，服务窗口会继续运行。
pause >nul
exit /b 0

:error_exit
echo.
echo 按任意键退出。
pause >nul
exit /b 1
