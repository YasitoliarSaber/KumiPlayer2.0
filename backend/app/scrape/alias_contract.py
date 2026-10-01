"""只用于检索的名称证据；不授予 Provider 绑定或图片能力。"""

import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class AliasEvidence:
    title: str
    language: str
    provider: str
    provider_object_id: str
    source_url: str
    observed_at: str
    evidence_kind: str = "public_title"

    def to_dict(self) -> dict:
        return asdict(self)


def clean_aliases(values, *, limit: int = 6) -> list[str]:
    cleaned: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            continue
        value = " ".join(raw.split())
        if (not value or len(value) > 300 or not any(ch.isalpha() for ch in value)
                or re.search(r"[<>]|https?://|ignore.*instructions|忽略.*指令|system\s*prompt", value, re.I)
                or re.fullmatch(r"(?:S\d+)?E\d+|第\s*\d+\s*集", value, re.I)):
            continue
        if value not in cleaned:
            cleaned.append(value)
        if len(cleaned) >= limit:
            break
    return cleaned
