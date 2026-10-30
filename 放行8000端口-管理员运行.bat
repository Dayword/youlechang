@echo off
chcp 936 >nul
title 放行 8000 端口 - 管理员运行

rem 【编码要求】本文件为 GBK(cp936) + CRLF，修改时请保持，勿存成 UTF-8。
rem   原因见「启动服务.bat」顶部说明：cmd 读 bat 用控制台代码页，
rem   文件编码与之不一致时含中文的行会被截断，报"不是内部或外部命令"。

echo 正在添加防火墙入站规则：放行 TCP 8000（供小程序真机预览访问后端）...
netsh advfirewall firewall add rule name="amusement-rag-backend-8000" dir=in action=allow protocol=TCP localport=8000

if %errorlevel%==0 (
  echo [OK] 规则添加成功。手机与电脑连同一 Wi-Fi 后即可访问后端。
) else (
  echo [失败] 请右键本文件，选择「以管理员身份运行」后重试。
)

echo.
pause
