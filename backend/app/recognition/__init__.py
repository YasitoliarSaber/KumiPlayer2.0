"""保留的纯媒体命名规则。作品身份由 V4 Resolver 创建。"""

from app.recognition.media import MediaGuess, recognize_media
from app.recognition.title_cleaner import TitleCleanResult, clean_work_title_container

__all__ = [
    "MediaGuess",
    "TitleCleanResult",
    "clean_work_title_container",
    "recognize_media",
]
