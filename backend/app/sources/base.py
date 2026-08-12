"""SourceAdapter 基础接口

每个来源实现自己的适配器。
来源适配器只负责把目录树或本地目录变成 RawFile[]，不判断媒体内容。
"""

from abc import ABC, abstractmethod

from app.catalog.models import DirectoryPage, SourceNodeInput
from app.raw.models import RawSnapshot


class SourceAdapter(ABC):
    """来源适配器抽象基类"""

    @property
    @abstractmethod
    def source_id(self) -> str:
        """来源标识，如 'pan115'、'baidu'、'local'"""
        ...

    @property
    @abstractmethod
    def mirror_namespace(self) -> str:
        """镜像输出命名空间，如 '115'、'baidu'、'local'"""
        ...

    @abstractmethod
    def parse(self, input_path: str, source_root: str) -> RawSnapshot:
        """解析目录树或扫描本地目录，输出 RawSnapshot

        参数:
            input_path: 输入文件路径（目录树 txt）或本地目录路径
            source_root: 来源挂载根目录

        返回:
            RawSnapshot 包含所有 RawFile
        """
        ...

    @abstractmethod
    def build_real_path(self, relative_path: str, source_root: str) -> str:
        """根据相对路径和来源根目录，拼接真实播放路径

        参数:
            relative_path: 相对路径，如 动画/冰菓.2012/视频.mkv
            source_root: 来源挂载根目录，如 H:\115open

        返回:
            真实播放路径
        """
        ...

    # ---- Source Catalog 契约（任务 3） ----

    @property
    def capabilities(self) -> dict:
        """能力声明：``{"paginated": bool}``。"""
        return {"paginated": False}

    def enumerate_directory(self, remote_path: str, page: int = 1, per_page: int = 100) -> DirectoryPage:
        """分页目录枚举（paginated 来源实现；不支持时抛 NotImplementedError）。"""
        raise NotImplementedError(f"{self.source_id} 不支持分页目录枚举")

    def snapshot_entries(self, input_path: str, source_root: str) -> list[SourceNodeInput]:
        """一次性完整快照（one-shot 来源，如目录树 TXT）。"""
        raise NotImplementedError(f"{self.source_id} 不支持一次性快照导入")
