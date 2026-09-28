"""写入路径的 token 启发式估算（`docs/kb-workflow.md` §6）。

刻意**不引真 tokenizer**：卡片构建是同步作业里最热的循环，逐张卡片跑 `tiktoken`
会把同步拖成分钟级；`tiktoken` 只在最后组装 prompt 时按 `AIWEB_RETRIEVAL__TOKEN_BUDGET`
精算裁切。所以这里估出来的数只用于"这张卡要不要切片"，宁可高估。
"""

from __future__ import annotations

import math
import re
from typing import Final

# 汉字本体（不含全角标点：`（）`、`，` 这类不该按一个字算钱）
_CJK: Final = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
# ASCII 词：标识符里常见的字母/数字/下划线连成一串算一个词
_ASCII_WORD: Final = re.compile(r"[0-9A-Za-z_]+")

_ASCII_WEIGHT: Final = 1.3


def estimate_tokens(text: str) -> int:
    """`CJK 字符数 × 1 + ASCII 词数 × 1.3`，向上取整。"""
    cjk = len(_CJK.findall(text))
    ascii_words = len(_ASCII_WORD.findall(text))
    return math.ceil(cjk + ascii_words * _ASCII_WEIGHT)
