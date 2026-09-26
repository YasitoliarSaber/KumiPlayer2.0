"""C-003 身份合同的唯一实现：季/集 identity key、规范展示名与跨来源复用谓词。

- ``season_identity_key`` / ``episode_identity_key`` / ``episode_row_key`` 被
  Resolver、候选合图、确认落库与重扫比较共用；键一律是 UTF-8 紧凑 JSON 数组，
  不以冒号或竖线拆标题。
- ``select_canonical_work`` / ``select_canonical_value`` 是集合级展示名与类别的
  唯一选择实现：先完成兼容分组，再按 ``(证据等级降序, 独立 SourceFile 支持数降序,
  非全大写拉丁拼写优先, NFC 码点序)`` 排序；多个候选不合并、不取最早 UUID，
  也不由遍历顺序先到先得。
- ``episode_row_key`` 是 ``ResolvedEpisode.episode_key`` 的唯一生成器：本地编号
  优先、其次绝对编号、特殊篇按编号、完全未定位的条目以 SourceFile 槽位为稳定锚点。
- ``strong_same_work`` / ``boundary_compatible`` 是跨 root 复用 Work 的唯一充分性
  判据：同 Provider/type/ID + 完整标题（或同一份已核验别名）覆盖两边，或同一受信
  内容哈希的唯一视频对应关系。
- ``semantic_signature_members`` / ``graph_semantic_digest`` 给出**不含观察 ID**、
  成员按稳定语义 key 排序的语义签名，供重扫比较使用；它与保护快照的 graph_digest
  （可含本次 evidence）分开，不能混用。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from app.media_v4.domain.identity import (
    CONTENT_CLASS_MOVIE,
    MEDIA_TYPE_MOVIE,
    MEDIA_TYPE_UNKNOWN,
    SEASON_KIND_REGULAR,
    SEASON_KIND_UNASSIGNED,
    canonical_json,
    digest_of,
    stable_sorted_keys,
)
from app.media_v4.domain.models import (
    ResolvedEpisode,
    ResolvedMediaGraph,
    ResolvedWork,
)

MEDIA_TYPE_TV = "tv"

#: 特殊篇的兼容季号：旧库用 0 表示 S00，新正片不得再生成 0 季（C-002）。
SPECIAL_SEASON_NUMBER = 0


def season_identity_key(
    *, local_season_number: int | None, season_kind: str, work_key: str
) -> str:
    """同 Work 下唯一：明确季号为 ``['local', n]``；未分季为 ``['unassigned', scope]``。

    scope 取该 Work 的本地边界键：同一部作品的未分季文件属于同一个"未分季组"，
    不会因目录换名而重新分季（C-003 的 scope_key 稳定锚点语义）。
    """

    if season_kind == SEASON_KIND_UNASSIGNED or local_season_number is None:
        return canonical_json(["unassigned", work_key])
    if season_kind == SEASON_KIND_REGULAR:
        return canonical_json(["local", int(local_season_number)])
    return canonical_json([season_kind, int(local_season_number)])


def episode_identity_key(
    *,
    season_key: str,
    local_episode_number: int | None,
    absolute_episode_number: int | None,
    fallback_key: str,
) -> str:
    """统一集身份：local 优先，其次绝对编号，最后未定位槽位锚点。

    调用方必须与确认落库使用同一组输入（ResolvedEpisode 的本地/绝对编号与该行的
    ``episode_key``），否则图与库会算出不同的键。
    """

    if local_episode_number is not None:
        return canonical_json(["local", season_key, int(local_episode_number)])
    if absolute_episode_number is not None:
        return canonical_json(["absolute", season_key, int(absolute_episode_number)])
    return canonical_json(["unresolved", fallback_key])


def episode_row_key(
    *,
    work_key: str,
    local_season_number: int | None,
    local_episode_number: int | None,
    absolute_episode_number: int | None,
    special_number: int | None,
    source_file_key: str = "",
) -> str:
    """``ResolvedEpisode.episode_key`` 的唯一生成器（同一次 revision 内唯一）。

    本地/绝对/特殊篇三种定位保留既有 ``|`` 形态（含作品与季，保证
    ``episode_ids[episode_key]`` 这类按行键索引的调用不会跨作品串号）；
    完全没有编号的条目返回 SourceFile 槽位，使两个未知文件天然是两个槽位
    （C-003/F-005）。绝对编号只在没有本地编号时参与键，本地编号始终为主。
    """

    if (
        local_episode_number is None
        and absolute_episode_number is None
        and special_number is None
    ):
        return source_file_key
    return "|".join(
        (
            work_key,
            str(local_season_number),
            str(local_episode_number),
            str(absolute_episode_number if local_episode_number is None else None),
            str(special_number),
        )
    )


@dataclass(frozen=True, slots=True)
class WorkFacts:
    """跨来源复用所需的本地事实子集（不含任何观察 ID）。"""

    title: str = ""
    aliases: tuple[str, ...] = ()
    media_type: str = MEDIA_TYPE_UNKNOWN
    year: int | None = None
    card_type: str = ""
    relation_type: str = ""
    provider: str = ""
    provider_id: str = ""
    content_hash: str = ""
    content_hash_evidence_count: int = 0


def _normalized(value: str) -> str:
    from app.media_v4.resolution.title_norm import normalize_identity_title

    return normalize_identity_title(value or "")


def boundary_compatible(left: WorkFacts, right: WorkFacts) -> bool:
    """边界兼容：类型一致、年份不冲突、不吸收独立续作/外传/电影。"""

    if left.media_type != right.media_type and MEDIA_TYPE_UNKNOWN not in {
        left.media_type,
        right.media_type,
    }:
        return False
    if left.year is not None and right.year is not None and left.year != right.year:
        return False
    left_bucket = _boundary_bucket(left)
    right_bucket = _boundary_bucket(right)
    if left_bucket and right_bucket and left_bucket != right_bucket:
        return False
    if (left.card_type == "standalone") != (right.card_type == "standalone"):
        return False
    return True


def _boundary_bucket(facts: WorkFacts) -> str:
    """主系列 / 外传 / 电影三类边界；未知返回空串（不阻断）。"""

    if facts.media_type == MEDIA_TYPE_MOVIE or facts.relation_type == "movie":
        return "movie"
    if facts.card_type == "standalone" or facts.relation_type in {"spin_off", "related"}:
        return "standalone"
    if facts.relation_type == "main":
        return "main"
    return ""


def strong_same_work(left: WorkFacts, right: WorkFacts) -> bool:
    """C-003 的充分条件；任一满足且 boundary_compatible 才可跨来源复用。"""

    if not boundary_compatible(left, right):
        return False
    if (
        left.content_hash
        and left.content_hash == right.content_hash
        and left.content_hash_evidence_count == 1
        and right.content_hash_evidence_count == 1
    ):
        return True
    left_titles = {_normalized(left.title), *(_normalized(item) for item in left.aliases)}
    right_titles = {_normalized(right.title), *(_normalized(item) for item in right.aliases)}
    left_titles.discard("")
    right_titles.discard("")
    titles_equal = bool(left_titles & right_titles)
    same_provider_identity = bool(
        left.provider
        and left.provider == right.provider
        and left.provider_id
        and left.provider_id == right.provider_id
    )
    return bool(titles_equal and same_provider_identity)


@dataclass(frozen=True, slots=True)
class TitleCandidate:
    """一个待选的展示名及其证据强度（C-004 的排序输入）。"""

    title: str
    #: 证据等级：越大越强。调用方按来源（文件级/目录级/结构规则）与"是否为该组声明的身份标题"给出。
    evidence_rank: int = 0
    #: 独立 SourceFile 支持数：越大越强。
    support: int = 0


#: 纯拉丁全大写拼写（`SHOW`）排在正常拼写（`Show`）之后。
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")


def _is_all_caps_latin(title: str) -> bool:
    text = (title or "").strip()
    if not text or not _LATIN_LETTER_RE.search(text):
        return False
    return text.upper() == text and text.lower() != text


def select_canonical_work(
    candidates: Sequence[TitleCandidate | str],
) -> tuple[str | None, bool]:
    """确定性地选展示名；多个候选不合并、不取最早 UUID、不看遍历顺序。

    排序键严格按 C-004：``(证据等级降序, 独立 SourceFile 支持数降序, 非全大写拉丁
    拼写优先, NFC 字符串码点序)``。返回 ``(规范展示名, 是否存在互斥标题)``：
    第二个值为真表示该组同时声明了规范化后**不同**的标题（例如同一系列下各季的
    标题写法不同），只作诊断；同一身份的不同拼写（``SHOW``/``Show``）不算冲突。
    """

    items: list[TitleCandidate] = []
    for candidate in candidates:
        if isinstance(candidate, str):
            items.append(TitleCandidate(title=candidate))
        else:
            items.append(candidate)
    items = [item for item in items if (item.title or "").strip()]
    if not items:
        return None, False
    normalized = {_normalized(item.title) for item in items}
    normalized.discard("")
    ambiguous = len(normalized) > 1
    best = min(
        items,
        key=lambda item: (
            -int(item.evidence_rank),
            -int(item.support),
            1 if _is_all_caps_latin(item.title) else 0,
            unicodedata.normalize("NFC", item.title.strip()),
        ),
    )
    return unicodedata.normalize("NFC", best.title.strip()), ambiguous


def select_canonical_value(values: Sequence[str]) -> tuple[str, bool]:
    """类别/类型等标量字段的唯一值选择：唯一明确值胜出，多值冲突返回未知。

    空值表示"未声明"，不参与冲突判定。同一组出现两个互不相同且都明确的类别时
    返回 ``("", True)``，由调用方按 C-004 处理为 unknown，不做先到先得赋值。
    """

    explicit = sorted({value.strip() for value in values if (value or "").strip()})
    if not explicit:
        return "", False
    if len(explicit) == 1:
        return explicit[0], False
    return "", True


def episode_semantic_key(episode: ResolvedEpisode) -> str:
    """单个逻辑集的语义 key：不含 evidence_id/scan_id，只含身份与版别。"""

    return canonical_json(
        [
            "episode",
            episode.work_key,
            episode.season_identity_key,
            episode.identity_key,
            episode.season_kind,
            episode.episode_kind,
            episode.edition_key,
            episode.special_number,
            len(episode.asset_evidence_ids),
        ]
    )


def work_semantic_key(work: ResolvedWork) -> str:
    """单个 Work 的语义 key：不含 evidence_id/scan_id，只含身份与类别。"""

    return canonical_json(
        [
            "work",
            work.work_key,
            work.preferred_title,
            work.year,
            work.media_type,
            work.card_type,
            work.show_type,
            work.series_group,
            work.relation_type,
            len(work.source_evidence_ids),
        ]
    )


def semantic_signature_members(graph: ResolvedMediaGraph) -> tuple[str, ...]:
    """集合级语义签名的成员：成员按稳定语义 key 排序，不含观察 ID。

    与保护快照的 ``graph_digest`` 分开：后者可含本次 evidence，本函数用于
    重扫比较与"加入排除条目不改变正片语义图"的判定。
    """

    members = [work_semantic_key(work) for work in graph.works]
    members.extend(episode_semantic_key(episode) for episode in graph.episodes)
    members.extend(
        canonical_json(["work_asset", asset.work_key, asset.edition_key, len(asset.asset_evidence_ids)])
        for asset in graph.work_assets
    )
    members.extend(
        canonical_json(
            ["relation", relation.parent_work_key, relation.child_work_key, relation.relation_type]
        )
        for relation in graph.relations
    )
    return stable_sorted_keys(members)


def graph_semantic_digest(graph: ResolvedMediaGraph) -> str:
    """去除 observation ID 的语义签名摘要，供重扫/集合置换比较使用。"""

    return digest_of(list(semantic_signature_members(graph)))


def is_movie_like(media_type: str, content_class: str = "") -> bool:
    return media_type == MEDIA_TYPE_MOVIE or content_class == CONTENT_CLASS_MOVIE


def is_tv_type(media_type: str) -> bool:
    return media_type == MEDIA_TYPE_TV
