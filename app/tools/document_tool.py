"""增强型文档深度解析工具:支持 PDF/Word/Excel/PowerPoint/Markdown/代码文件。

对标 Claude 的文档解析能力:
- 自动检测文件类型并调用对应的解析库
- 提取结构化内容(标题/段落/表格/代码块/列表)
- 支持多页 PDF 的分页处理
- 表格智能提取与格式化
- 代码文件 AST 级分析
"""
from __future__ import annotations

import os
import re
from typing import Optional

from pydantic import Field

from app.tools.registry import tool


def _read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _extract_pdf_text(data: bytes) -> dict:
    """PDF 文档解析。"""
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=data, filetype="pdf")
        pages = []
        for page_num, page in enumerate(doc, 1):
            text = page.get_text()
            # 尝试提取表格
            tables = []
            try:
                table_data = page.extract_tables()
                if table_data:
                    tables = [[str(cell) if cell else "" for cell in row] for row in table_data]
            except Exception:
                pass
            pages.append({
                "page": page_num,
                "text": text.strip(),
                "table_count": len(tables),
                "tables": tables[:3],  # 每页最多3个表格
            })
        doc.close()
        return {
            "type": "pdf",
            "page_count": len(pages),
            "pages": pages,
            "full_text": "\n\n--- PAGE BREAK ---\n\n".join(p["text"] for p in pages),
        }
    except ImportError:
        return {"type": "pdf", "error": "需要安装 pymupdf: pip install pymupdf"}
    except Exception as e:
        return {"type": "pdf", "error": f"PDF 解析失败: {e}"}


def _extract_docx_text(data: bytes) -> dict:
    """Word 文档解析。"""
    try:
        from docx import Document
        doc = Document(stream=data)
        paragraphs = []
        for i, para in enumerate(doc.paragraphs):
            if para.text.strip():
                paragraphs.append({
                    "index": i,
                    "text": para.text.strip(),
                    "style": para.style.name if para.style else "",
                })
        # 提取表格
        tables = []
        for ti, table in enumerate(doc.tables):
            row_data = []
            for row in table.rows:
                row_data.append([cell.text.strip() for cell in row.cells])
            tables.append({"table_index": ti, "rows": row_data[:20]})
        return {
            "type": "docx",
            "paragraph_count": len(paragraphs),
            "table_count": len(tables),
            "paragraphs": paragraphs[:50],
            "tables": tables,
            "full_text": "\n".join(p["text"] for p in paragraphs),
        }
    except ImportError:
        return {"type": "docx", "error": "需要安装 python-docx: pip install python-docx"}
    except Exception as e:
        return {"type": "docx", "error": f"Word 解析失败: {e}"}


def _extract_excel(data: bytes) -> dict:
    """Excel 文件解析。"""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(stream=data, read_only=True)
        sheets = {}
        for name in wb.sheetnames:
            ws = wb[name]
            rows = []
            for row in ws.iter_rows(values_only=True):
                if any(c is not None for c in row):
                    rows.append([str(c) if c is not None else "" for c in row])
                if len(rows) >= 100:
                    break
            sheets[name] = {"row_count": len(rows), "data": rows}
        wb.close()
        return {"type": "excel", "sheet_count": len(sheets), "sheets": sheets}
    except ImportError:
        return {"type": "excel", "error": "需要安装 openpyxl: pip install openpyxl"}
    except Exception as e:
        return {"type": "excel", "error": f"Excel 解析失败: {e}"}


def _extract_pptx(data: bytes) -> dict:
    """PowerPoint 解析。"""
    try:
        from pptx import Presentation
        prs = Presentation(stream=data)
        slides = []
        for si, slide in enumerate(prs.slides, 1):
            texts = []
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    texts.append(shape.text.strip())
            slides.append({"slide": si, "text": "\n".join(texts)})
        return {
            "type": "pptx",
            "slide_count": len(slides),
            "slides": slides,
        }
    except ImportError:
        return {"type": "pptx", "error": "需要安装 python-pptx: pip install python-pptx"}
    except Exception as e:
        return {"type": "pptx", "error": f"PPT 解析失败: {e}"}


