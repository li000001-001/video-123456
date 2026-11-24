@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   视频分类助手 - 启动中
echo ============================================
echo.
python app.py
echo.
echo 服务已停止，按任意键关闭窗口。
pause >nul
