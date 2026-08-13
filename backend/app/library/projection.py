"""LibraryIndex Projection 适配层：SQLite current state → 兼容 DTO。

- ScrapeMap projection：scrape_bindings → ScrapeMap（经 effective store 分流，
  V3 走 SQLite、legacy 走 JSON）；
- MirrorScanResult projection：artifact_records → MirrorScanResult
  （kind=strm → MirrorFile；nfo/poster/fanart/clearlogo → MirrorAsset）。

只做投影适配，不成为新的 truth。物化文件缺失（Path.exists() 为 False）时
不假装仍可播放/展示；远程 artwork URL 不进 artifact_records（保留在
scrape_bindings 的 path 字段）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.db.database import get_connection
from app.library.scanner import MirrorAsset, MirrorFile, MirrorScanResult, _NAMESPACE_MAP
from app.scrape.models import ScrapeMap


def load_scrape_map_projection(plan_id: str) -> ScrapeMap:
    """按 plan 代次加载 ScrapeMap 兼容投影（V3 → SQLite bindings，legacy → JSON）。"""
    from app.scrape.effective_store import load_effective_scrape_map

    return load_effective_scrape_map(plan_id)


def _namespace_for(source: str) -> str:
    return _NAMESPACE_MAP.get(source, source)


def build_scan_result_projection(plan: Any, revision_id: str) -> MirrorScanResult:
    """artifact_records（按 revision）→ MirrorScanResult 兼容 DTO。

    本地文件必须真实存在才进入投影：缺失的 STRM/NFO/图片不假装可用。
    """
    from app.import_plan import revision_store as _rs

    rows = get_connection().execute(
        "SELECT * FROM artifact_records WHERE revision_id = ?", (revision_id,)
    ).fetchall()
    strm_files: list[MirrorFile] = []
    assets: list[MirrorAsset] = []
    for row in rows:
        kind = str(row["kind"] or "")
        path_value = str(row["path"] or "")
        if not path_value or not Path(path_value).exists():
            continue
        if kind == "strm":
            try:
                real_path = Path(path_value).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                real_path = ""
            strm_files.append(
                MirrorFile(
                    source=plan.source,
                    namespace=_namespace_for(plan.source),
                    strm_path=path_value,
                    relative_strm_path=path_value,
                    real_path=real_path,
                    exists=True,
                )
            )
        elif kind in ("nfo", "poster", "fanart", "clearlogo"):
            assets.append(MirrorAsset(path=path_value, kind=kind, exists=True))
    return MirrorScanResult(
        source=plan.source,
        mirror_root="",
        scanned_at=_rs.now_iso(),
        strm_files=strm_files,
        assets=assets,
    )
