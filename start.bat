@echo off
setlocal
title AuroraAgent - 启动器
cd /d "%~dp0"

REM 探测 python（python / python3 / py），避免双击时 PATH 不含 python 而闪退
set "PY=python"
where python >nul 2>&1 || set "PY="
if not defined PY (where python3 >nul 2>&1 && set "PY=python3")
if not defined PY (where py >nul 2>&1 && set "PY=py")
if not defined PY (
  echo [错误] 未找到 python，请先安装 Python 3.11+ 并加入 PATH。
  echo         下载：https://www.python.org  （安装时勾选 Add python.exe to PATH）
  pause
  exit /b 1
)

REM 启动模式：命令行参数优先（start.bat mock / start.bat real）
REM 否则按 .env 是否配置 AURORA_API_KEY 自动选择：有则真实模式，无则演示模式
set "MODE=%~1"

if /i "%MODE%"=="real" goto :real
if /i "%MODE%"=="mock" goto :mock

set "HAS_KEY=0"
findstr /r "^AURORA_API_KEY=.+" .env >nul 2>&1 && set "HAS_KEY=1"
if "%HAS_KEY%"=="1" (goto :real) else (goto :mock)

:mock
echo ================================================
echo   [AuroraAgent] 演示模式 (mock, 无需 API Key)
echo ================================================
%PY% run.py --mock
goto :done

:real
echo ================================================
echo   [AuroraAgent] 真实 API 模式 (读取 .env)
echo ================================================
%PY% run.py
goto :done

:done
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo [提示] 进程退出码 %RC%（非主动关闭请查看上方报错）
echo.
echo   服务已退出。按任意键关闭窗口...
pause
