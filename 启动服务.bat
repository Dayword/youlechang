@echo off
chcp 936 >nul
title 鸭先知 - 智能问答与运营系统 (conda rag)
setlocal

rem ============================================================
rem  大黄鸭游乐园 AI 智能客服 - 后端启动脚本
rem ------------------------------------------------------------
rem  【编码要求】本文件必须保存为 GBK(cp936) + CRLF 换行。
rem   不要改成 UTF-8：cmd.exe 读 bat 用的是控制台代码页，
rem   文件编码一旦和控制台不一致，含中文的行会被拦腰截断，
rem   报一堆 "'xxx' 不是内部或外部命令，也不是可运行的程序"。
rem   用记事本另存为时选「ANSI」即可（即 GBK）。
rem
rem  【必须用 conda 的 rag 环境启动】
rem   同机实测 PyMySQL 单次建连：
rem     conda rag  0.012s/次     系统 Python  0.612s/次（49 倍）
rem   一次问答链路有多次 DB 操作（会话记忆/办卡推荐/日志落库/账户查询），
rem   用错解释器会让普通问答从 6s 涨到 20~40s、Redis 缓存命中也要 1s+。
rem
rem  【历史坑】旧脚本用 call activate.bat rag 激活环境，但当前 conda 安装里
rem   并没有 Scripts\activate.bat，激活失败后静默回退到系统 Python，
rem   表现为优化过一阵又变慢，且很难发现。
rem   现在直接调用环境内的 python.exe，不再依赖任何 activate 脚本。
rem ============================================================

rem ---- 定位 conda rag 环境解释器（按顺序取第一个存在的）----
set "PY="
for %%P in ("C:\Users\86133\.conda\envs\rag\python.exe" "D:\software\miniconda3\envs\rag\python.exe" "C:\ProgramData\miniconda3\envs\rag\python.exe") do @if not defined PY if exist "%%~P" set "PY=%%~P"

if not defined PY (
    echo   [错误] 未找到 conda rag 环境解释器，已尝试以下路径:
    echo          C:\Users\86133\.conda\envs\rag\python.exe
    echo          D:\software\miniconda3\envs\rag\python.exe
    echo          C:\ProgramData\miniconda3\envs\rag\python.exe
    echo   请修改本脚本中的候选路径，或先创建环境:  conda create -n rag python=3.10
    pause
    exit /b 1
)

echo.
echo   ============================================
echo     鸭先知 · 大黄鸭游乐园
echo     智能问答与运营系统 v2.0
echo   ============================================
echo.

echo   [1/4] 校验 conda rag 环境...
"%PY%" -c "import sys;print('         interpreter:',sys.executable)"
if errorlevel 1 (
    echo   [错误] conda rag 环境不可用，请检查环境完整性。
    pause
    exit /b 1
)

echo   [2/4] 进入项目目录...
cd /d "%~dp0"
echo         %CD%

echo   [3/4] 检查 8000 端口占用...
if exist "%TEMP%\rag_health.txt" del "%TEMP%\rag_health.txt" >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { (Invoke-WebRequest http://127.0.0.1:8000/health -TimeoutSec 4 -UseBasicParsing).StatusCode } catch { 'DOWN' }" > "%TEMP%\rag_health.txt"
set "RAGHEALTH="
set /p RAGHEALTH=<"%TEMP%\rag_health.txt"
if "%RAGHEALTH%"=="200" (
    echo   [提示] 后端已经在运行了（8000 健康检查通过），不需要重复启动。
    echo          直接去用即可: http://localhost:8000/
    echo          确实想重启: 先关掉那个后端窗口 ^(或结束 python.exe^) 再运行本脚本。
    pause
    exit /b 0
)
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo   [提示] 8000 端口被占用，但不是本项目的健康后端。
    echo          若接下来启动失败，请先结束占用 8000 的进程再试。
)

echo   [4/4] 启动服务（conda rag）...
echo         服务地址: http://localhost:8000
echo         接口文档: http://localhost:8000/docs
echo         时延验收: python _probe_latency.py
echo.

rem ------------------------------------------------------------
rem 下面的输出交给 Python：切到 UTF-8 控制台，避免 emoji 日志崩。
rem 注意：从这里往下不要再出现中文，否则又会触发上面的编码错位。
rem ------------------------------------------------------------
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul

"%PY%" main.py

chcp 936 >nul
echo.
echo   [服务已退出] 若为异常退出，请查看 logs\app.log
pause
endlocal
