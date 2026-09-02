@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
title 极光 Agent OS - 启动器
cd /d "%~dp0"

REM 启动模式：命令行参数优先（start.bat mock / start.bat real）
REM 否则按 .env 是否配置 AURORA_API_KEY 自动选择：有则真实模式，无则演示模式
set "MODE=%~1"

if /i "%MODE%"=="real" goto :real
if /i "%MODE%"=="mock" goto :mock

set "HAS_KEY=0"
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /i "%%A"=="AURORA_API_KEY" (
        if not "%%B"=="" set "HAS_KEY=1"
    )
)
if "%HAS_KEY%"=="1" (goto :real) else (goto :mock)

:mock
echo ================================================
echo   ▲ 极光 Agent OS - 演示模式 (mock, 无需 API Key)
echo ================================================
python run.py --mock
goto :done

:real
echo ================================================
echo   ▲ 极光 Agent OS - 真实 API 模式 (读取 .env)
echo ================================================
python run.py
goto :done

:done
echo.
echo   服务已退出。
pause
