from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Certification:
    value: str = ""
    country: str = ""


def extract_certification(detail: dict, media_type: str, regions: Iterable[str]) -> Certification:
    candidates = _tv_candidates(detail) if media_type == "tv" else _movie_candidates(detail)
    ordered_regions = [str(region).upper() for region in regions]
    for region in ordered_regions:
        values = candidates.get(region) or []
        if values:
            return Certification(values[0][1], region)
    for country, values in candidates.items():
        if values:
            return Certification(values[0][1], country)
    return Certification()


def _tv_candidates(detail: dict) -> dict[str, list[tuple[int, str]]]:
    result: dict[str, list[tuple[int, str]]] = {}
    for item in (detail.get("content_ratings") or {}).get("results") or []:
        country = str(item.get("iso_3166_1") or "").upper()
        value = str(item.get("rating") or "").strip()
        if country and value:
            result.setdefault(country, []).append((0, value))
    return result


def _movie_candidates(detail: dict) -> dict[str, list[tuple[int, str]]]:
    result: dict[str, list[tuple[int, str]]] = {}
    priority = {3: 0, 2: 1, 4: 2, 6: 3, 5: 4, 1: 5}
    for group in (detail.get("release_dates") or {}).get("results") or []:
        country = str(group.get("iso_3166_1") or "").upper()
        for item in group.get("release_dates") or []:
            value = str(item.get("certification") or "").strip()
            if country and value:
                result.setdefault(country, []).append((priority.get(int(item.get("type") or 0), 9), value))
    for values in result.values():
        values.sort(key=lambda item: item[0])
    return result
