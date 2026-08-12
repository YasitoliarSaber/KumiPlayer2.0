"""分类列表单独投影本地镜像图片，不改写作品的远程图片引用。"""

from app.library.models import WorkIndex
from app.library.service import _work_summary_to_dict


def test_compact_library_exposes_local_artwork_alongside_remote_reference(tmp_path):
    work_dir = tmp_path / "mirror" / "作品A"
    work_dir.mkdir(parents=True)
    poster = work_dir / "poster.jpg"
    fanart = work_dir / "fanart.jpg"
    poster.write_bytes(b"poster")
    fanart.write_bytes(b"fanart")
    work = WorkIndex(
        work_id="work-a",
        title="作品A",
        source="pan115",
        dir_path=str(work_dir),
        poster_path="https://image.tmdb.org/t/p/w780/remote-poster.jpg",
        fanart_path="https://image.tmdb.org/t/p/original/remote-fanart.jpg",
    )

    compact = _work_summary_to_dict(work)

    assert compact["poster_path"].startswith("https://")
    assert compact["fanart_path"].startswith("https://")
    assert compact["local_poster_path"] == str(poster)
    assert compact["local_fanart_path"] == str(fanart)


def test_compact_keeps_remote_poster_when_no_local_file(tmp_path):
    work = WorkIndex(
        work_id="work-remote-only",
        title="无本地图片作品",
        source="pan115",
        dir_path=str(tmp_path / "不存在的目录"),
        poster_path="https://image.tmdb.org/t/p/w780/remote-poster.jpg",
        fanart_path="https://image.tmdb.org/t/p/original/remote-fanart.jpg",
    )

    compact = _work_summary_to_dict(work)

    assert compact["poster_path"].startswith("https://")
    assert compact["fanart_path"].startswith("https://")
    assert compact["local_poster_path"] == ""
    assert compact["local_fanart_path"] == ""
