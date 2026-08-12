# -*- coding: utf-8 -*-
"""分集标题与发行参数的保守判定。"""

import re


_TECH_TOKENS = {
    "bd", "bdrip", "bluray", "blu-ray", "web", "webdl", "webrip",
    "remux", "avc", "hevc", "x264", "x265", "h264", "h265",
    "flac", "aac", "opus", "truehd", "atmos", "dts", "dtshd",
    "jpn", "jp", "eng", "chs", "cht", "gb", "big5", "dual",
    "ma10p", "hi10p", "10bit", "8bit", "hdr", "sdr", "uhd",
}


def is_release_metadata_title(value: str) -> bool:
    """判断文本是否只有年份、画质、编码、音轨和发布组信息。"""
    text = " ".join((value or "").split()).strip(" ._-")
    if not text:
        return False
    if re.search(r"[\u3400-\u9fff\u3040-\u30ff]", text):
        return False

    unwrapped = re.sub(r"[\[\]【】()（）]", " ", text)
    normalized = re.sub(r"[._/\\-]+", " ", unwrapped.lower())
    tokens = [token for token in normalized.split() if token]
    if not tokens:
        return False

    known = 0
    unknown = []
    for token in tokens:
        compact = token.replace("-", "")
        if token in _TECH_TOKENS or compact in _TECH_TOKENS:
            known += 1
        elif re.fullmatch(r"(?:19|20)\d{2}", token):
            known += 1
        elif re.fullmatch(r"\d{3,4}p", token):
            known += 1
        elif re.fullmatch(r"\d{1,2}bit", token):
            known += 1
        elif re.fullmatch(r"\d", token) and re.search(r"\d[._ ]\d", text):
            known += 1
        else:
            unknown.append(token)

    if not unknown:
        return known > 0
    # 发布组名通常是技术参数串末尾唯一的未知 token，例如 ZeroTV。
    return known >= 2 and len(unknown) == 1 and bool(re.fullmatch(r"[a-z0-9]+", unknown[0]))

