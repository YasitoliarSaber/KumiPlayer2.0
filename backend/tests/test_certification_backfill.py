# -*- coding: utf-8 -*-

from pathlib import Path

from app.scrape.certification_backfill import update_nfo_certification


def test_update_nfo_certification_only_fills_missing_value(tmp_path: Path):
    path = tmp_path / "tvshow.nfo"
    path.write_text("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<tvshow><title>示例</title></tvshow>", encoding="utf-8")

    assert update_nfo_certification(path, "TV-14", "US") is True
    content = path.read_text(encoding="utf-8")
    assert "<mpaa>TV-14</mpaa>" in content
    assert "<certificationcountry>US</certificationcountry>" in content

    assert update_nfo_certification(path, "R", "US") is False
    assert "<mpaa>TV-14</mpaa>" in path.read_text(encoding="utf-8")
