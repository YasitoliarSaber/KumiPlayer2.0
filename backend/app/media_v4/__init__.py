"""KumiPlayer 后端 V4 媒体域。

V4 将来源观察、纯解析事实、作品图、确认修订和执行任务分开。

schema 版本号只有一个事实来源：``app.media_v4.persistence.schema_v4.V4_SCHEMA_VERSION``
（并同步到 ``persistence/database.py`` 的 ``PRAGMA user_version`` 字面量）。
这里曾经留过一份陈旧的 ``V4_SCHEMA_VERSION = 4`` 副本，已删除——多份版本号是
"改了常量忘了字面量"这类启动级故障的温床。
"""
