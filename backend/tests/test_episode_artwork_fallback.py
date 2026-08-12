# -*- coding: utf-8 -*-
"""详情页无剧集图时必须使用作品横幅。"""

from pathlib import Path


def test_episode_card_renders_the_resolved_preview_image():
    source = (Path(__file__).resolve().parents[2] / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")

    assert "{previewImage ? (" in source
    assert "src={previewImage}" in source
