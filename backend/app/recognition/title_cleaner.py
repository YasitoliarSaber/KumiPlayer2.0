"""作品名清洗规则

从作品容器目录名中提取干净的 work_title。
只处理已确认的清洗问题，保守清洗，不强行猜。
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

# 状态词
_STATUS_WORDS = ["（将更新）", "(将更新)", "（更新中）", "(更新中)"]

#: 裸四位数字当年份的合理性判据（仅用于"空格+数字"这类无明确年份标记的写法）。
#:
#: 真实片名常以孤立数字结尾——``Blade Runner 2049``、``2012``、``银翼杀手 2049``——
#: 把这类数字当年份会同时改错三处：标题被删成 ``Blade Runner``、年份记成 2049、
#: 而年份会进入身份键，使刮削评分把正确候选判为"年份差 ≥2"直接阻断，作品永远
#: 停在人工确认。允许到"当前年 + 2"，以容纳次年新番预告。
_MIN_YEAR = 1900
_MAX_YEAR = 2099
_FUTURE_YEAR_TOLERANCE = 2


def is_plausible_year(value: int, *, current_year: int | None = None) -> bool:
    """裸四位数字是否有资格当年份。

    ``.2005`` / ``(2005)`` 这类带明确年份标记的写法不适用此判据（标记本身就说明
    它是年份），只有"空格 + 四位数字"这种裸数字才需要合理性上界。
    """

    year = int(value)
    if not _MIN_YEAR <= year <= _MAX_YEAR:
        return False
    reference = datetime.now().year if current_year is None else int(current_year)
    return year <= reference + _FUTURE_YEAR_TOLERANCE


# 系列容器结构词正则：.S1-S2、.S1~S3 等范围结构
# 只处理明确的 S\d+-S\d+ 范围，不处理单独 .S1 避免误伤
_RE_SERIES_RANGE = re.compile(r"\.[Ss]\d+\s*[-~]\s*[Ss]\d+.*$", re.DOTALL)

# 明确的目录组织标签，不是作品标题。只处理带 "+" 的合集结构，避免把
# 正式作品副标题中的 TV/外传等词误删。
_RE_SERIES_CATALOG_SUFFIX = re.compile(
    r"\.(?:TV(?:版)?|动画版)(?:\s*\+\s*(?:外传|剧场版|电影|OVA|OAD|SP|特别篇))*$",
    re.IGNORECASE,
)
_RE_RELEASE_LANGUAGE_SUFFIX = re.compile(r"\s+(?:内封中字|内封简繁|简繁内封|中字内封)$")

# 目录树导出或 Windows 复制同名目录时常在发布标签后追加 ``(1)``。
# 只有紧跟方括号发布/画质标签时才删除，避免误伤作品名本身的“第 1 部”。
_RE_FILESYSTEM_COPY_SUFFIX = re.compile(r"(?<=\])\s*[（(]\d+[）)]$")

# 电影目录常把画质/介质信息直接写在年份后面，例如
# ``作品 (2021) -1080p-Blu-ray``。这里只删除明确位于末尾的发布尾巴；
# 标题中间的数字、4K 等内容保持不动，避免把作品本名误当技术信息。
_RE_TRAILING_RELEASE_QUALITY = re.compile(
    r"(?:\s*[-–—._]\s*|\s+)"
    r"(?:\d{3,4}p|[248]k)"
    r"(?:\s*[-–—._]\s*(?:blu[- ]?ray|bd(?:rip|remux)?|uhd|web(?:rip|-?dl)?|remux|hdr(?:10\+)?))*"
    r"\s*$",
    re.IGNORECASE,
)

# 方括号 token 正则
_RE_BRACKET_TOKEN = re.compile(r"\[([^\]]*)\]")

# 部分目录树会把分区字母塞进作品名，例如：
# B 86-不存在的战区 -> 86-不存在的战区
# 仅在单字母后面接数字标题时清理，避免误伤 A Channel / K-ON! 等真实作品名。
_RE_SINGLE_LETTER_NUMERIC_PREFIX = re.compile(r"^[A-Za-z]\s+(?=\d)")

# 目录树导出常把画质写在作品名**前面**，例如 ``4k偶像大师 灰姑娘女孩 U149``。
# 只剥离已知的画质/介质词，且要求后面跟着分隔符或中日韩文字，避免误伤
# 86、22/7、3月的狮子、91Days 这类真标题。
_RE_LEADING_QUALITY_PREFIX = re.compile(
    r"^(?:"
    r"(?:\d{3,4}p|[248]k|uhd|hdr(?:10\+)?)\s*(?=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])"
    r"|(?:[248]k|uhd|blu[- ]?ray|bd(?:rip|remux)?|web(?:rip|-?dl)?|remux|hdr(?:10\+)?|\d{3,4}p)"
    r"[\s._\-]+"
    # 特典/花絮类前缀（``OVA 小魔女学园`` → ``小魔女学园``）。只在后面确实跟着
    # 分隔符时剥离，且不收录 sp/pv/cm/op/ed 这类易与真标题冲突的短词。
    r"|(?:ova|oad|特别篇|特典|映像特典|ncop|nced|menu)"
    r"[\s._\-]+"
    r")",
    re.IGNORECASE,
)


def strip_leading_quality_prefix(title: str) -> str:
    """剥离位于标题最前面的画质/介质前缀；结果为空时保留原值。"""

    value = str(title or "")
    if not value:
        return value
    cleaned = _RE_LEADING_QUALITY_PREFIX.sub("", value, count=1).strip()
    return cleaned or value


#: 目录序号前缀：``1.命运石之门`` / ``16.物语系列`` 这类排序编号属于发布目录的组织
#: 信息，不属于作品名（留着会让在线候选的规范化等值匹配失败，整部作品无法自动采用）。
#: **必须格外小心真标题本身带数字**：``2.5次元的诱惑``、``86-不存在的战区-``、
#: ``3月的狮子``、``22／7``、``91Days``；因此规则收紧为四重保护：
#:   1. 只认 1–2 位数字（排序编号的真实形态，≤99）；
#:   2. 必须紧跟 ``.`` / ``．`` / ``、`` 这类**序号分隔符**（连字符、斜杠、空白都不算）；
#:   3. 分隔符之后**不能**是数字，否则 ``2.5次元`` 会被截断成 ``5次元``；
#:   4. 剥离后必须仍有 ≥2 个字符且含字母或中日韩文字。
_ORDERING_PREFIX_PATTERN = re.compile(r"^\s*(?:\d{1,2})\s*[.．、]\s*(?P<rest>.*)$", re.DOTALL)
_RE_HAS_WORD_CHAR = re.compile(r"[A-Za-z\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")

#: 前导装饰 token 的最大剥离轮数：``C 4k Clannad``（京阿尼合集）、``O 4k 偶像大师…``
#: （用户选中的根目录名）都把画质标记放在**第二个** token，只剥一次第一个 token 会留下
#: 中间的 ``4k``，并一路进入展示与刮削命名。
_MAX_LEADING_STRIP_ROUNDS = 3


def strip_ordering_prefix(title: str) -> str:
    """剥离目录序号前缀（``1.命运石之门`` → ``命运石之门``），带四重反例保护。"""

    value = str(title or "")
    match = _ORDERING_PREFIX_PATTERN.match(value)
    if not match:
        return value
    rest = match.group("rest").strip()
    if not rest or rest[0].isdigit():
        return value
    if len(rest) < 2 or not _RE_HAS_WORD_CHAR.search(rest):
        return value
    return rest


def strip_leading_decoration_run(title: str) -> str:
    """反复剥离前导装饰 token（画质/介质 + 单字母标记），最多 3 轮。

    复用两条已经过验证的规则（画质前缀、单字母标记），只是**循环执行**，因此不会
    引入新的误伤面；任一轮没有变化就停止，永不返回空串。
    """

    value = str(title or "").strip()
    if not value:
        return value
    for _ in range(_MAX_LEADING_STRIP_ROUNDS):
        candidate = strip_leading_quality_prefix(value)
        if candidate == value:
            stripped_single = _RE_SINGLE_LETTER_NUMERIC_PREFIX.sub("", value, count=1).strip()
            candidate = stripped_single or value
        if candidate == value:
            break
        value = candidate
    return value

# 技术标签关键词（用于过滤方括号 token）
_TECH_KEYWORDS = {
    "bdrip", "webrip", "web-dl", "bdmv", "remux",
    "hevc", "hevc-10bit", "h264", "h265", "x264", "x265", "avc",
    "10bit", "8bit", "hi10p", "ma10p",
    "1080p", "2160p", "720p", "480p", "4k",
    "chs", "cht", "chsjp", "chttp", "chi_jpn", "scjp", "tcjp", "jpn", "eng",
    "mp4", "mkv", "flac", "aac", "ac3", "truehd", "dts", "e-ac3",
    "fin", "movie", "v2", "v3",
}

# 技术标签正则模式（匹配类似 Ma10p_1080p、01-12、01-47+MOVIE 等）
_RE_TECH_PATTERN = re.compile(
    r"^(?:"
    r"Ma\d+p.*"                    # Ma10p_1080p 等
    r"|\d+[-~]\d+.*"               # 01-12、13-24 等集数范围
    r"|\d+bit"                     # 10bit、8bit
    r"|\d+p"                       # 1080p、2160p
    r"|[A-Z]{2,3}_\d+[pi]"        # WEB_1080p 等
    r"|[A-Z]+-[A-Z]+-\d+"         # HEVC-10bit 等
    r")$",
    re.IGNORECASE,
)

# 字幕组关键词（用于识别字幕组 token）
_FANSUB_KEYWORDS = {
    "sub", "raw", "raws", "studio", "vcb", "vcb-studio",
    "lolihouse", "sakurato", "sweetsub", "dmg", "haruhana",
    "nekomoe", "kissaten", "nekomoe kissaten", "beansub", "fzsd", "cz", "mai",
    "t.h.x", "lp-raws", "airota", "ktxp", "thx",
    "sweet", "loli", "as", "nw", "ea",
}


@dataclass
class TitleCleanResult:
    """清洗结果"""

    title: str = ""
    changed: bool = False
    confidence_delta: str = ""  # 清洗后置信度变化
    needs_review: bool = False
    warnings: list[str] = field(default_factory=list)
    applied_rules: list[str] = field(default_factory=list)


def _is_tech_token(token: str) -> bool:
    """判断 token 是否为技术标签"""
    lower = token.lower().strip()
    if lower in _TECH_KEYWORDS:
        return True
    if _RE_TECH_PATTERN.match(token.strip()):
        return True
    return False


def _is_fansub_token(token: str) -> bool:
    """判断 token 是否为字幕组标签。

    字幕组名称按完整 token 精确匹配，或由 &、+ 连接的逐组件精确匹配，
    不再对 as、ea、nw、mai、sweet 等短关键词执行任意子串包含，
    避免误伤 Mai-HiME、Sweet Home 等真实标题。

    只按 & 和 + 拆分（字幕组连接符），不按 -、空格、_、. 拆分：
    一方面保留 'vcb-studio'、'lp-raws'、't.h.x' 这类带内部标点的真实组名，
    另一方面避免把 'Mai-HiME'、'Sweet Home' 这类含分隔符的真实标题拆成短词后误命中。
    """
    lower = token.lower().strip()
    if not lower:
        return False
    # 整体精确匹配（如 sweetsub、lolihouse、vcb-studio）
    if lower in _FANSUB_KEYWORDS:
        return True
    # 按 &、+ 拆分后逐组件精确匹配（如 BeanSub&FZSD、T.H.X&VCB-Studio）
    has_joiner = "&" in lower or "+" in lower
    if not has_joiner:
        return False
    parts: list[str] = []
    for sep in ("&", "+"):
        if not parts:
            parts = lower.split(sep)
        else:
            parts = [p for part in parts for p in part.split(sep)]
    for part in parts:
        cleaned = part.strip()
        if cleaned and cleaned in _FANSUB_KEYWORDS:
            return True
    return False


def _select_best_token(tokens: list[str]) -> tuple[str, bool]:
    """从方括号 token 中选择最像作品名的

    返回:
        (选中的 token, 是否需要 review)
    """
    if not tokens:
        return "", True

    if len(tokens) == 1:
        return tokens[0], False

    # 过滤掉技术标签和字幕组标签
    candidates = []
    for t in tokens:
        if _is_tech_token(t):
            continue
        if _is_fansub_token(t):
            continue
        candidates.append(t)

    if not candidates:
        # 全被过滤了，选最长的
        return max(tokens, key=len), True

    if len(candidates) == 1:
        return candidates[0], False

    # 多个候选，选最长的
    return max(candidates, key=len), True


def clean_work_title_container(container: str) -> TitleCleanResult:
    """清洗作品容器目录名，提取干净的 work_title

    清洗顺序：
    1. 去掉状态词
    2. 去掉系列容器结构词（.S1-S2 等范围）
    3. 处理字幕组方括号标签
    4. 空白归一化

    参数:
        container: 作品容器目录名，如 "CLANNAD.S1-S2+SP+OVA"

    返回:
        TitleCleanResult
    """
    if not container:
        return TitleCleanResult(title="", changed=False, needs_review=True, warnings=["容器名为空"])

    original = container
    result = TitleCleanResult(title=container)
    applied = []

    # 0. 先剥离前置画质前缀（``4k偶像大师…``）——它属于发布信息，不属于作品名，
    #    留着会让在线候选的规范化等值匹配失败，作品永远无法自动采用。
    #    用**循环**版本：实测 ``C 4k Clannad``、``O 4k 偶像大师…``（用户选中的根
    #    目录名）把画质标记放在第二个 token，只剥一次会留下中间的 ``4k`` 并进入展示。
    original_before_leading = container
    prefixed = strip_leading_decoration_run(container)
    if prefixed != container:
        container = prefixed
        applied.append("去掉前置画质/装饰前缀")
        # 循环剥离可能已经带走了单字母分区前缀（``B 86-…`` / ``C 4k …``）：
        # 补记该规则，让 applied_rules 仍然如实说明"单字母前缀被去掉了"。
        if _RE_SINGLE_LETTER_NUMERIC_PREFIX.match(original_before_leading):
            applied.append("去掉单字母分区前缀")

    # 0.1 目录序号前缀（``1.命运石之门`` → ``命运石之门``）：同样会让在线匹配失败。
    unnumbered = strip_ordering_prefix(container)
    if unnumbered != container:
        container = unnumbered
        applied.append("去掉目录序号前缀")

    # 1. 去掉状态词
    cleaned = container
    for word in _STATUS_WORDS:
        if word in cleaned:
            cleaned = cleaned.replace(word, "")
            applied.append(f"去掉状态词: {word}")

    # 1.2 去掉 TMDB ID 提示，ID 本身由识别层作为结构化字段保留。
    from app.scrape.tmdb_hint import strip_tmdb_hint

    tmdb_cleaned = strip_tmdb_hint(cleaned)
    if tmdb_cleaned != cleaned:
        cleaned = " ".join(tmdb_cleaned.split())
        applied.append("去掉 TMDB ID 提示")

    # 1.5 去掉目录树分区字母前缀
    prefixed = _RE_SINGLE_LETTER_NUMERIC_PREFIX.sub("", cleaned).strip()
    if prefixed != cleaned:
        cleaned = prefixed
        applied.append("去掉单字母分区前缀")

    # 2. 去掉系列容器结构词（.S1-S2 等范围）
    m = _RE_SERIES_RANGE.search(cleaned)
    if m:
        cleaned = _RE_SERIES_RANGE.sub("", cleaned)
        applied.append(f"去掉系列容器结构词: {m.group()}")

    # 2.1 去掉明确的合集目录标签与发布语言尾注。
    catalog_cleaned = _RE_SERIES_CATALOG_SUFFIX.sub("", cleaned).strip()
    if catalog_cleaned != cleaned:
        cleaned = catalog_cleaned
        applied.append("去掉系列合集目录标签")

    release_cleaned = _RE_RELEASE_LANGUAGE_SUFFIX.sub("", cleaned).strip()
    if release_cleaned != cleaned:
        cleaned = release_cleaned
        applied.append("去掉发布语言尾注")

    copy_suffix_cleaned = _RE_FILESYSTEM_COPY_SUFFIX.sub("", cleaned).strip()
    if copy_suffix_cleaned != cleaned:
        cleaned = copy_suffix_cleaned
        applied.append("去掉同名目录复制序号")

    # 3. 处理字幕组方括号标签
    brackets = _RE_BRACKET_TOKEN.findall(cleaned)
    if brackets:
        # 去掉所有方括号 token
        no_brackets = _RE_BRACKET_TOKEN.sub("", cleaned).strip()
        # 空白归一化
        no_brackets = " ".join(no_brackets.split())

        if no_brackets:
            # 有剩余文本，使用剩余文本
            cleaned = no_brackets
            applied.append("去掉方括号标签，保留剩余文本")
        else:
            # 去掉方括号后为空，从 token 中选择
            selected, needs_review = _select_best_token(brackets)
            if selected:
                cleaned = selected
                applied.append(f"从方括号 token 中选择: {selected}")
                if needs_review:
                    result.needs_review = True
                    result.warnings.append("从多个方括号 token 中选择，可能不准确")
            else:
                # 无法选择，保留原始值
                result.needs_review = True
                result.warnings.append("无法从方括号 token 中提取作品名")
                applied.append("无法提取，保留原始值")

    # 3.5 去掉年份之后的明确画质/介质尾巴，使年份重新成为末尾结构事实。
    release_quality_cleaned = _RE_TRAILING_RELEASE_QUALITY.sub("", cleaned).strip()
    if release_quality_cleaned != cleaned:
        cleaned = release_quality_cleaned
        applied.append("去掉末尾画质与介质信息")

    # 4. 去掉末尾年份（.2005、(2005)、（2005）等）
    #    "空格 + 四位数字"必须先确认像年份：真实片名常以孤立数字结尾
    #    （Blade Runner 2049），删掉会同时改错标题与身份键。
    _year_patterns = (
        (re.compile(r"[.．](\d{4})$"), False),
        (re.compile(r"[(\（](\d{4})[)\）]$"), False),
        (re.compile(r"\s(\d{4})$"), True),
    )
    for pat, needs_plausible in _year_patterns:
        matched = pat.search(cleaned)
        if matched is None:
            continue
        if needs_plausible and not is_plausible_year(int(matched.group(1))):
            continue
        new_cleaned = pat.sub("", cleaned).strip()
        if new_cleaned != cleaned:
            cleaned = new_cleaned
            applied.append("去掉末尾年份")
            break

    # 5. 空白归一化
    cleaned = " ".join(cleaned.split()).strip()

    # 更新结果
    result.title = cleaned
    result.changed = cleaned != original
    result.applied_rules = applied

    # 如果清洗后为空，保留原始值
    if not cleaned:
        result.title = original
        result.changed = False
        result.needs_review = True
        result.warnings.append("清洗后为空，保留原始值")

    return result
