"""启动入口：python app.py"""
from __future__ import annotations

from server.main import app
from server.settings import settings

if __name__ == "__main__":
    import uvicorn

    host = settings.get("server", "host", default="0.0.0.0")
    port = int(settings.get("server", "port", default=8000))
    print("=" * 52)
    print(" 视频分类助手 已启动")
    print(f" 电脑访问: http://127.0.0.1:{port}")
    print(f" 手机访问: http://<本机局域网IP>:{port}")
    print("=" * 52)
    uvicorn.run(app, host=host, port=port, log_level="info")
