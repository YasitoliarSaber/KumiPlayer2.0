"""标题规范化的两个语义：身份（identity）与匹配（match），不要混用。

项目里"规范化标题"其实在回答两个不同的问题：

- :func:`normalize_identity_title` —— **这是哪个作品**。结果会进入持久化身份键
  （``title:<标题>:<年份>:<类型>``）与作品去重判断，必须稳定：只做全角折半、
  大小写折叠、空白收敛，并去掉两端的装饰标点。**不能为了匹配更宽松而放宽它**，
  否则同一个标题会算出不同身份键，重新导入时会生成重复作品。
- :func:`normalize_match_title` —— **这是不是同一个标题**。只用于比较：草稿候选评分
  与刮削排名。网盘与 Provider 的标题常在标点上不同（``摇曳露营！`` 与 ``摇曳露营``、
  ``Re:Zero`` 与 ``Re Zero``），比较时必须忽略全部标点与空格。

两个函数都必须只有**唯一实现**。历史上有四份同名私有函数，其中三份是身份语义、
一份是匹配语义；跨组件比较时结论相反，表现为"导入预览说没把握、刮削却自动采用"
（或反过来）。反向统一同样危险：把身份语义放宽成匹配语义会改写已持久化的身份键。
"""

from __future__ import annotations

import re
import unicodedata

#: 身份语义只剥离两端的装饰标点（正文中的标点属于标题本身）。
_EDGE_TRIM = " ._-·:：/\\()（）【】[]{}<>《》「」『』\"'"

_WHITESPACE = re.compile(r"\s+")


def normalize_identity_title(value: str | None) -> str:
    """身份语义：全角折半 + 大小写折叠 + 空白收敛 + 去两端装饰标点。

    结果会参与持久化身份键，因此**只允许收紧实现、不允许放宽**。
    """

    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = _WHITESPACE.sub(" ", text).strip()
    return text.strip(_EDGE_TRIM)


def normalize_match_title(value: str | None) -> str:
    """匹配语义：只保留 Unicode 字母与数字，忽略全部标点与空白。

    不能枚举标点：网盘标题会同时出现中英文逗号、顿号、书名号与发行组分隔符，
    只保留字母数字才能让"同一部作品的不同写法"稳定判等。
    """

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in text if char.isalnum())
