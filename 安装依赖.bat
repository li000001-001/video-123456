@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   安装运行依赖（使用清华镜像加速）
echo ============================================
echo.
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
echo.
echo 正在安装字幕组件（体积较大，可跳过，跳过则无法本地识别字幕）
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-subtitle.txt
echo.
echo 安装完成，按任意键关闭窗口。
pause >nul
