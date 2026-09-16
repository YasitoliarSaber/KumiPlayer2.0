"""共享候选评分与自动采用决策（D3：CandidateRanker）。

确认前候选规划、第三步 metadata job 与手动恢复搜索共用同一套标题清洗、
特征计算与排序，消除三套不同判断。评分可解释：每个候选返回 score 与
reasons；popularity 只作同分排序信号，绝不覆盖身份冲突。

自动采用门槛移植自基准 34b4f2f ``scrape/auto.py`` 的身份门禁：
- 类型必须匹配，movie 篇章相反必须阻断；
- 年份冲突（差 ≥2）硬阻断；差一且标题强匹配允许；
- 身份安全 = 完整规范化标题等值，或可信别名链完整等值；
- 通用短标题不构成身份证据；
- 采用还需分数达标，且相对第二名有足够分差（唯一候选除外）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from app.media_v4.resolution.title_norm import normalize_match_title

# 自动采用门槛（由人工确认语料回放校准：recall ≥ 95%、precision ≥ 98%）。
AUTO_ADOPT_MIN_SCORE = 55.0
AUTO_ADOPT_MIN_MARGIN = 10.0

# 篇章标识（作品身份约束，不是普通评分项）。
_PART_PATTERN = re.compile(r"(前篇|后篇|上篇|下篇|第一章|第二章|第[一二三四五六七八九十]+部)")

# 通用短标题：长度不足或属于容器词时不能作为身份证据。
_GENERIC_TITLE_PATTERN = re.compile(r"^(剧场版| movie|映画|特别篇|总集篇|oad|ova)$", re.IGNORECASE)
_GENERIC_MIN_LENGTH = 4

# 中文/日文直接等值比较前，去掉的装饰性包裹词。
# 常见译名等值辅助：日文假名不参与规范化折叠，等值依赖别名链证据。
_KANA_PATTERN = re.compile(r"[\u3040-\u30ff]")


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """带数值证据的排序后候选。"""

    provider: str
    provider_id: str
    media_type: str
    title: str
    original_title: str = ""
    aliases: tuple[str, ...] = ()
    year: int | None = None
    popularity: float = 0.0
    score: float = 0.0
    reasons: tuple[str, ...] = ()
    recommended: bool = False
    identity_safe: bool = False
    blocked: bool = False
    extra: dict = field(default_factory=dict)


def _normalize_title(value: str | None) -> str:
    """匹配语义：统一全角、忽略全部标点与空白，不截断正文。

    实现唯一在 `title_norm.normalize_match_title`——草稿候选评分与刮削排名必须同源，
    否则同一对标题会在两边得出相反结论。身份键用的是另一套更严格的语义，见该模块。
    """

    return normalize_match_title(value)


def _animation_state(candidate: dict) -> bool | None:
    """从 TMDB genre 事实判断动画域；缺少 genre 时返回未知。"""

    genre_ids: set[int] = set()
    genre_names: set[str] = set()
    for raw in candidate.get("genre_ids") or ():
        try:
            genre_ids.add(int(raw))
        except (TypeError, ValueError):
            continue
    for genre in candidate.get("genres") or ():
        raw_name = None
        if isinstance(genre, dict):
            raw_id = genre.get("id")
            raw_name = genre.get("name")
            try:
                if raw_id is not None:
                    genre_ids.add(int(raw_id))
            except (TypeError, ValueError):
                pass
        else:
            raw_name = genre
        if raw_name:
            genre_names.add(str(raw_name).strip().casefold())
    if not genre_ids and not genre_names:
        return None
    return 16 in genre_ids or bool(
        genre_names.intersection({"animation", "anime", "动画", "動畫", "アニメ"})
    )


def _domain_level(target: dict, candidate: dict) -> tuple[int, str]:
    """1=作品域匹配，0=未知，-1=动画/真人域明确冲突。"""

    show_type = str(target.get("show_type") or "").strip().casefold()
    if not show_type.startswith(("anime", "live")):
        return 0, ""
    is_animation = _animation_state(candidate)
    if is_animation is None:
        return 0, ""
    wants_animation = show_type.startswith("anime")
    if is_animation == wants_animation:
        return 1, "动画作品域匹配" if wants_animation else "真人作品域匹配"
    return -1, "作品域不符（真人候选）" if wants_animation else "作品域不符（动画候选）"


def _is_generic_title(value: str) -> bool:
    """通用短标题：空、太短（中日文 3 字以下或拉丁 4 字以下）或容器词。"""

    text = _normalize_title(value)
    if not text or _GENERIC_TITLE_PATTERN.match(text):
        return True
    has_cjk = bool(_KANA_PATTERN.search(text)) or any(
        "\u4e00" <= char <= "\u9fff" for char in text
    )
    minimum = 3 if has_cjk else 4
    return len(text) < minimum


def _title_identity_level(target_titles: list[str], candidate: dict) -> tuple[int, str]:
    """0=无证据，1=足够长前缀/高相似，2=完整规范化等值，3=可信别名链等值。"""

    provider_titles = [
        str(candidate.get("title") or ""),
        str(candidate.get("original_title") or ""),
        *(str(alias) for alias in (candidate.get("aliases") or ())),
    ]
    provider_norms = {_normalize_title(value) for value in provider_titles}
    provider_norms.discard("")
    target_norms = {_normalize_title(value) for value in target_titles}
    target_norms.discard("")

    if provider_norms & target_norms:
        # 完整等值：候选主标题/原名/别名 任一与查询或本地标题规范化等值。
        direct = any(_normalize_title(candidate.get("title")) == norm or
                     _normalize_title(candidate.get("original_title")) == norm
                     for norm in target_norms)
        if direct:
            return 2, "完整标题等值"
        return 3, "可信别名链等值"

    # 装饰符（如 △）已由 normalize 去除；剩余副标题是作品身份的一部分。
    # 前缀只能用于展示弱候选，不能和真正的完整标题同分。
    # 足够长前缀/高相似：仅当双方都足够长（≥6 字）且一方是另一方前缀时
    # 给弱证据（如「葬送的芙莉莲」与「葬送的芙莉莲 第一季」）。
    for target_norm in target_norms:
        if len(target_norm) < 6:
            continue
        for provider_norm in provider_norms:
            if len(provider_norm) < 6:
                continue
            if provider_norm.startswith(target_norm) or target_norm.startswith(provider_norm):
                return 1, "长前缀弱证据"
    return 0, ""


def _year_level(target_year: int | None, candidate_year: int | None) -> tuple[int, str]:
    """-1=明确冲突，0=无年份证据，1=差一，2=等值。"""

    if candidate_year is None or target_year is None:
        return 0, ""
    delta = abs(int(candidate_year) - int(target_year))
    if delta == 0:
        return 2, "年份等值"
    if delta == 1:
        return 1, "年份差一"
    return -1, f"年份冲突（目标 {target_year}，候选 {candidate_year}）"


def _part_direction(value: str) -> str:
    """提取篇章方向：前/上/第一部 → 'first'，后/下/第二部… → 'second'。"""

    text = _normalize_title(value)
    if re.search(r"前篇|上篇|第一章|第一部", text):
        return "first"
    if re.search(r"后篇|下篇|第二章|第二部", text):
        return "second"
    return ""


def _part_mismatch(target_titles: list[str], candidate: dict) -> bool:
    candidate_direction = _part_direction(str(candidate.get("title") or ""))
    if not candidate_direction:
        return False
    for value in target_titles:
        target_direction = _part_direction(value)
        if target_direction and target_direction != candidate_direction:
            return True
    return False


def score_candidate(
    target: dict,
    candidate: dict,
    *,
    part_mismatch: bool | None = None,
) -> RankedCandidate:
    """为单个候选计算可解释分数与理由。"""

    target_titles = [
        str(target.get("preferred_title") or ""),
        *(str(item) for item in (target.get("queries") or ())),
        *(str(item) for item in (target.get("aliases") or ())),
    ]
    target_titles = [value for value in target_titles if value.strip()]
    target_media_type = str(target.get("media_type") or "tv")

    reasons: list[str] = []
    score = 0.0
    identity_level, identity_reason = _title_identity_level(target_titles, candidate)

    if identity_level == 3:
        # 可信别名链来自 provider 元数据，证据强度与直接等值同级。
        score += 55.0
        reasons.append(identity_reason)
    elif identity_level == 2:
        score += 50.0
        reasons.append(identity_reason)
    elif identity_level == 1:
        score += 12.0
        reasons.append(identity_reason)

    year_level, year_reason = _year_level(target.get("year"), candidate.get("year"))
    if year_level == 2:
        score += 8.0
        reasons.append(year_reason)
    elif year_level == 1:
        score += 3.0
        reasons.append(year_reason)
    elif year_level == -1:
        score -= 25.0
        reasons.append(year_reason)

    candidate_media_type = str(candidate.get("media_type") or "")
    type_match = candidate_media_type == target_media_type
    if type_match:
        score += 5.0
    else:
        score -= 30.0
        reasons.append("类型不符")

    domain_level, domain_reason = _domain_level(target, candidate)
    if domain_level == 1:
        score += 10.0
        reasons.append(domain_reason)
    elif domain_level == -1:
        score -= 40.0
        reasons.append(domain_reason)

    if part_mismatch is None:
        part_mismatch = _part_mismatch(target_titles, candidate)
    if part_mismatch:
        score -= 40.0
        reasons.append("电影篇章相反")

    # 通用短标题只在身份证据薄弱时扣分；「虫师」这类与候选完整等值的
    # 短标题是精确身份，不因长度受罚（路人 vs 路人超能100 才是风险场景）。
    if target_titles and _is_generic_title(target_titles[0]) and identity_level < 2:
        score -= 20.0
        reasons.append("查询标题过短或过于通用")

    identity_safe = identity_level >= 2 and type_match and domain_level != -1
    # 硬阻断：年份硬冲突、篇章相反、类型不符——无论分数多高都不自动采用，
    # 也不进入推荐位（人工确认仍能看到完整理由）。
    blocked = (
        year_level == -1
        or bool(part_mismatch)
        or not type_match
        or domain_level == -1
    )
    return RankedCandidate(
        provider=str(candidate.get("provider") or ""),
        provider_id=str(candidate.get("provider_id") or ""),
        media_type=candidate_media_type,
        title=str(candidate.get("title") or ""),
        original_title=str(candidate.get("original_title") or ""),
        aliases=tuple(str(alias) for alias in (candidate.get("aliases") or ())),
        year=candidate.get("year"),
        popularity=float(candidate.get("popularity") or 0.0),
        score=round(score, 2),
        reasons=tuple(reasons),
        identity_safe=identity_safe,
        blocked=blocked,
        extra={
            "genre_ids": tuple(candidate.get("genre_ids") or ()),
            "genres": tuple(candidate.get("genres") or ()),
        },
    )


def rank_candidates(target: dict, candidates: list[dict]) -> list[RankedCandidate]:
    """排序候选：分数降序，popularity 只作同分 tiebreaker；标记推荐位。"""

    ranked = [score_candidate(target, candidate) for candidate in candidates]
    ranked.sort(key=lambda item: (item.score, item.popularity), reverse=True)
    recommended_index: int | None = None
    for index, item in enumerate(ranked):
        if item.blocked:
            continue
        if item.identity_safe:
            recommended_index = index
            break
    if recommended_index is None:
        for index, item in enumerate(ranked):
            if not item.blocked and item.score >= AUTO_ADOPT_MIN_SCORE:
                recommended_index = index
                break
    if recommended_index is not None:
        best = ranked[recommended_index]
        ranked[recommended_index] = replace(best, recommended=True)
    return ranked


class CandidateRanker:
    """面向调用方的排序/采用门禁入口；阈值可注入便于语料回放。"""

    def __init__(
        self,
        *,
        min_score: float = AUTO_ADOPT_MIN_SCORE,
        min_margin: float = AUTO_ADOPT_MIN_MARGIN,
    ):
        self.min_score = min_score
        self.min_margin = min_margin

    def rank(self, target: dict, candidates: list[dict]) -> list[RankedCandidate]:
        return rank_candidates(target, candidates)

    def auto_adopt(
        self,
        ranked: list[RankedCandidate],
        *,
        min_score: float | None = None,
        min_margin: float | None = None,
    ) -> tuple[RankedCandidate | None, str]:
        return auto_adopt(
            ranked,
            min_score=self.min_score if min_score is None else min_score,
            min_margin=self.min_margin if min_margin is None else min_margin,
        )


def auto_adopt(
    ranked: list[RankedCandidate],
    *,
    min_score: float = AUTO_ADOPT_MIN_SCORE,
    min_margin: float = AUTO_ADOPT_MIN_MARGIN,
) -> tuple[RankedCandidate | None, str]:
    """决定是否自动采用；返回 (候选, 原因)。达不到门槛一律交给人工。

    拒绝条件：无候选 / 类型不符 / 年份硬冲突 / 篇章相反 / 通用标题 /
    分数不足 / 分差不足（唯一候选除外）。
    """

    if not ranked:
        return None, "无候选"
    viable = [item for item in ranked if not item.blocked]
    if not viable:
        best = ranked[0]
        return None, best.reasons[0] if best.reasons else "候选被身份门禁阻断"
    best = viable[0]
    if not best.identity_safe:
        return None, "无完整标题或可信别名等值证据"
    if best.score < min_score:
        return None, f"最高分候选分数不足（{best.score} < {min_score}）"
    competitors = [
        item
        for item in viable[1:]
        if item.identity_safe and item.score >= min_score
    ]
    if competitors:
        runner_up = competitors[0]
        margin = best.score - runner_up.score
        if margin < min_margin:
            return None, f"与第二名分差不足（{margin:.1f} < {min_margin}）"
    return best, "最高分候选，自动采用"
