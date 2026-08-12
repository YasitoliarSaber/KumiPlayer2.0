"""OpenList 网盘目录来源适配器。

只负责把 KumiPlayer 自有格式的 OpenList 目录清单（见
``app/integrations/openlist/manifest.py``）转换为 RawSnapshot / RawFile，
不发起任何网络请求。

路径合同：
- ``relative_path`` 相对用户选中的远端目录（``remote_locator``）；
- ``real_path = 本地 source_root / relative_path``，绝不使用远端 URL；
- 远端条目的存储侧 ``path`` 字段不进入本模块。
"""

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from app.integrations.openlist.manifest import read_manifest
from app.integrations.openlist.providers import compat_ingest, compat_provider
from app.raw.models import RawFile, RawSnapshot
from app.recognition.resource_type import _EXT_TYPE_MAP
from app.sources.base import SourceAdapter

_SYSTEM_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}


def _split_name_ext(name: str) -> tuple[str, str]:
    """分离文件名和扩展名"""
    dot_idx = name.rfind(".")
    if dot_idx <= 0:
        return name, ""
    return name[:dot_idx], name[dot_idx:]


def _resource_hint(ext: str) -> str:
    return _EXT_TYPE_MAP.get(ext.lower(), "other")


def _make_stable_id(source: str, relative_path: str) -> str:
    """生成稳定的 RawFile ID（同一相对路径重复解析时保持稳定）。"""
    return hashlib.md5(f"{source}:{relative_path}".encode()).hexdigest()


def make_snapshot_id(remote_locator: str, sha256: str) -> str:
    """由远端定位 + 内容哈希生成稳定的快照 ID。"""
    content = f"openlist:{remote_locator}:{sha256[:12]}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()


class OpenListAdapter(SourceAdapter):
    """OpenList 目录清单适配器"""

    @property
    def source_id(self) -> str:
        return "openlist"

    @property
    def mirror_namespace(self) -> str:
        return "openlist"

    def parse(self, input_path: str, source_root: str) -> RawSnapshot:
        """读取 OpenList 目录清单，输出 RawSnapshot

        参数:
            input_path: KumiPlayer 自有格式的清单 JSON 路径
            source_root: 本地挂载根目录（映射后，如 G:\\quark\\动画）

        返回:
            RawSnapshot 只包含文件节点的 RawFile
        """
        data = read_manifest(Path(input_path))
        if not data:
            raise ValueError("OpenList 目录清单缺失或格式非法，请重新扫描")

        remote_locator = str(data.get("remote_locator") or "/")
        snapshot_id = str(data.get("manifest_id") or "")
        provider_id = str(data.get("provider_id") or compat_provider("openlist"))
        ingest_method = str(data.get("ingest_method") or compat_ingest("openlist"))
        source_route_id = str(data.get("source_route_id") or "")
        # 选中目录名：relative 相对它计算，无作品容器层时它本身就是系列名候选
        root_container = PurePosixPath(remote_locator).name or ""
        entries = data.get("entries") or []
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()

        files: list[RawFile] = []
        for raw in entries:
            if not isinstance(raw, dict):
                continue
            if raw.get("is_dir"):
                continue
            name = str(raw.get("name") or "")
            remote_path = str(raw.get("remote_path") or "")
            if not name or not remote_path:
                continue
            if name.lower() in _SYSTEM_FILES:
                continue

            # 相对路径 = 远端路径相对于用户选中的远端目录
            relative = _relative_to_locator(remote_path, remote_locator)
            if relative is None:
                continue

            parts = list(PurePosixPath(relative).parts)
            if not parts:
                continue
            stem, ext = _split_name_ext(name)
            hint = _resource_hint(ext)
            file_id = _make_stable_id("openlist", relative)

            files.append(
                RawFile(
                    id=file_id,
                    snapshot_id=snapshot_id,
                    source="openlist",
                    source_root=source_root,
                    virtual_root=parts[0],
                    source_path_parts=parts,
                    relative_path=relative,
                    real_path=self.build_real_path(relative, source_root),
                    name=name,
                    stem=stem,
                    ext=ext,
                    depth=int(raw.get("depth") or 0),
                    parent_path="/".join(parts[:-1]) if len(parts) > 1 else "",
                    is_file=True,
                    resource_hint=hint,
                    size=raw.get("size"),
                    mtime=raw.get("modified"),
                    content_fingerprint="",  # 网盘元数据扫描不计算内容指纹
                )
            )

        file_count = len(files)
        video_count = sum(1 for f in files if f.resource_hint == "video")
        return RawSnapshot(
            snapshot_id=snapshot_id,
            source="openlist",
            provider_id=provider_id,
            ingest_method=ingest_method,
            source_route_id=source_route_id,
            source_root=source_root,
            root_container=root_container,
            created_at=now,
            input_file=input_path,
            file_count=file_count,
            video_count=video_count,
            files=files,
        )

    def build_real_path(self, relative_path: str, source_root: str) -> str:
        """只拼接本地挂载路径：source_root + 相对路径各层。"""
        result = Path(source_root)
        for part in PurePosixPath(relative_path).parts:
            result = result / part
        return str(result)


def _relative_to_locator(remote_path: str, remote_locator: str) -> str | None:
    """计算远端路径相对选中目录的相对路径；不在其下时返回 None。"""
    locator = remote_locator.rstrip("/")
    if locator and locator != "/":
        prefix = locator + "/"
        if not remote_path.startswith(prefix):
            return None
        remainder = remote_path[len(prefix):]
    else:
        remainder = remote_path.lstrip("/")
    if not remainder:
        return None
    if ".." in PurePosixPath(remainder).parts:
        return None
    return remainder
