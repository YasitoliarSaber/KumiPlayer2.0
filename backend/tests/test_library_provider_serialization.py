# -*- coding: utf-8 -*-
"""OLIST-02-R2：Library API 序列化 / library_index.json 反序列化保留 provider 字段。

覆盖验收缺口：
- _work_to_dict() 输出 provider_id/ingest_method/source_route_id 与 episode.provider_id；
- _work_summary_to_dict() 输出 provider 三字段；
- save_library_index → load_library_index 往返保留 provider 三字段。
"""

from app.library.models import EpisodeIndex, WorkIndex
from app.library.store import load_library_index


def _make_work(provider_id: str = "quark") -> WorkIndex:
    return WorkIndex(
        work_id="w-provider-1",
        title="测试作品",
        source="openlist",
        sources=["openlist"],
        provider_id=provider_id,
        ingest_method="openlist_api",
        source_route_id="route-q",
        episodes=[
            EpisodeIndex(
                episode_id="ep-1",
                work_id="w-provider-1",
                source="openlist",
                provider_id=provider_id,
                season_number=1,
                episode_number=1,
                title="第 1 集",
                strm_path="mirror/openlist/测试作品/S01/ep-1.strm",
            )
        ],
    )


class TestWorkToDictProviderFields:
    def test_work_to_dict_serializes_provider_fields(self):
        from app.library.service import _work_to_dict

        data = _work_to_dict(_make_work())
        assert data["provider_id"] == "quark"
        assert data["ingest_method"] == "openlist_api"
        assert data["source_route_id"] == "route-q"
        assert data["episodes"][0]["provider_id"] == "quark"

    def test_work_summary_to_dict_serializes_provider_fields(self):
        from app.library.service import _work_summary_to_dict

        data = _work_summary_to_dict(_make_work())
        assert data["provider_id"] == "quark"
        assert data["ingest_method"] == "openlist_api"
        assert data["source_route_id"] == "route-q"


class TestLibraryIndexRoundTripProviderFields:
    def test_save_load_preserves_provider_fields(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
        from app.library.models import LibraryIndex
        from app.library.store import save_library_index

        index = LibraryIndex(works=[_make_work()])
        save_library_index(index)

        loaded = load_library_index()
        assert loaded is not None
        work = loaded.works[0]
        assert work.provider_id == "quark"
        assert work.ingest_method == "openlist_api"
        assert work.source_route_id == "route-q"
        assert work.episodes[0].provider_id == "quark"

    def test_load_missing_provider_fields_falls_back_empty(self, monkeypatch, tmp_path):
        """旧库索引无 provider 字段时兼容读取（空值回退，不崩溃）。"""
        monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
        import json

        from app.library.store import _get_index_path

        index_path = _get_index_path()
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "works": [
                        {
                            "work_id": "legacy-work",
                            "title": "旧作品",
                            "source": "openlist",
                            "sources": ["openlist"],
                            "episodes": [
                                {
                                    "episode_id": "legacy-ep",
                                    "work_id": "legacy-work",
                                    "source": "openlist",
                                    "season_number": 1,
                                    "episode_number": 1,
                                    "strm_path": "mirror/openlist/旧作品/S01/legacy-ep.strm",
                                }
                            ],
                        }
                    ],
                    "generated_at": "2026-08-10T00:00:00+08:00",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        loaded = load_library_index()
        assert loaded is not None
        work = loaded.works[0]
        assert work.provider_id == ""
        assert work.ingest_method == ""
        assert work.source_route_id == ""
        assert work.episodes[0].provider_id == ""
