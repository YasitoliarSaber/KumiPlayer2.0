# -*- coding: utf-8 -*-
"""经官方资料或 TMDB 核实的少量结构例外与译名别名。

这里仅保存自动规则无法可靠推导、且已核验的事实。通用命名解析仍由
``media.py`` 负责，避免把规则库变成模糊标题猜测表。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class VerifiedSeriesSpecial:
    series_title: str
    tmdb_id: int
    special_number: int | None
    title: str
    reason: str


@dataclass(frozen=True)
class VerifiedTmdbBinding:
    markers: tuple[str, ...]
    tmdb_id: int
    tmdb_type: str
    canonical_title: str


def _series_special(
    series_title: str,
    tmdb_id: int,
    special_number: int | None,
    title: str,
    reason: str,
) -> VerifiedSeriesSpecial:
    return VerifiedSeriesSpecial(
        series_title=series_title,
        tmdb_id=tmdb_id,
        special_number=special_number,
        title=title,
        reason=reason,
    )


def match_verified_series_special(
    relative_path: str,
    filename: str,
) -> VerifiedSeriesSpecial | None:
    """匹配已核实为主系列 Special、但目录名容易被当成独立作品的条目。"""
    path_text = unicodedata.normalize("NFKC", relative_path or "").casefold()
    file_text = unicodedata.normalize("NFKC", filename or "")
    file_folded = file_text.casefold()

    if "air.2005" in path_text:
        if "总集篇" in file_text:
            return _series_special("AIR", 26201, 1, "总集篇", "已核实为《AIR》TV 第 0 季第 1 集总集篇")
        ova_match = re.search(r"\bOVA\s*0?([12])\b", file_text, re.IGNORECASE)
        if ova_match:
            ova_number = int(ova_match.group(1))
            return _series_special(
                "AIR",
                26201,
                ova_number + 1,
                "山路～mountain path～" if ova_number == 1 else "天地～universe～",
                "已核实为《AIR in Summer》两集特别篇",
            )

    if "clannad.s1-s2+sp+ova" in path_text:
        clannad_rules = (
            ("s01e23", 1, "暑假的故事"),
            ("智代篇", 2, "另一个世界 智代篇"),
            ("一年前的事", 3, "一年前的事"),
            ("苍绿", 4, "在那苍绿的树下"),
            ("杏篇", 5, "另一个世界 杏篇"),
        )
        for marker, special_number, title in clannad_rules:
            if marker.casefold() in path_text:
                return _series_special("CLANNAD", 24835, special_number, title, "已核实为 CLANNAD 第 0 季特别篇")

    if "伪恋.s1-s2+oad" in path_text:
        if "[s2]" in path_text and "oad" in file_folded:
            return _series_special("伪恋", 62640, 4, "OAD 4", "已核实为《伪恋》第 4 部 OAD")
        oad_match = re.search(r"\[OAD\s*0?(\d+)\]", file_text, re.IGNORECASE)
        if oad_match:
            number = int(oad_match.group(1))
            return _series_special("伪恋", 62640, number, f"OAD {number}", "已核实为《伪恋》OAD")

    if "中二病也要谈恋爱.s1-s2+剧场版" in path_text:
        sp_match = re.search(r"\[SP0?(\d+)\]", file_text, re.IGNORECASE)
        if "take on me" in path_text and sp_match:
            number = 29 + int(sp_match.group(1))
            return _series_special("中二病也要谈恋爱", 45501, number, f"Take On Me 特典 {sp_match.group(1)}", "已核实为 Take On Me 电影特典")
        if "/1.中二病" in path_text:
            if "[lite]" in file_folded:
                return _series_special("中二病也要谈恋爱", 45501, 35, "中二病也要谈恋爱！Lite", "已核实为第一季 Lite 合集")
            if sp_match:
                number = 6 + int(sp_match.group(1))
                return _series_special("中二病也要谈恋爱", 45501, number, f"DEPTH OF FIELD {sp_match.group(1)}", "已核实为第一季 BD 特典")
            if re.search(r"\[13\]", file_text):
                return _series_special("中二病也要谈恋爱", 45501, 14, "闪耀的…圣爆诞祭", "已核实为第一季番外篇")
        if "/3.中二病" in path_text:
            if "[lite]" in file_folded:
                return _series_special("中二病也要谈恋爱", 45501, 36, "中二病也要谈恋爱！恋 Lite", "已核实为第二季 Lite 合集")
            if sp_match:
                number = 21 + int(sp_match.group(1))
                return _series_special("中二病也要谈恋爱", 45501, number, f"恋 Lite 特典 {sp_match.group(1)}", "已核实为第二季 BD 特典")
            if re.search(r"\[13\]", file_text):
                return _series_special("中二病也要谈恋爱", 45501, 29, "再生的…邪王真眼默示录", "已核实为第二季番外篇")

    if "轻音少女.s1-s2+剧场版" in path_text:
        episode_match = re.search(r"\[(\d+)\]", file_text)
        if episode_match:
            episode = int(episode_match.group(1))
            if "轻音少女.[s1].2009" in path_text and episode in {13, 14}:
                special = {13: 1, 14: 9}[episode]
                return _series_special("轻音少女", 42253, special, f"第一季番外篇 {episode - 12}", "已核实为《轻音少女》第一季番外篇")
            if "轻音少女.[s2].2010" in path_text and episode in {25, 26, 27}:
                special = {25: 13, 26: 14, 27: 21}[episode]
                return _series_special("轻音少女", 42253, special, f"第二季番外篇 {episode - 24}", "已核实为《轻音少女》第二季番外篇")

    if "在下坂本,有何贵干?" in path_text and re.search(r"\[13\]", file_text):
        return _series_special("在下坂本，有何贵干？", 65944, 1, "坂本吗？", "已核实为第 13 集未播出特别篇")

    if "hyouka" in path_text and "11.5" in file_folded and "preview" not in file_folded:
        return _series_special("Hyouka", 65329, 1, "应有之物", "已核实为《冰菓》第 11.5 集 OAD")

    if "无职转生.s1-s2" in path_text:
        if "[s1]" in path_text and "ova" in file_folded:
            return _series_special("无职转生", 94664, 1, "艾莉丝的哥布林讨伐", "已核实为《无职转生》第一季 OVA")
        if "[s2]" in path_text and re.search(r"\[00\]", file_text):
            return _series_special("无职转生", 94664, 2, "守护术师菲兹", "已核实为《无职转生 II》第 0 话")

    if "刀剑神域.s1-s3+剧场版+外传" in path_text:
        if "/3.刀剑神域[s2].2014/" in path_text:
            if "14.5" in file_folded:
                return _series_special("刀剑神域", 45782, 12, "第 14.5 话 Debriefing", "已核实为《刀剑神域 II》第 14.5 话")
            sp_match = re.search(r"\[SP0?(\d+)\]", file_text, re.IGNORECASE)
            if sp_match:
                number = 12 + int(sp_match.group(1))
                return _series_special("刀剑神域", 45782, number, f"Sword Art Offline II {sp_match.group(1)}", "已核实为《刀剑神域 II》BD 特典")
        if "/5.刀剑神域:alicization篇" in path_text and "18.5" in file_folded:
            return _series_special("刀剑神域", 45782, 23, "第 18.5 话 Recollection", "已核实为 Alicization 总集篇")
        if "/6.刀剑神域:alicization篇 war of underworld" in path_text:
            if "12.5" in file_folded:
                return _series_special("刀剑神域", 45782, 25, "第 12.5 话 回忆", "已核实为 War of Underworld 总集篇")
            if re.search(r"异界战争\s+00(?:\D|$)", file_text):
                return _series_special("刀剑神域", 45782, 24, "第 0 话 Reflection", "已核实为 War of Underworld 第 0 话")

    if "路人女主的养成方法.s1-s2+剧场版" in path_text and re.search(r"\[00\]", file_text):
        number = 2 if "/2.路人女主" in path_text else 1
        return _series_special("路人女主的养成方法", 69367, number, f"第 {number} 季番外篇", "已核实为《路人女主的养成方法》番外篇")

    if "某科学的超电磁炮.s1-s3" in path_text and "ova" in file_folded:
        if "超电磁炮s.[s2].2013" in path_text:
            return _series_special("某科学的超电磁炮", 30977, 7, "重要的事都能在澡堂学到", "已核实为《某科学的超电磁炮 S》OVA")
        if "超电磁炮.[s1].2009" in path_text:
            return _series_special("某科学的超电磁炮", 30977, 4, "御坂学姐现在是焦点人物", "已核实为《某科学的超电磁炮》OVA")

    if "命运石之门.s1-s2+剧场版+ova" in path_text:
        if "命运石之门0.[s2].2018" in path_text and "ova" in file_folded:
            return _series_special("命运石之门0", 78102, 1, "结晶多形的情人节", "已核实为《命运石之门 0》OVA")
        if "聪明睿智的认知计算" in path_text:
            part_match = re.search(r"\(([1-3])\)", file_text)
            number = int(part_match.group(1)) + 1 if part_match else 5
            return _series_special("命运石之门", 42509, number, "聪明睿智的认知计算", "已核实为《命运石之门》认知计算短篇")
        if "命运石之门.[s1].2011" in path_text:
            if "23β" in file_text:
                return _series_special("命运石之门", 42509, 6, "境界面上的缺失之环（β线）", "已核实为第 23β 话")
            if "[sp]" in file_folded:
                return _series_special("命运石之门", 42509, 1, "横行跋扈的浪荡之徒", "已核实为《命运石之门》OVA")

    if (
        "辉夜大小姐想让我告白.s1-s4+剧场版" in path_text
        and "/2.辉夜大小姐" in path_text
        and "ova" in file_folded
    ):
        return _series_special("辉夜大小姐想让我告白", 83121, 2, "第 2 季 OVA", "已核实为《辉夜大小姐想让我告白》第二季 OVA")

    if "灵能百分百.s1-s3+ova" in path_text:
        if "reigen.[ova]" in path_text:
            return _series_special("灵能百分百", 67075, 7, "REIGEN～不为人知的奇迹灵能者～", "已核实为《灵能百分百》REIGEN 特别篇")
        if "灵能百分百 ii.2019" in path_text and "ova" in file_folded:
            return _series_special("灵能百分百", 67075, 8, "第一次灵能咨询所员工旅游", "已核实为《灵能百分百 II》OVA")

    if "re:从零开始的异世界生活.s1-s3" in path_text and "/5.re:从零开始的异世界生活.[s3].2024/sps/" in path_text:
        sp_match = re.search(r"\[SP(\d+)\]", file_text, re.IGNORECASE)
        if sp_match:
            number = 50 + int(sp_match.group(1))
            return _series_special("Re：从零开始的异世界生活", 65942, number, f"Re:从零开始的休息时间 3rd {sp_match.group(1)}", "已核实为第三季休息时间短篇")

    if (
        "莉可丽丝" in path_text
        and "友谊是时间的窃贼" in path_text
    ) or "friends are thieves of time" in path_text:
        episode_match = re.search(r"S00E(\d+)", file_text, re.IGNORECASE)
        special_number = int(episode_match.group(1)) if episode_match else None
        return _series_special(
            "莉可丽丝",
            154494,
            special_number,
            "友谊是时间的窃贼",
            "已核实为《莉可丽丝》六篇短篇动画",
        )

    return None


_VERIFIED_TMDB_BINDINGS = (
    # 电影与总集篇使用强绑定，避免罗马音、译名和副标题导致候选漂移。
    VerifiedTmdbBinding(("mygo!!!!!", "后篇"), 1233186, "movie", "迷途之子!!!!! 后篇：唱吧、成为我们羁绊的诗歌＆电影演唱会"),
    VerifiedTmdbBinding(("mygo!!!!!", "前篇"), 1231799, "movie", "迷途之子!!!!! 前篇：春暖向阳，迷星之猫"),
    VerifiedTmdbBinding(("银河特急", "各站停车前往剧场"), 1598785, "movie", "银河特急 Milky☆Subway 各站停车前往剧场"),
    # Re- 与 Re:Re: 只差一个后缀，必须把更具体的规则放在前面。
    VerifiedTmdbBinding(("剧场总集篇", "孤独摇滚", "re-re"), 1201387, "movie", "孤独摇滚 (下)"),
    VerifiedTmdbBinding(("剧场总集篇", "孤独摇滚", "re-"), 1129610, "movie", "孤独摇滚 (上)"),
    VerifiedTmdbBinding(("飞跃巅峰 内封中字",), 66931, "tv", "飞跃巅峰!"),
    VerifiedTmdbBinding(("top o nerae",), 66931, "tv", "飞跃巅峰!"),
    VerifiedTmdbBinding(("demon slayer",), 85937, "tv", "鬼灭之刃"),
    VerifiedTmdbBinding(("鬼灭之刃系列",), 85937, "tv", "鬼灭之刃"),
    VerifiedTmdbBinding(("kono subarashii sekai ni shukufuku wo! movie",), 532067, "movie", "为美好的世界献上祝福！红传说"),
    VerifiedTmdbBinding(("小鸟游六花", "改"), 214553, "movie", "小鸟游六花·改"),
    VerifiedTmdbBinding(("take on me", "中二病"), 460399, "movie", "中二病也要谈恋爱！Take On Me"),
    VerifiedTmdbBinding(("梦幻银河乐园",), 959646, "movie", "佐贺偶像是传奇 梦幻银河乐园"),
    VerifiedTmdbBinding(("alternative gun gale online",), 78204, "tv", "刀剑神域外传 Gun Gale Online"),
    VerifiedTmdbBinding(("gun gale online", "外传"), 78204, "tv", "刀剑神域外传 Gun Gale Online"),
    VerifiedTmdbBinding(("叹息之丘的圣星",), 80518, "movie", "钢之炼金术师：叹息之丘的圣星"),
    VerifiedTmdbBinding(("millennium.actress",), 33320, "movie", "千年女优"),
    VerifiedTmdbBinding(("revue starlight", "剧场版"), 645440, "movie", "少女歌剧 Revue Starlight 剧场版"),
    VerifiedTmdbBinding(("我想吃掉你的胰脏",), 504253, "movie", "我想吃掉你的胰脏"),
    VerifiedTmdbBinding(("无职转生2",), 94664, "tv", "无职转生～到了异世界就拿出真本事～"),
    VerifiedTmdbBinding(("perfect.blue",), 10494, "movie", "未麻的部屋"),
    VerifiedTmdbBinding(("gekijouban violet evergarden",), 533514, "movie", "紫罗兰永恒花园 剧场版"),
    VerifiedTmdbBinding(("paprika",), 4977, "movie", "红辣椒"),
    VerifiedTmdbBinding(("路人女主", "fine"), 608826, "movie", "路人女主的养成方法 Fine"),
    VerifiedTmdbBinding(("yuru camp movie",), 566466, "movie", "摇曳露营△ 剧场版"),
    VerifiedTmdbBinding(("clannad", "剧场版"), 16516, "movie", "CLANNAD 剧场版"),
    VerifiedTmdbBinding(("福音战士新剧场版", "序"), 15137, "movie", "福音战士新剧场版：序"),
    VerifiedTmdbBinding(("福音战士新剧场版", "破"), 22843, "movie", "福音战士新剧场版：破"),
)


def match_verified_tmdb_binding(relative_path: str) -> VerifiedTmdbBinding | None:
    """返回路径中全部标题标记均命中的已核验 TMDB 绑定。"""
    path_text = unicodedata.normalize("NFKC", relative_path or "").casefold()
    for binding in _VERIFIED_TMDB_BINDINGS:
        if all(unicodedata.normalize("NFKC", marker).casefold() in path_text for marker in binding.markers):
            return binding
    return None


def match_verified_tmdb_season(tmdb_id: int, source_path: str) -> int | None:
    """返回本地目录编号与 TMDB 官方季号不同的已核验季号。"""
    if tmdb_id != 85937:
        return None
    text = unicodedata.normalize("NFKC", source_path or "").casefold()
    for markers, season_number in (
        (("柱训练", "hashira training"), 5),
        (("锻刀村", "swordsmith village"), 4),
        (("游郭", "花街", "entertainment district"), 3),
        (("无限列车", "mugen train"), 2),
        (("立志", "tanjiro kamado"), 1),
    ):
        if any(marker in text for marker in markers):
            return season_number
    return None


def match_verified_tmdb_episode_placement(
    tmdb_id: int,
    source_path: str,
    episode_number: int,
) -> tuple[int, int] | None:
    """返回已核实篇章对应的 TMDB 季号与季内集号。

    《鬼灭之刃》部分发布目录使用跨篇章绝对集号：无限列车篇从 27 开始，
    游郭篇从 34 开始。识别阶段必须先归位，否则镜像会把它们写进上一季。
    已经是季内编号的文件保持原值。
    """
    season = match_verified_tmdb_season(tmdb_id, source_path)
    if season is None:
        return None
    episode = int(episode_number)
    if tmdb_id == 85937:
        offsets = {2: (7, 26), 3: (11, 33)}
        max_episode, offset = offsets.get(season, (0, 0))
        if offset and episode > max_episode:
            episode -= offset
    return season, episode


_VERIFIED_ALIAS_GROUPS = (
    ("拔作岛", "住在拔作岛上的我应该如何是好？", "Nukitashi the Animation"),
    ("无职转生2", "无职转生Ⅱ", "无职转生～到了异世界就拿出真本事～", "Mushoku Tensei"),
    ("成长秀～向日葵马戏团～", "Grow Up Show ～向日葵马戏团～"),
    ("再见，拉拉", "再见菈菈", "Goodbye Lara", "さよならララ"),
    ("Hyouka", "冰菓", "氷菓"),
    ("KonoSuba", "为美好的世界献上祝福！", "この素晴らしい世界に祝福を！"),
    ("Yuru Camp", "摇曳露营△", "ゆるキャン△"),
)


def _normalize_alias(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", normalized)


_ALIAS_INDEX = {
    _normalize_alias(alias): group_index
    for group_index, aliases in enumerate(_VERIFIED_ALIAS_GROUPS)
    for alias in aliases
}


def titles_share_verified_alias(left: str, right: str) -> bool:
    """仅对明确核验过的跨语言/长短标题返回 True。"""
    left_key = _normalize_alias(left)
    right_key = _normalize_alias(right)
    if not left_key or not right_key:
        return False
    left_group = _ALIAS_INDEX.get(left_key)
    return left_group is not None and left_group == _ALIAS_INDEX.get(right_key)
