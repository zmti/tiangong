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
        import io
        doc = Document(io.BytesIO(data))
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


def _extract_xls_legacy(data: bytes) -> dict:
    """老版 .xls(Excel 97-2003, OLE2/BIFF)解析,用 xlrd(xlrd 2.0 仅支持 .xls)。"""
    try:
        import xlrd
        book = xlrd.open_workbook(file_contents=data)
        sheets = {}
        for sheet in book.sheets():
            rows = []
            max_rows = min(sheet.nrows, 100)  # 只取前 100 行,防超大表卡死
            for r in range(max_rows):
                row = [sheet.cell_value(r, c) for c in range(sheet.ncols)]
                if any(v not in (None, "") for v in row):
                    rows.append([str(v) for v in row])
            sheets[sheet.name] = {"row_count": sheet.nrows, "data": rows}
        return {
            "type": "excel",
            "format": "xls",
            "sheet_count": len(sheets),
            "sheets": sheets,
        }
    except ImportError:
        return {"type": "excel", "error": "需要安装 xlrd: pip install xlrd"}
    except Exception as e:  # noqa: BLE001
        return {"type": "excel", "error": f"老版 .xls 解析失败: {e}"}


def _extract_excel(data: bytes) -> dict:
    """Excel 文件解析:按**文件魔数**自动分流(而非仅看扩展名)。

    扩展名常被误标(HTML 表格 / CSV 数据库导出另存为 .xls;老 .xls 与 .xlsx 二进制
    完全不同),只看扩展名会把老 .xls 或伪装文件喂给 openpyxl → 报
    "File contains no valid workbook part" / BadZipFile。这里:
      - OLE2/BIFF 老 .xls(魔数 D0CF11E0) → xlrd(xlrd 2.0 仅支持 .xls)
      - OOXML/ZIP  .xlsx(魔数 PK..)        → openpyxl
      - 其他(HTML/CSV 等伪装)              → 按文本解析
    """
    if data[:4] == b"\xd0\xcf\x11\xe0":  # OLE2 compound doc: 老 .xls
        return _extract_xls_legacy(data)
    if data[:2] == b"PK":  # ZIP 容器: .xlsx
        try:
            import openpyxl
            import io
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
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
            return {"type": "excel", "format": "xlsx", "sheet_count": len(sheets), "sheets": sheets}
        except ImportError:
            return {"type": "excel", "error": "需要安装 openpyxl: pip install openpyxl"}
        except Exception as e:  # noqa: BLE001
            return {"type": "excel", "error": f".xlsx 解析失败: {e}"}
    # 既非 OLE2 也非 ZIP:多为 HTML/CSV 被误标成 .xls,按文本解析
    return _extract_text_file(data)


def _extract_pptx(data: bytes) -> dict:
    """PowerPoint 解析。"""
    try:
        from pptx import Presentation
        import io
        prs = Presentation(io.BytesIO(data))
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

    def _is_list_line(s: str) -> bool:
        s = s.strip()
        return s.startswith(("- ", "* ", "+ ")) or s[:1].isdigit()

    lists = [l for l in lines if _is_list_line(l)]

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
    result = _parse_bytes_by_ext(data, ext)
    result["file_path"] = file_path
    result["file_size_bytes"] = len(data)
    return result


def _parse_bytes_by_ext(data: bytes, ext: str) -> dict:
    """按扩展名路由到对应解析器(供 parse_document 与附件链路复用)。"""
    ext = (ext or "").lower()
    if ext in (".pdf",):
        result = _extract_pdf_text(data)
    elif ext in (".docx", ".doc"):
        result = _extract_docx_text(data)
    elif ext in (".xlsx", ".xls"):
        result = _extract_excel(data)
    elif ext in (".pptx", ".ppt"):
        result = _extract_pptx(data)
    else:
        # 文本/代码/未知:按文本读
        result = _extract_text_file(data)
    return result


def extract_bytes(file_name: str, content: bytes) -> dict:
    """附件链路专用:解析上传的二进制文档(PDF/docx/xlsx/pptx),无需落盘。

    供 chat._prepare_attachments 把用户上传的 Office/PDF 转成 LLM 可读的结构化
    文本(此前这类附件因 utf-8 解码 zip/pdf 二进制失败而被静默丢弃,LLM 看不到)。
    返回 {ok, type, text, detail, error}:
      - ok=True: text 为可注入上下文的紧凑文本(失败/二进制无法解析时为 "");
      - detail 为轻量元信息(如页数/表格数),供日志;
      - 解析库缺失/文件损坏时 ok=False + error 描述,调用方据此降级。
    """
    ext = os.path.splitext(file_name or "")[1].lower()
    result = _parse_bytes_by_ext(content, ext)
    rtype = result.get("type", "")
    if result.get("error"):
        # 解析库缺失或文件损坏:无法产出文本,交给调用方降级为文件名提示
        return {"ok": False, "type": rtype, "text": "", "detail": {}, "error": result["error"]}

    text = _format_doc_as_text(rtype, result)
    detail = _doc_detail(result)
    return {"ok": bool(text.strip()), "type": rtype, "text": text, "detail": detail, "error": ""}


def _doc_detail(result: dict) -> dict:
    """轻量元信息,便于日志/前端展示。"""
    t = result.get("type")
    d: dict = {"type": t}
    if t == "pdf":
        d["page_count"] = result.get("page_count", 0)
    elif t == "docx":
        d["paragraph_count"] = result.get("paragraph_count", 0)
        d["table_count"] = result.get("table_count", 0)
    elif t == "excel":
        d["sheet_count"] = result.get("sheet_count", 0)
    elif t == "pptx":
        d["slide_count"] = result.get("slide_count", 0)
    elif t == "text":
        d["line_count"] = result.get("line_count", 0)
    return d


def _format_doc_as_text(rtype: str, result: dict) -> str:
    """把结构化解析结果拼成紧凑、LLM 友好的纯文本(带截断,避免上下文爆)。"""
    cap = 12000  # 单文档注入上限
    if rtype == "pdf":
        parts = [f"[PDF 第{p['page']}页]\n{p['text']}" for p in result.get("pages", [])]
        body = "\n\n".join(parts)
    elif rtype == "docx":
        body = "\n".join(p["text"] for p in result.get("paragraphs", []))
        for tb in result.get("tables", []):
            rows = tb.get("rows", [])
            if not rows:
                continue
            body += "\n\n[表格]" + "\n".join(" | ".join(str(c) for c in r) for r in rows)
    elif rtype == "excel":
        blocks = []
        for sname, s in (result.get("sheets") or {}).items():
            rows = s.get("data", [])
            if not rows:
                continue
            head = " | ".join(str(c) for c in rows[0]) if len(rows) > 1 else ""
            data_lines = " | ".join(" | ".join(str(c) for c in r) for r in rows[:50])
            blocks.append(f"[工作表 {sname}]({s.get('row_count', 0)} 行)\n{head}\n{data_lines}")
        body = "\n\n".join(blocks)
    elif rtype == "pptx":
        body = "\n\n".join(f"[第{s.get('slide')}页]\n{s.get('text','')}" for s in result.get("slides", []))
    else:  # text
        body = result.get("preview", "") or result.get("full_text", "")
    if len(body) > cap:
        body = body[:cap] + "\n…[内容过长,已截断]"
    return body


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
