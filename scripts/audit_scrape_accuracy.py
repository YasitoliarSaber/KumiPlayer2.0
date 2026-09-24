"""刮削准确率审计：逐部作品核对「本地事实 vs 在线身份」。

用户目标（2026-09-24）：进度的主指标是「正确刮削、没有错误的作品」，所以需要一个
可以反复运行、逐条可核对的审计工具，而不是只看「镜像生成了多少」。

用法（在项目根）：

    # 只读数据库，不访问网络：按已存快照分类
    uv run --project backend python scripts/audit_scrape_accuracy.py --offline

    # 用真实 TMDB 重新跑一遍生产识别链路（默认只读、不写库）
    uv run --project backend python scripts/audit_scrape_accuracy.py --limit 20

    # 指定来源 / 修订
    uv run --project backend python scripts/audit_scrape_accuracy.py \
        --root-id root_xxx --revision-id rev-yyy --output .context/audit.md

安全边界：
- 数据库以 `file:...?mode=ro` 只读打开；本脚本不写任何表、不改任何文件（除 --output）。
- 不打印、不落盘 TMDB Token；只报告 provider_id / 标题 / 年份等公开事实。
- 在线调用串行并复用同一个客户端，避免把 TMDB 打出 429。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

DEFAULT_DB = ROOT / "data" / "kumiplayer.db"

# 判定用：这些 reason_code 表示「作品资料已就绪，但集数/图片有缺项」。
PARTIAL_CODES = {
    "episode_mapping_incomplete",
    "special_episode_metadata_incomplete",
    "artifact_incomplete",
}


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w\u3400-\u9fff]+", "", text)


def _tokens(value: object) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return {token for token in re.split(r"[^a-z0-9\u3400-\u9fff]+", text) if len(token) > 1}


def open_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_works(conn: sqlite3.Connection, root_id: str, revision_id: str) -> list[dict]:
    """取出该修订下、该来源的全部作品 + 本地证据 + 已存刮削快照。"""

    rows = conn.execute(
        """
        SELECT DISTINCT w.work_id, w.preferred_title, w.original_title, w.year,
               w.work_type, w.show_type, w.card_type
        FROM work_source_bindings b
        JOIN works w ON w.work_id = b.work_id
        WHERE b.root_id = ? AND w.status = 'active'
        ORDER BY w.preferred_title
        """,
        (root_id,),
    ).fetchall()
    works: list[dict] = []
    for row in rows:
        work_id = str(row["work_id"])
        episodes = [
            dict(item)
            for item in conn.execute(
                """
                SELECT e.episode_id, e.local_episode_number, e.episode_kind,
                       e.special_number, e.display_title,
                       s.local_season_number, s.season_kind,
                       epm.provider_episode_number
                FROM episodes e
                JOIN seasons s ON s.season_id = e.season_id
                LEFT JOIN episode_provider_mappings epm
                  ON epm.episode_id = e.episode_id AND epm.provider = 'tmdb'
                WHERE e.work_id = ?
                ORDER BY s.local_season_number, e.local_episode_number
                """,
                (work_id,),
            ).fetchall()
        ]
        paths = [
            str(item["relative_path"])
            for item in conn.execute(
                """
                SELECT se.relative_path
                FROM revision_bindings rb
                JOIN source_evidence se ON se.evidence_id = rb.evidence_id
                WHERE rb.revision_id = ? AND rb.work_id = ?
                ORDER BY se.relative_path
                """,
                (revision_id, work_id),
            ).fetchall()
        ]
        binding = conn.execute(
            """
            SELECT status, metadata_json FROM scrape_bindings
            WHERE revision_id = ? AND work_id = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (revision_id, work_id),
        ).fetchone()
        stored: dict = {}
        if binding is not None and binding["metadata_json"]:
            try:
                decoded = json.loads(binding["metadata_json"])
                stored = decoded if isinstance(decoded, dict) else {}
            except (TypeError, ValueError):
                stored = {}
        works.append(
            {
                "work_id": work_id,
                "preferred_title": str(row["preferred_title"] or ""),
                "original_title": str(row["original_title"] or ""),
                "year": row["year"],
                "work_type": str(row["work_type"] or ""),
                "show_type": str(row["show_type"] or ""),
                "card_type": str(row["card_type"] or ""),
                "episodes": episodes,
                "paths": paths,
                "binding_status": str(binding["status"] or "") if binding else "",
                "stored": stored,
            }
        )
    return works


