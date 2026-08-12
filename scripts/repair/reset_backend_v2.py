"""后端数据流 V2 一次性重置命令（默认预览）。

用法：
    python scripts/repair/reset_backend_v2.py            # 只预览
    python scripts/repair/reset_backend_v2.py --apply    # 预览后确认执行

安全：
- 只删除 KumiPlayer 受管数据（data 下快照/计划/镜像/刮削/播放/数据库等）与受管镜像目录；
- 任何目标是来源根、磁盘根或用户主目录时立即中止；
- 真实媒体、本地来源目录、网盘文件与 OpenList 远端内容绝不被删除。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.maintenance.reset_backend_state import (  # noqa: E402
    ResetProtectionError,
    apply_reset,
    preview_reset,
)


def main() -> int:
    apply = "--apply" in sys.argv
    preview = preview_reset()
    print("=== KumiPlayer 管理数据重置（后端数据流 V2）===")
    print(f"数据根：{preview['data_dir']}")
    print(f"镜像根：{preview['mirror_root']}")
    print("将删除（KumiPlayer 受管数据，可重建）：")
    for target in preview["targets"]:
        print(f"  - {target}")
    if not preview["targets"]:
        print("  （无）")
    print("受保护来源根（绝不删除）：")
    for root in preview["source_roots_protected"]:
        print(f"  - {root}")
    if not preview["source_roots_protected"]:
        print("  （未配置）")

    if not apply:
        print("\n预览模式：未删除任何数据。确认后加 --apply 执行。")
        return 0

    answer = input("\n确认执行重置？输入 DELETE 继续：").strip()
    if answer != "DELETE":
        print("已取消，未删除任何数据。")
        return 1

    try:
        result = apply_reset()
    except ResetProtectionError as exc:
        print(f"已中止（路径保护）：{exc}")
        return 2
    print("\n已删除：")
    for path in result["removed"]:
        print(f"  - {path}")
    print("重置完成。下一次启动将创建空的后端 V2 数据库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
