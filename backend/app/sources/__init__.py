# -*- coding: utf-8 -*-
"""来源适配器包"""

from app.sources.pan115 import Pan115Adapter
from app.sources.baidu import BaiduAdapter
from app.sources.local import LocalScanner

__all__ = ["Pan115Adapter", "BaiduAdapter", "LocalScanner"]