def build_target(work: dict, conn: sqlite3.Connection) -> dict:
    """按 `jobs/scrape.py:process()` 的方式重建刮削目标（只读）。"""

    work_id = work["work_id"]
    target: dict = {
        "work_id": work_id,
        "preferred_title": work["preferred_title"],
        "original_title": work["original_title"],
        "year": work["year"],
        "work_type": work["work_type"],
        "show_type": work["show_type"],
        "episodes": work["episodes"],
    }
    target["provider_bindings"] = [
        dict(row)
        for row in conn.execute(
            "SELECT provider, media_type, provider_id FROM provider_bindings WHERE work_id = ?",
            (work_id,),
        ).fetchall()
    ]
    # 本地身份标题：与 scrape.py 同源（works 标题 + 该作品已确认成员的解析标题）。
    titles: list[str] = []
    for raw in (work["preferred_title"], work["original_title"]):
        if str(raw or "").strip():
            titles.append(str(raw).strip())
    for row in conn.execute(
        """
        SELECT DISTINCT pf.work_title, pf.original_title, pf.title_candidates_json
        FROM revision_bindings rb
        JOIN parsed_facts pf ON pf.evidence_id = rb.evidence_id
        WHERE rb.revision_id = ? AND rb.work_id = ?
        """,
        (REVISION_ID, work_id),
    ).fetchall():
        for raw in (row["work_title"], row["original_title"]):
            if str(raw or "").strip():
                titles.append(str(raw).strip())
        try:
            for item in json.loads(row["title_candidates_json"] or "[]"):
                if str(item or "").strip():
                    titles.append(str(item).strip())
        except (TypeError, ValueError):
            continue
    deduped: list[str] = []
    seen: set[str] = set()
    for value in titles:
        key = _normalize(value)
        if key and key not in seen:
            seen.add(key)
            deduped.append(value)
    target["identity_titles"] = deduped[:8]
    return target


def local_name_hints(work: dict) -> list[str]:
    """从本地目录/文件名提取可用于人工核对的标题线索。"""

    hints: list[str] = []
    for path in work["paths"][:6]:
        parts = [part for part in path.replace("\\", "/").split("/") if part]
        if parts:
            hints.append(parts[-1])
            if len(parts) >= 2:
                hints.append(parts[-2])
    return list(dict.fromkeys(hint for hint in hints if hint))


def classify(work: dict, result: dict | None) -> tuple[str, str]:
    """返回 (判定, 说明)。判定：ok / partial / manual / unavailable。"""

    stored = work["stored"]
    state = (result or stored).get("metadata_state") or ""
    code = (result or stored).get("reason_code") or ""
    if state == "ready" and code not in PARTIAL_CODES:
        return "ok", ""
    if state == "ready" or code in PARTIAL_CODES:
        return "partial", code or "作品资料已就绪，部分集数未映射"
    if state == "waiting_review":
        return "manual", code or "需要人工确认在线作品"
    if state in {"source_unavailable", "failed", "waiting_metadata"}:
        return "unavailable", code or state
    return "manual", code or state or "未知状态"


def verdict_evidence(work: dict, result: dict | None) -> list[str]:
    """对「自动采用」的结果做可核对的证据检查，返回可疑点列表。"""

    if not result or result.get("metadata_state") != "ready":
        return []
    online_title = str(result.get("title") or "")
    online_original = str(result.get("original_title") or "")
    online_norm = {_normalize(online_title), _normalize(online_original)} - {""}
    online_tokens = _tokens(online_title) | _tokens(online_original)

    local_norms = {
        _normalize(work["preferred_title"]),
        _normalize(work["original_title"]),
        *(_normalize(title) for title in (result.get("queries") or [])),
    } - {""}
    if online_norm & local_norms:
        return []

    suspicious: list[str] = []
    # 本地文件名（压制组命名通常是罗马音/英文）与在线标题/原名的词集合有交集才算可解释。
    for hint in local_name_hints(work):
        tokens = _tokens(hint)
        if tokens and (tokens & online_tokens):
            return []
    suspicious.append(
        f"在线标题 {online_title!r} / 原名 {online_original!r} 与本地标题、文件名均无共同词"
    )
    local_year = work["year"]
    online_year = result.get("year")
    if local_year and online_year and abs(int(local_year) - int(online_year)) >= 2 and not work.get("season_scoped"):
        suspicious.append(f"年份差 {abs(int(local_year) - int(online_year))} 年（本地 {local_year}、在线 {online_year}）")
    return suspicious


