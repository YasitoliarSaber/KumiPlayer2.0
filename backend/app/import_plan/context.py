"""Import Revision 识别上下文解析。

通过 revision → media_unit → source_root → sources 恢复人工确认与刮削
所需的上下文事实（root_container 等派生值不落库，按需稳定重建）。
"""

from __future__ import annotations

from pathlib import PurePosixPath

from app.db.database import get_connection


def load_revision_context(revision_id: str) -> dict:
    """恢复 revision 的识别上下文。

    返回字段：revision_id, unit_id, root_id, remote_locator, root_container,
    import_family, import_scope, source, provider_id。

    - root_container = PurePosixPath(remote_locator.rstrip('/')).name；
    - 根路径 '/' 或空 remote_locator 时 root_container 安全为空字符串；
    - import_revisions 无 root_container 列，仅通过 source_roots 稳定重建。
    """
    conn = get_connection()
    revision_row = conn.execute(
        "SELECT * FROM import_revisions WHERE revision_id = ?", (revision_id,)
    ).fetchone()
    if revision_row is None:
        raise ValueError(f"revision 不存在: {revision_id}")
    revision = dict(revision_row)

    unit_row = conn.execute(
        "SELECT * FROM media_units WHERE unit_id = ?", (revision["unit_id"],)
    ).fetchone()
    unit = dict(unit_row) if unit_row else {}

    root = {}
    if unit:
        root_row = conn.execute(
            "SELECT * FROM source_roots WHERE root_id = ?", (unit.get("root_id") or "",)
        ).fetchone()
        root = dict(root_row) if root_row else {}

    source_type = ""
    if root:
        source_row = conn.execute(
            "SELECT * FROM sources WHERE source_id = ?", (root.get("source_id") or "",)
        ).fetchone()
        if source_row is not None:
            source_type = str(source_row["source_type"] or "")

    remote_locator = (root.get("remote_locator") or "").rstrip("/")
    # 根路径 '/' 安全空值处理：rstrip 后为空 → 不进入 PurePosixPath
    if remote_locator:
        root_container = PurePosixPath(remote_locator).name
    else:
        root_container = ""

    return {
        "revision_id": revision["revision_id"],
        "unit_id": revision["unit_id"],
        "root_id": unit.get("root_id") or "",
        "remote_locator": remote_locator,
        "root_container": root_container,
        "import_family": root.get("import_family") or "",
        "import_scope": root.get("import_scope") or "",
        "source": str(revision.get("source") or "") or source_type,
        "provider_id": str(revision.get("provider_id") or ""),
    }
