from app.raw.models import RawFile, RawSnapshot
from app.recognition.planner import build_draft_import_plan
from app.recognition.plan_recognizer import recognize_import_plan_media


def _snapshot(import_family: str, relative_path: str) -> RawSnapshot:
    return RawSnapshot(
        snapshot_id=f"snap-{import_family}",
        source="pan115",
        source_root="H:/media",
        import_family=import_family,
        file_count=1,
        video_count=1,
        files=[
            RawFile(
                id="raw-1",
                source="pan115",
                source_root="H:/media",
                relative_path=relative_path,
                real_path=f"H:/media/{relative_path}",
                name=relative_path.rsplit("/", 1)[-1],
                ext=".mkv",
                is_file=True,
                resource_hint="video",
            )
        ],
    )


def test_import_family_live_overrides_animation_path_for_series():
    snapshot = _snapshot("live", "动画/夏日回声/S01E01.mkv")

    plan = recognize_import_plan_media(build_draft_import_plan(snapshot))
    item = plan.items[0]

    assert plan.import_family == "live"
    assert plan.summary["import_family"] == "live"
    assert item.import_family == "live"
    assert item.show_type == "live_series"


def test_import_family_anime_routes_movie_to_anime_movie():
    snapshot = _snapshot("anime", "电影/云端之约/云端之约.2024.mkv")

    plan = recognize_import_plan_media(build_draft_import_plan(snapshot))
    item = plan.items[0]

    assert plan.import_family == "anime"
    assert item.import_family == "anime"
    assert item.show_type == "anime_movie"
