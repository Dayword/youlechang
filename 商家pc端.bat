
@echo off
chcp 65001 >nul
title 鸭先知 - 商家 PC 管理端

echo.
echo   ============================================
echo     鸭先知 · 大黄鸭游乐园
echo     智能问答与运营系统 v2.0
echo   ============================================
echo.

echo   [1/2] 进入项目目录...
rem NOTE: cd into this bat's own folder (%~dp0), never a hardcoded absolute path.
rem A hardcoded path broke once after the project folder got renamed: cd failed
rem silently, vite started in the repo root, port was up but the page 404'd.
rem (Also: do not add multi-line non-ASCII comments here - with chcp 65001 cmd
rem  mis-parses them and prints a bogus "not recognized" error.)
cd /d "%~dp0code"

echo   [2/2] 启动商家pc服务...
echo.
npx vite --host

pause