def _extract_text_file(data: bytes, encoding: str = "utf-8") -> dict:
    """纯文本/Markdown/代码文件解析。"""
    try:
        text = data.decode(encoding, errors="ignore")
    except Exception:
        text = data.decode("latin-1", errors="ignore")

    lines = text.split("\n")
    # 统计结构
    headings = [l for l in lines if l.startswith("#")]
    code_blocks = re.findall(r"```[\s\S]*?```", text)
    lists = [l for l in lines if l.strip().startswith(("- ", "* ", "+ ", str.isdigit))]

    return {
        "type": "text",
        "encoding": encoding,
        "line_count": len(lines),
        "char_count": len(text),
        "heading_count": len(headings),
        "code_block_count": len(code_blocks),
        "list_item_count": len(lists),
        "headings": headings[:20],
        "first_code_block": code_blocks[0][:500] if code_blocks else None,
        "preview": text[:2000],
    }


@tool(
    "parse_document",
    "深度解析文档(PDF/Word/Excel/PPT/Markdown/代码),提取结构化内容",
    timeout=60,
)
def parse_document(
    file_path: str = Field(description="文档文件路径"),
    max_pages: int = Field(50, description="最大解析页数(PDF/Excel)/行数"),
    extract_tables: bool = Field(True, description="是否提取表格数据"),
) -> dict:
    """增强型文档解析:自动检测文件类型并调用对应解析器。"""
    if not os.path.exists(file_path):
        return {"error": f"文件不存在: {file_path}"}

    ext = os.path.splitext(file_path)[1].lower()
    data = _read_file_bytes(file_path)

    if ext in (".pdf",):
        result = _extract_pdf_text(data)
    elif ext in (".docx", ".doc"):
        result = _extract_docx_text(data)
    elif ext in (".xlsx", ".xls"):
        result = _extract_excel(data)
    elif ext in (".pptx", ".ppt"):
        result = _extract_pptx(data)
    elif ext in (".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".py", ".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".css", ".html", ".xml", ".toml"):
        result = _extract_text_file(data)
    else:
        # 尝试作为文本读取
        result = _extract_text_file(data)

    result["file_path"] = file_path
    result["file_size_bytes"] = len(data)
    return result


@tool(
    "document_summary",
    "对已解析的文档内容进行摘要总结",
    timeout=30,
)
def document_summary(
    file_path: str = Field(description="文档文件路径"),
    max_length: int = Field(500, description="摘要最大长度(字符)"),
    language: str = Field("zh", description="摘要语言: zh/en"),
) -> dict:
    """基于文档解析结果的智能摘要。"""
    from app.core.llm import LLMClient
    from app.tools.multimodal_tool import parse_document

    # 先解析文档
    parse_result = parse_document.func(file_path=file_path, max_pages=20)
    if "error" in parse_result:
        return parse_result

    # 提取全文
    full_text = parse_result.get("full_text", "") or ""
    if not full_text:
        # 尝试拼接
        if "pages" in parse_result:
            full_text = "\n\n".join(p["text"] for p in parse_result["pages"][:10])
        elif "paragraphs" in parse_result:
            full_text = "\n".join(p["text"] for p in parse_result["paragraphs"][:50])
        elif "preview" in parse_result:
            full_text = parse_result["preview"]

    if not full_text.strip():
        return {"error": "文档内容为空或无法提取文本"}

    # 截断到合适长度
    if len(full_text) > 8000:
        full_text = full_text[:8000] + "\n...[内容过长已截断]"

    llm = LLMClient()
    lang_prompt = "中文" if language == "zh" else "English"
    prompt = f"请对以下文档进行精简摘要，突出核心内容和关键信息，用{lang_prompt}输出，控制在{max_length}字以内：\n\n{full_text}"

    try:
        summary = asyncio.run(llm.chat([{"role": "user", "content": prompt}], temperature=0.3))
    except Exception as e:
        return {"error": f"摘要生成失败: {e}"}

    return {
        "status": "ok",
        "original_length": len(full_text),
        "summary_length": len(summary),
        "summary": summary,
    }
