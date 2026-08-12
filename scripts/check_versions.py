"""检查 KumiPlayer 各运行层是否与根 package.json 版本一致。"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_toml(path: Path) -> dict:
    with path.open("rb") as file:
        return tomllib.load(file)


def main() -> int:
    expected = str(read_json(ROOT / "package.json")["version"])
    package_lock = read_json(ROOT / "package-lock.json")
    uv_lock = read_toml(ROOT / "backend" / "uv.lock")
    backend_lock_version = next(
        str(package["version"])
        for package in uv_lock["package"]
        if package["name"] == "kumiplayer-backend"
    )
    versions = {
        "package-lock.json": str(package_lock["version"]),
        "package-lock.json packages['']": str(package_lock["packages"][""]["version"]),
        "backend/pyproject.toml": str(read_toml(ROOT / "backend" / "pyproject.toml")["project"]["version"]),
        "backend/uv.lock": backend_lock_version,
        "src-tauri/Cargo.toml": str(read_toml(ROOT / "src-tauri" / "Cargo.toml")["package"]["version"]),
        "src-tauri/tauri.conf.json": str(read_json(ROOT / "src-tauri" / "tauri.conf.json")["version"]),
    }
    mismatches = {name: version for name, version in versions.items() if version != expected}
    if mismatches:
        print(f"版本基准 package.json = {expected}", file=sys.stderr)
        for name, version in mismatches.items():
            print(f"版本不一致：{name} = {version}", file=sys.stderr)
        return 1
    print(f"KumiPlayer 版本一致：{expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
