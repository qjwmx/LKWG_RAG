"""文本切分。

用 LangChain 的 ``RecursiveCharacterTextSplitter``：它按"语义边界从强到弱"
递归下探（段落 > 换行 > 中文句末标点 > 英文句末标点 > 空格 > 字符），
正是切分该做的事，不必自己维护一套分隔符优先级。

**短文本不切**：切分是为了让检索命中更精准，把一段完整的话切成两半反而
会让两边都缺语义。所以短于 ``min_chunk_length`` 的文档整篇作为一个切片。

中文标点必须排在英文前面，否则中文文档会被英文标点规则切得七零八落。
"""

from __future__ import annotations

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import settings

# 顺序即优先级。中文标点在英文之前是刻意的。
_SEPARATORS = [
    "\n\n",
    "\n",
    "。",
    "！",
    "？",
    "；",
    ".",
    "!",
    "?",
    ";",
    "，",
    ",",
    " ",
    "",
]


def _build_splitter(chunk_size: int, chunk_overlap: int) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=_SEPARATORS,
        keep_separator=True,
        length_function=len,
    )


def split_text(
    text: str,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> list[str]:
    """把长文本切成有重叠的片段。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= settings.min_chunk_length:
        return [text]

    splitter = _build_splitter(
        chunk_size or settings.chunk_size,
        chunk_overlap or settings.chunk_overlap,
    )
    chunks = [piece.strip() for piece in splitter.split_text(text)]
    return [piece for piece in chunks if piece]
