# -*- coding: utf-8 -*-
"""PyInstaller 桌面后端入口；普通用户无需安装 Python。"""

import os

import uvicorn

from app.main import app


def main() -> None:
    host = os.environ.get("KUMIPLAYER_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("KUMIPLAYER_PORT", "37821"))
    except ValueError:
        port = 37821
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