def main() -> int:
    parser = argparse.ArgumentParser(description="刮削准确率审计（只读）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--root-id", default=os.environ.get("KUMI_AUDIT_ROOT", ""))
    parser.add_argument("--revision-id", default=os.environ.get("KUMI_AUDIT_REVISION", ""))
    parser.add_argument("--limit", type=int, default=0, help="只审计前 N 部（0=全部）")
    parser.add_argument("--only-pending", action="store_true", help="只看待处理作品")
    parser.add_argument("--offline", action="store_true", help="不访问网络，只用已存快照分类")
    parser.add_argument("--output", type=Path, default=None, help="把逐条结果写入该文件（UTF-8）")
    args = parser.parse_args()

    conn = open_readonly(args.db)
    root_id = args.root_id or _default_root(conn)
    revision_id = args.revision_id or _default_revision(conn, root_id)
    global REVISION_ID
    REVISION_ID = revision_id
    if not root_id or not revision_id:
        print("未能确定来源或修订；请显式传 --root-id / --revision-id", file=sys.stderr)
        return 2

    works = load_works(conn, root_id, revision_id)
    pending_only = args.only_pending
    if pending_only:
        works = [work for work in works if classify(work, None)[0] != "ok"]
    if args.limit:
        works = works[: args.limit]

    provider = None
    if not args.offline:
        from app.media_v4.jobs.metadata import default_metadata_provider

        provider = default_metadata_provider

    lines: list[str] = []
    counts: dict[str, int] = {"ok": 0, "partial": 0, "manual": 0, "unavailable": 0}
    suspicious_items: list[str] = []
    lines.append(f"# 刮削准确率审计 — {datetime.now(UTC).isoformat(timespec='seconds')}")
    lines.append("")
    lines.append(f"- 来源：`{root_id}`，修订：`{revision_id}`")
    lines.append(f"- 模式：{'离线（只读已存快照）' if args.offline else '在线（真实 TMDB 重新识别）'}")
    lines.append(f"- 作品数：{len(works)}")
    lines.append("")
    lines.append("| 判定 | 作品 | 类型 | 文件 | 采用身份 | 说明 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")

    def flush_report() -> None:
        """每完成一件就落盘：长审计中途被打断时，已完成的部分仍然可用。"""

        if not args.output:
            return
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")

    flush_report()

    for index, work in enumerate(works, start=1):
        result = None
        if provider is not None:
            target = build_target(work, conn)
            try:
                result = provider(target)
            except Exception as exc:  # noqa: BLE001 - 审计工具必须逐条继续
                result = {"metadata_state": "failed", "reason_code": f"exception:{type(exc).__name__}"}
        verdict, note = classify(work, result)
        counts[verdict] = counts.get(verdict, 0) + 1
        if result and result.get("metadata_state") == "ready":
            identity = f"{result.get('title') or ''}"
            if result.get("provider_id"):
                identity += f" (tmdb:{result['provider_id']}, {result.get('year') or '—'})"
        else:
            identity = ""
        for item in verdict_evidence(work, result):
            suspicious_items.append(f"{work['preferred_title']} — {item}")
        print(
            f"[{index}/{len(works)}] {verdict:11} {work['preferred_title'][:28]:28} "
            f"{identity[:60]} {note[:40]}",
            flush=True,
        )
        lines.append(
            f"| {verdict} | {work['preferred_title']} | {work['work_type']} | {len(work['paths'])} | "
            f"{identity} | {note} |"
        )
        flush_report()

    lines.append("")
    lines.append("## 汇总")
    lines.append("")
    for verdict, label in (
        ("ok", "正确刮削（无错误）"),
        ("partial", "已刮削但有缺项"),
        ("manual", "需要人工确认"),
        ("unavailable", "在线资料不可用/失败"),
    ):
        lines.append(f"- {label}：{counts.get(verdict, 0)}")
    if suspicious_items:
        lines.append("")
        lines.append("## 可疑采用（需要人工核对）")
        lines.append("")
        for item in suspicious_items:
            lines.append(f"- {item}")

    report = "\n".join(lines) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"\n报告已写入 {args.output}")
    print(
        "\n汇总：正确 {ok} / 有缺项 {partial} / 需人工 {manual} / 不可用 {unavailable}".format(**counts)
    )
    return 0


REVISION_ID = ""


def _default_root(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """
        SELECT b.root_id, COUNT(DISTINCT b.work_id) AS works
        FROM work_source_bindings b
        JOIN works w ON w.work_id = b.work_id AND w.status = 'active'
        JOIN source_roots r ON r.root_id = b.root_id
        WHERE r.retired_at = ''
        GROUP BY b.root_id ORDER BY works DESC LIMIT 1
        """
    ).fetchone()
    return str(row["root_id"]) if row else ""


def _default_revision(conn: sqlite3.Connection, root_id: str) -> str:
    row = conn.execute(
        """
        SELECT revision_id FROM import_revisions
        WHERE root_id = ? AND status = 'confirmed'
        ORDER BY confirmed_at DESC LIMIT 1
        """,
        (root_id,),
    ).fetchone()
    return str(row["revision_id"]) if row else ""


if __name__ == "__main__":
    raise SystemExit(main())
