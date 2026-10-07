"""多格式文档解析：把上传的字节转成归一化纯文本。

支持的扩展名见 ``config.SUPPORTED_FILE_TYPES``。分派按扩展名（不按 MIME——
浏览器的 MIME 对 ``.md`` 常常给 ``application/octet-stream``，不可靠）。

**``.md`` / ``.markdown`` 走纯文本路径**，Markdown 语法刻意**保留不清洗**：
``#`` 标题层级天然是切分边界，让 chunk 语义更完整；表格与列表符号也有助于
模型理解结构。只做 BOM 剥离、行尾归一与空行压缩。
"""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 文本类文件按顺序尝试的编码。gbk 放最后：它是中文 Windows 的常见默认，
# 但 utf-8 的字节流有时也能被 gbk "成功"解出乱码，所以必须先试 utf-8。
_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gbk")


class ParseError(ValueError):
    """解析失败，消息可直接展示给用户。"""


def parse(file_name: str, file_bytes: bytes) -> tuple[str, str]:
    """返回 ``(归一化文本, 扩展名)``。失败抛 ``ParseError``。"""
    suffix = Path(file_name).suffix.lower().lstrip(".")
    if not suffix:
        raise ParseError("文件没有扩展名，无法判断类型。")

    if suffix in ("txt", "md", "markdown"):
        text = _decode_text(file_bytes)
    elif suffix == "pdf":
        text = _parse_pdf(file_bytes)
    elif suffix == "docx":
        text = _parse_docx(file_bytes)
    elif suffix == "xlsx":
        text = _parse_excel(file_bytes)
    elif suffix == "csv":
        text = _parse_csv(file_bytes)
    else:
        raise ParseError(f"暂不支持的文件类型：.{suffix}")

    normalized = normalize_text(text)
    if not normalized:
        raise ParseError("文档中没有提取到可用文本，请检查文件内容。")
    return normalized, suffix


def _decode_text(file_bytes: bytes) -> str:
    for encoding in _TEXT_ENCODINGS:
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ParseError("文本文件编码无法识别，建议转为 UTF-8 后再上传。")


def _parse_pdf(file_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的可读提示
        raise ParseError("缺少 pypdf 依赖，无法解析 PDF。请 pip install pypdf。") from exc

    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 - 转成可读提示
        raise ParseError(f"PDF 解析失败：{type(exc).__name__}: {exc}") from exc

    text = "\n".join(pages)
    if not text.strip():
        # 扫描件没有文本层。这不算异常，但用户必须知道原因，
        # 否则会以为"上传成功了却搜不到"。
        raise ParseError("PDF 中没有可提取的文本（可能是扫描件/图片型 PDF）。")
    return text


def _parse_docx(file_bytes: bytes) -> str:
    try:
        from docx import Document as DocxDocument
    except ImportError as exc:  # pragma: no cover
        raise ParseError("缺少 python-docx 依赖，无法解析 DOCX。") from exc

    try:
        document = DocxDocument(io.BytesIO(file_bytes))
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"DOCX 解析失败：{type(exc).__name__}: {exc}") from exc

    parts = [p.text for p in document.paragraphs if p.text.strip()]
    # 表格里的内容常常是制度文档的关键信息（如审批层级），漏掉会答不出来
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _parse_excel(file_bytes: bytes) -> str:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise ParseError("缺少 openpyxl 依赖，无法解析 XLSX。") from exc

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"XLSX 解析失败：{type(exc).__name__}: {exc}") from exc

    sheets: list[str] = []
    for sheet in workbook.worksheets:
        lines: list[str] = []
        for row in sheet.iter_rows(values_only=True):
            values = ["" if v is None else str(v).strip() for v in row]
            if any(values):
                lines.append(" | ".join(values))
        if lines:
            sheets.append(f"工作表：{sheet.title}\n" + "\n".join(lines))
    return "\n\n".join(sheets)


def _parse_csv(file_bytes: bytes) -> str:
    text = None
    for encoding in _TEXT_ENCODINGS:
        try:
            text = file_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ParseError("CSV 文件编码无法识别，建议转为 UTF-8 后再上传。")

    reader = csv.reader(io.StringIO(text))
    lines: list[str] = []
    for row in reader:
        values = [cell.strip() for cell in row]
        if any(values):
            lines.append(" | ".join(values))
    return "\n".join(lines)


def normalize_text(text: str) -> str:
    """BOM 剥离、行尾归一、去空行、去行首尾空白。

    **不去 Markdown 语法**（见模块 docstring）。
    """
    clean = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in clean.split("\n")]
    return "\n".join(line for line in lines if line)
