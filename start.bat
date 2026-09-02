@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
title 极光 Agent OS - 启动器

cd /d "%~dp0"

echo ================================================
echo   ▲ 极光 Agent OS - AuroraAgent 启动器
echo ================================================
echo.
echo   1) 演示模式 (mock, 无需 API Key)
echo   2) 真实 API 模式 (DeepSeek / 其他 OpenAI 兼容)
echo   3) 退出
echo.

set /p MODE=请选择 (1/2/3):

if "%MODE%"=="1" goto mock
if "%MODE%"=="2" goto real
if "%MODE%"=="3" exit /b 0
goto error

:mock
echo.
echo   启动演示模式 (mock)...
python run.py --mock
goto done

:real
echo.
set /p APPROACH=请输入 API 地址 (默认 https://api.deepseek.com/v1):
if "%APPROACH%"=="" set APPROACH=https://api.deepseek.com/v1
set /p KEY=请输入 API Key:
set /p MODEL=请输入模型名 (默认 deepseek-chat):
if "%MODEL%"=="" set MODEL=deepseek-chat
if "%KEY%"=="" (
  echo   [提示] 未输入 Key, 将使用 mock 模式继续演示。
  python run.py --mock
  goto done
)
python run.py --api-base "%APPROACH%" --api-key "%KEY%" --model "%MODEL%"
goto done

:error
echo   无效选项，请重新运行。
exit /b 1

:done
echo.
echo   服务已退出。
pause