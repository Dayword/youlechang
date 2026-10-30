@echo off
chcp 936 >nul
title 鸭先知 - 小程序重新编译（自动探测局域网IP）
setlocal

echo.
echo   ============================================
echo     鸭先知 · 大黄鸭游乐园
echo     智能问答与运营系统 v2.0
echo   ============================================
echo.
echo   [1/4] 自动探测本机局域网 IP ...
echo.

rem ------------------------------------------------------------
rem  【探测口径】（2026-09-16 修正，别再改回单条件写法）
rem   旧写法只取「第一个带默认网关的网卡」，被代理软件的 TUN 虚拟网卡骗过：
rem   Clash Verge / Mihomo 的 TUN 网卡 IP=198.18.0.1、网关=198.18.0.2，
rem   同样带默认网关且往往排在 WLAN 前面 —— 编译进小程序的地址变成
rem   http://198.18.0.1:8000（fake-ip 段），手机与模拟器都路由不到，
rem   表现是「登录不上去、所有请求都连不上」。
rem   现在：先排除虚拟网卡与保留地址段，再优先取 WLAN/Ethernet。
rem ------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -ne $null } | Where-Object { $_.IPv4Address.IPAddress -notlike '127.*' -and $_.IPv4Address.IPAddress -notlike '169.254.*' -and $_.IPv4Address.IPAddress -notlike '198.18.*' -and $_.IPv4Address.IPAddress -notlike '198.19.*' } | Where-Object { $_.InterfaceAlias -notmatch 'Mihomo|Clash|vEthernet|VMware|VirtualBox|Hyper-V|Loopback|TUN|Tailscale|ZeroTier|Radmin|Bluetooth' }; $w = $c | Where-Object { $_.InterfaceAlias -match 'WLAN|Wi-?Fi|Ethernet' } | Select-Object -First 1; if (-not $w) { $w = $c | Select-Object -First 1 }; if ($w) { $w.IPv4Address.IPAddress }" > "%TEMP%\lanip.txt"
set /p LANIP=<"%TEMP%\lanip.txt"

if "%LANIP%"=="" set "LANIP=127.0.0.1"
set "TARO_APP_API_BASE=http://%LANIP%:8000"

echo         探测到本机 IP : %LANIP%
echo   [2/4] 后端地址     = %TARO_APP_API_BASE%
echo         （已自动注入，无需再手改 config/index.ts）
if "%LANIP%"=="127.0.0.1" echo         [警告] 只探测到回环地址，真机预览会连不上，请检查网络。
echo.

cd /d "%~dp0miniprogram"
echo   [3/4] 开始编译小程序 ...
echo.
call npm run build:weapp
if errorlevel 1 (
    echo.
    echo   [失败] 编译报错了，请把上面的错误信息发出来。
    pause
    exit /b 1
)

echo.
echo   [4/4] 回读编译产物，确认地址真的写进了包 ...
set "BAKED="
for /f "usebackq delims=" %%A in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = Get-Content -Raw -Encoding UTF8 'dist\common.js'; ([regex]::Matches($s, 'http://[0-9.]+:8000') | ForEach-Object { $_.Value } | Select-Object -Unique) -join ','"`) do set "BAKED=%%A"
echo        包内地址 = %BAKED%
if /i "%BAKED%"=="%TARO_APP_API_BASE%" (
    echo        [OK] 小程序已指向后端 %TARO_APP_API_BASE%
) else (
    echo        [警告] 包内地址与探测结果不一致！检查 config/index.ts 或代理软件是否又抢了默认网关。
)
echo.
echo   ============================================
echo   接着在微信开发者工具里点「编译」刷新即可。
echo   手机真机预览：手机与本机连同一个 Wi-Fi，
echo   开发者工具「详情 - 本地设置」勾选「不校验合法域名」。
echo   ============================================
pause
endlocal
