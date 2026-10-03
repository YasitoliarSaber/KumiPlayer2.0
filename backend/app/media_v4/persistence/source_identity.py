"""SourceFile / 观察关联的事务仓储（C-001）。

只负责把 ``decide_source_identity`` 的纯判定落成三张表的事实：

- ``source_files`` 保存来源命名空间内的文件槽位；
- ``source_file_observations`` 保存每一代观察与槽位、Asset 的关联，一经插入不可 UPDATE；
- 读取上一代 baseline 供连续性判定使用。

槽位、Asset 与观察关联必须在同一个事务里准备好，不允许先写 NULL 再改写。
真实路径、内容哈希与探测一律不做。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from app.media_v4.domain.identity import (
    IDENTITY_KIND_CONTENT_HASH,
    IDENTITY_KIND_LOCATOR,
    IDENTITY_KIND_PROVIDER_ID,
    STRENGTH_CONTENT_HASH,
    STRENGTH_PROVIDER_ID,
    SourceIdentityDecision,
)
from app.media_v4.domain.models import SourceEvidence
from app.media_v4.persistence.database import V4Database
from app.media_v4.sources.file_identity import (
    ObservedFile,
    derived_source_file_id,
    namespace_key,
    namespace_kind,
)

#: 只有本机物理磁盘的 stat 才被视为内容代次证据；挂载盘/TXT/远端 API 仅作提示。
_RELIABLE_MTIME_INGESTS = frozenset({"local_scan"})

#: 槽位的定位键种类：强度不等于身份种类，需要显式映射。
_STRENGTH_TO_IDENTITY_KIND = {
    STRENGTH_CONTENT_HASH: IDENTITY_KIND_CONTENT_HASH,
    STRENGTH_PROVIDER_ID: IDENTITY_KIND_PROVIDER_ID,
}


def _identity_kind_for_strength(strength: str) -> str:
    return _STRENGTH_TO_IDENTITY_KIND.get(strength, IDENTITY_KIND_LOCATOR)


@dataclass(frozen=True, slots=True)
class SourceFileRow:
    source_file_id: str
    root_id: str
    origin_evidence_id: str
    identity_kind: str
    identity_namespace: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ConfirmedSlotBinding:
    """上一代 confirmed 成员的槽位绑定（C-003 连续性复用的输入）。"""

    source_file_id: str
    evidence_id: str
    work_id: str
    season_id: str | None
    episode_id: str | None
    edition_id: str | None
    asset_id: str | None


@dataclass(frozen=True, slots=True)
class IdentityContext:
    """一次确认可用的上一代 baseline 与既有槽位。"""

    root_id: str
    namespace: str
    identity_kind_name: str
    previous_observations: tuple[ObservedFile, ...]
    source_files: dict[str, SourceFileRow]
    #: 每个槽位最近一次观察；Asset 连续性直接取自这里的 ``asset_id``。
    latest_by_slot: dict[str, ObservedFile] = field(default_factory=dict)
    # 混合基线的 TXT 是本地定位，API 是远端定位；同根相对槽位才是共同坐标。
    relative_slots: bool = False

    def observation_for_evidence(self, evidence_id: str) -> ObservedFile | None:
        for item in self.previous_observations:
            if item.evidence_id == evidence_id:
                return item
        return None

    def observation_for_slot(self, source_file_id: str) -> ObservedFile | None:
        if not source_file_id:
            return None
        observation = self.latest_by_slot.get(source_file_id)
        if observation is not None:
            return observation
        for item in self.previous_observations:
            if item.source_file_id == source_file_id:
                return item
        return None


def observed_from_evidence(
    evidence: SourceEvidence, *, namespace: str, namespace_kind_name: str, relative_slots: bool = False
) -> ObservedFile:
    """把不可变观察映射成连续性输入；不触盘、不算内容哈希。

    ``content_hash`` 只在适配器给出可信内容哈希时才有值。本地扫描的
    ``fingerprint`` 是 ``stat`` 事实（size+mtime），不是内容哈希，因此不参与
    "字节相同"的判断，只按 size 比较（C-001 分支 1）。
    """

    return ObservedFile(
        evidence_id=evidence.evidence_id,
        source_key=evidence.source_key,
        locator=evidence.relative_path if relative_slots else (evidence.source_locator or evidence.playback_locator or evidence.source_key),
        identity_namespace=namespace,
        namespace_kind_name=namespace_kind_name,
        size=evidence.size,
        mtime=evidence.mtime,
        raw_file_id=evidence.raw_file_id,
        identity_kind=IDENTITY_KIND_PROVIDER_ID if evidence.raw_file_id else IDENTITY_KIND_LOCATOR,
        mtime_reliable=str(evidence.ingest_method or "") in _RELIABLE_MTIME_INGESTS,
    )


def content_version_key() -> str:
    """首次内容代次使用独立UUID；hash/stat只参与连续性判定，不能成为物理主键。"""
    return str(uuid.uuid4())


class SourceIdentityRepository:
    def __init__(self, database: V4Database) -> None:
        self.database = database

    @staticmethod
    def _observed_from_row(row, *, identity_kind_name: str, relative_slots: bool = False) -> ObservedFile:
        return ObservedFile(
            evidence_id=str(row["evidence_id"]),
            source_key=str(row["source_key"] or ""),
            locator=str(row["relative_path"] if relative_slots else (row["source_locator"] or row["playback_locator"] or row["source_key"] or "")),
            identity_namespace=str(row["identity_namespace"] or ""),
            namespace_kind_name=identity_kind_name,
            source_file_id=str(row["source_file_id"] or ""),
            asset_id=str(row["asset_id"]) if row["asset_id"] else None,
            size=row["size"],
            mtime=row["mtime"],
            raw_file_id=str(row["raw_file_id"] or ""),
            identity_kind=str(row["identity_kind"] or IDENTITY_KIND_LOCATOR),
            mtime_reliable=str(row["ingest_method"] or "") in _RELIABLE_MTIME_INGESTS,
        )

    def load_identity_context(
        self,
        root_id: str,
        *,
        route_id: str = "",
        provider: str = "",
        namespace: str | None = None,
        conn=None,
    ) -> IdentityContext:
        """读取该来源命名空间的上一代 current/baseline 观察与既有槽位。"""

        target_namespace = namespace or namespace_key(root_id=root_id, route_id=route_id)
        identity_kind_name = namespace_kind(provider)
        source_files: dict[str, SourceFileRow] = {}
        observations: list[ObservedFile] = []
        owns_connection = conn is None
        connection = conn if conn is not None else self.database.open_connection()
        try:
            root = connection.execute("SELECT source_mode FROM source_roots WHERE root_id=?", (root_id,)).fetchone()
            relative_slots = bool(root and root["source_mode"] == "tree_openlist")
            for row in connection.execute(
                "SELECT * FROM source_files WHERE root_id = ? ORDER BY created_at, source_file_id",
                (root_id,),
            ).fetchall():
                source_files[str(row["source_file_id"])] = SourceFileRow(
                    source_file_id=str(row["source_file_id"]),
                    root_id=str(row["root_id"]),
                    origin_evidence_id=str(row["origin_evidence_id"]),
                    identity_kind=str(row["identity_kind"]),
                    identity_namespace=str(row["identity_namespace"]),
                    created_at=str(row["created_at"]),
                )
            rows = connection.execute(
                """
                SELECT o.evidence_id, o.source_file_id, o.asset_id,
                       f.identity_namespace, f.identity_kind,
                       e.source_key, e.source_locator, e.playback_locator, e.relative_path,
                       e.size, e.mtime, e.raw_file_id, e.ingest_method
                FROM source_file_observations o
                JOIN source_files f ON f.source_file_id = o.source_file_id
                JOIN source_evidence e ON e.evidence_id = o.evidence_id
                WHERE f.root_id = ?
                ORDER BY o.created_at, o.evidence_id
                """,
                (root_id,),
            ).fetchall()
            if rows:
                latest: dict[str, ObservedFile] = {}
                for row in rows:
                    observed = self._observed_from_row(row, identity_kind_name=identity_kind_name, relative_slots=relative_slots)
                    latest[observed.source_file_id or observed.evidence_id] = observed
                observations = list(latest.values())
            else:
                # 升级前的库只有 confirmed revision 证据：按显式 legacy 分支读取，
                # 不声称这些观察已验证，也不为它们伪造 SourceFile。
                observations = self._legacy_observations(
                    connection, root_id, identity_kind_name=identity_kind_name, relative_slots=relative_slots
                )
        finally:
            if owns_connection:
                connection.close()
        latest_by_slot = {
            item.source_file_id: item
            for item in observations
            if item.source_file_id
        }
        return IdentityContext(
            root_id=root_id,
            namespace=target_namespace,
            identity_kind_name=identity_kind_name,
            previous_observations=tuple(observations),
            source_files=source_files,
            latest_by_slot=latest_by_slot, relative_slots=relative_slots,
        )

    def load_confirmed_slot_bindings(
        self, root_id: str, *, conn
    ) -> tuple[ConfirmedSlotBinding, ...]:
        """读取该来源根当前 confirmed 成员的文件槽位绑定。

        只回答"这个槽位上一代属于哪个 Work/季/集/Asset"，不做语义判断；
        退役来源、superseded revision 与旧 legacy 空槽位都不会被带进来。
        """

        rows = conn.execute(
            """
            SELECT o.source_file_id, o.evidence_id, o.asset_id AS observation_asset_id,
                   rb.work_id, rb.season_id, rb.episode_id, rb.edition_id, rb.asset_id
            FROM source_file_observations o
            JOIN revision_bindings rb ON rb.evidence_id = o.evidence_id
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE sr.root_id = ? AND ir.status = 'confirmed' AND sr.retired_at = ''
            ORDER BY o.source_file_id, rb.binding_id
            """,
            (root_id,),
        ).fetchall()
        bindings: list[ConfirmedSlotBinding] = []
        seen: set[tuple[str, str, str, str | None, str | None, str | None, str | None]] = set()
        for row in rows:
            asset_id = row["asset_id"] or row["observation_asset_id"]
            item = ConfirmedSlotBinding(
                source_file_id=str(row["source_file_id"]),
                evidence_id=str(row["evidence_id"]),
                work_id=str(row["work_id"]),
                season_id=str(row["season_id"]) if row["season_id"] else None,
                episode_id=str(row["episode_id"]) if row["episode_id"] else None,
                edition_id=str(row["edition_id"]) if row["edition_id"] else None,
                asset_id=str(asset_id) if asset_id else None,
            )
            key = (
                item.source_file_id,
                item.work_id,
                item.evidence_id,
                item.season_id,
                item.episode_id,
                item.edition_id,
                item.asset_id,
            )
            if key in seen:
                continue
            seen.add(key)
            bindings.append(item)
        return tuple(bindings)

    @staticmethod
    def _legacy_observations(
        conn, root_id: str, *, identity_kind_name: str, relative_slots: bool = False
    ) -> list[ObservedFile]:
        rows = conn.execute(
            """
            SELECT se.evidence_id, se.source_key, se.source_locator, se.playback_locator, se.relative_path,
                   se.size, se.mtime, se.raw_file_id, se.ingest_method, rb.asset_id
            FROM import_revisions ir
            JOIN revision_evidence re ON re.revision_id = ir.revision_id
            JOIN source_evidence se ON se.evidence_id = re.evidence_id
            LEFT JOIN revision_bindings rb
              ON rb.revision_id = ir.revision_id AND rb.evidence_id = se.evidence_id
            WHERE ir.root_id = ? AND ir.status = 'confirmed'
            ORDER BY se.relative_path COLLATE NOCASE, se.evidence_id
            """,
            (root_id,),
        ).fetchall()
        return [
            ObservedFile(
                evidence_id=str(row["evidence_id"]),
                source_key=str(row["source_key"] or ""),
                locator=str(row["relative_path"] if relative_slots else (row["source_locator"] or row["playback_locator"] or row["source_key"] or "")),
                identity_namespace="",
                namespace_kind_name=identity_kind_name,
                source_file_id="",
                asset_id=str(row["asset_id"]) if row["asset_id"] else None,
                size=row["size"],
                mtime=row["mtime"],
                raw_file_id=str(row["raw_file_id"] or ""),
                identity_kind=IDENTITY_KIND_LOCATOR,
                mtime_reliable=str(row["ingest_method"] or "") in _RELIABLE_MTIME_INGESTS,
            )
            for row in rows
        ]

    def register_source_file(
        self,
        *,
        evidence: SourceEvidence,
        decision: SourceIdentityDecision,
        namespace: str,
        conn,
        created_at: str,
    ) -> str:
        """登记或复用槽位；同事务内幂等，重复预览不会产生第二行。"""

        existing_id = decision.source_file_id or ""
        if existing_id:
            row = conn.execute(
                "SELECT source_file_id FROM source_files WHERE source_file_id = ?",
                (existing_id,),
            ).fetchone()
            if row is not None:
                return str(row["source_file_id"])
        derived = existing_id or derived_source_file_id(evidence.evidence_id)
        conn.execute(
            """
            INSERT INTO source_files(
                source_file_id, root_id, origin_evidence_id, identity_kind,
                identity_namespace, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_file_id) DO NOTHING
            """,
            (
                derived,
                evidence.root_id,
                evidence.evidence_id,
                _identity_kind_for_strength(decision.strength),
                namespace,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT source_file_id FROM source_files WHERE source_file_id = ?",
            (derived,),
        ).fetchone()
        if row is None:
            # 同一出生观察已登记过别的槽位（跨 root 复用或并发确认）：以库里那行为准。
            existing = conn.execute(
                "SELECT source_file_id FROM source_files WHERE root_id = ? AND origin_evidence_id = ?",
                (evidence.root_id, evidence.evidence_id),
            ).fetchone()
            if existing is None:
                raise RuntimeError(f"SourceFile 登记失败: {evidence.evidence_id}")
            return str(existing["source_file_id"])
        return derived

    def register_observation(
        self,
        *,
        evidence: SourceEvidence,
        source_file_id: str,
        asset_id: str | None,
        decision: SourceIdentityDecision,
        conn,
        created_at: str,
    ) -> None:
        """写入观察关联；已存在时只核对绑定，不 UPDATE（表本身禁止）。

        C-001：一个证据最多映射一个 SourceFile 与一个 Asset，槽位/Asset 不得改写；
        但连续性是**每次确认重新决策**的派生值（基线与上一代无关地变化，例如首次
        为 ``new``、同一 scan 再次确认时为 ``unchanged``），因此重复登记时以先写入
        的决定为准，不因派生值变化而阻断合法重扫。
        """

        existing = conn.execute(
            "SELECT source_file_id, asset_id, decision_json FROM source_file_observations "
            "WHERE evidence_id = ?",
            (evidence.evidence_id,),
        ).fetchone()
        if existing is not None:
            same_binding = (
                str(existing["source_file_id"]) == source_file_id
                and (str(existing["asset_id"]) if existing["asset_id"] else None) == asset_id
            )
            if not same_binding:
                raise ValueError(f"不可变观察关联冲突: {evidence.evidence_id}")
            return
        conn.execute(
            """
            INSERT INTO source_file_observations(
                evidence_id, source_file_id, asset_id, decision_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (evidence.evidence_id, source_file_id, asset_id, decision.to_json(), created_at),
        )

    @staticmethod
    def decision_payload(decision: SourceIdentityDecision) -> str:
        return decision.to_json()

    @staticmethod
    def load_decision(decision_json: str) -> dict:
        try:
            return json.loads(decision_json or "{}")
        except (TypeError, ValueError):
            return {}
