"""O11：schema 版本号必须只有一个事实来源，且与 PRAGMA 字面量一致。

`PRAGMA user_version` 的值位置不接受绑定参数，只能在 `database.py` 里写字面量。
这个"常量 + 字面量"的双份结构已经咬过两次：
- 只改常量忘改字面量 → 每次启动先写错版本再抛 RuntimeError，应用根本起不来；
- 字面量落后一个版本 → 落库版本低于实际结构，下次启动被判"旧后端数据架构需要重置"，
  用户被要求清空媒体库。

因此这里把"单一来源"和"字面量一致"都变成测试，而不是依赖注释里的承诺。
"""

from __future__ import annotations

import re
from pathlib import Path


def _app_root() -> Path:
    return Path(__file__).resolve().parents[1] / "app"


def test_user_version_literal_matches_the_schema_constant():
    from app.media_v4.persistence.schema_v4 import V4_SCHEMA_VERSION

    source = (_app_root() / "media_v4" / "persistence" / "database.py").read_text(encoding="utf-8")

    assert f"PRAGMA user_version = {V4_SCHEMA_VERSION}" in source, (
        f"database.py 的 PRAGMA 字面量必须与 V4_SCHEMA_VERSION({V4_SCHEMA_VERSION}) 一致"
    )
    # 只允许一处写入字面量：多出来的那一份迟早与常量漂移。
    assert source.count("PRAGMA user_version = ") == 1


def test_schema_version_is_defined_in_exactly_one_module():
    definitions: list[str] = []
    for path in _app_root().rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.match(r"\s*V4_SCHEMA_VERSION\s*=", line):
                definitions.append(path.relative_to(_app_root()).as_posix())

    assert definitions == ["media_v4/persistence/schema_v4.py"], (
        f"版本号只能定义在 schema_v4.py，实际：{definitions}"
    )


def test_media_domain_package_exposes_no_stale_version_constant():
    import app.media_v4 as media_domain

    assert not hasattr(media_domain, "V4_SCHEMA_VERSION"), (
        "app.media_v4 不应再暴露版本号副本（历史上曾是陈旧的 4）"
    )


def test_deleted_scan_state_helpers_stay_deleted():
    """README/注释曾经承诺"统一阶段语义"，但那些常量无人引用；删掉后不要被重新加回。"""

    from app.media_v4.sources import scan_state

    for name in ("ACTIVE_SCAN_STATUSES", "TERMINAL_SCAN_STATUSES", "STAGE_LABELS", "stage_order"):
        assert not hasattr(scan_state, name), f"{name} 已确认无引用，不应重新引入"
