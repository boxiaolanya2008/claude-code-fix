@echo off
REM ==============================================================================
REM Claude Code Proxy — Windows 一键启动
REM 用法: start.bat -k "sk-xxx" -m "deepseek-chat" -b "https://api.deepseek.com/v1"
REM ==============================================================================

python "%~dp0server.py" %*
pause
