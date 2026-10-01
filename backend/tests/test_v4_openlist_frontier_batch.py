from types import SimpleNamespace

from app.media_v4.sources.scanner import scan_openlist_directory


def test_page_registers_child_directories_as_one_batch_without_losing_files():
    pending = []
    registrations = []
    requests = []

    def add(*, remote_paths, depth):
        registrations.append(list(remote_paths))
        pending.extend({"remote_path": path, "depth": depth, "next_page": 1} for path in remote_paths)

    def list_dir(path, **kwargs):
        requests.append(path)
        paths = [f"/Anime/Show{i}" for i in range(3)] if path == "/Anime" else [path + "/Show.S01E01.mkv"]
        entries = [SimpleNamespace(remote_path=p, name=p.rsplit("/", 1)[-1], is_dir=path == "/Anime", size=1024, modified=0) for p in paths]
        return SimpleNamespace(entries=entries, total=len(entries))

    _, evidence = scan_openlist_directory(
        SimpleNamespace(list_dir=list_dir), remote_root="/Anime", mapping_root="/Anime",
        mount_root="", root_id="r", scan_id="s", default_provider="quark",
        frontier_add=add, frontier_next=lambda: pending.pop(0) if pending else None,
        frontier_mark=lambda **kwargs: None,
    )
    assert registrations == [["/Anime"], ["/Anime/Show0", "/Anime/Show1", "/Anime/Show2"]]
    assert len(evidence) == 3
    assert requests == ["/Anime", "/Anime/Show0", "/Anime/Show1", "/Anime/Show2"]
