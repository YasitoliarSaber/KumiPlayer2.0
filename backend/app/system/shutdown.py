# -*- coding: utf-8 -*-
"""后端退出机制

生产环境使用 os._exit(0) 退出。
测试中通过注入 shutdown_callback 避免真实退出。
"""

import os
import threading


def request_backend_shutdown(reason: str) -> None:
    """请求后端退出

    使用短暂延迟后 os._exit(0)，给响应留时间发送。
    """
    def _do_exit():
        os._exit(0)

    threading.Timer(0.5, _do_exit).start()
