"""文档管理:上传、解析、切块、索引到向量库。"""
from __future__ import annotations

import io
import uuid
from typing import Optional

from pydantic import Field

from app.rag.chunker import chunk_documents
from app.rag.embeddings import get_embedder, run_embed
from app.rag.retriever import get_store
from app.tools.registry import tool


def extract_text(content: bytes, filename: str) -> str:
    """从上传文件提取纯文本。

    支持 txt/md/json/csv 等文本文件,以及 pdf/docx/xlsx/xls/pptx 等文档格式;
    二进制或未支持的格式返回提示信息。
    """
    name = filename.lower()
    try:
        if name.endswith((".txt", ".md", ".markdown", ".json", ".csv", ".log", ".py", ".java", ".js", ".ts", ".html", ".xml")):
            return content.decode("utf-8", errors="ignore")
        # 尝试 utf-16 (Windows 记事本)
        if name.endswith((".txt", ".md")) and b"\xff\xfe" in content[:4]:
            return content.decode("utf-16-le", errors="ignore")
        if name.endswith(".pdf"):
            return _extract_pdf(content)
        if name.endswith(".docx"):
            return _extract_docx(content)
        if name.endswith(".doc"):
            return f"[文件 {filename} 为旧版 .doc 格式,请另存为 .docx 后上传]"
        if name.endswith(".xlsx"):
            return _extract_xlsx(content)
        if name.endswith(".xls"):
            return _extract_xls(content)
        if name.endswith(".pptx"):
            return _extract_pptx(content)
        return f"[文件 {filename} 是二进制或未支持的格式,无法提取文本]"
    except Exception:  # noqa: BLE001
        return f"[文件 {filename} 解析失败]"


def _cell_to_str(value: object) -> str:
    """表格单元格值转为文本:整数浮点去掉小数点,其余直接转字符串。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _extract_pdf(content: bytes) -> str:
    """从 PDF 二进制内容提取纯文本。"""
    import pymupdf

    doc = pymupdf.open(stream=content, filetype="pdf")
    try:
        pages = [page.get_text() for page in doc]
    finally:
        doc.close()
    text = "\n\n".join(pages).strip()
    if not text:
        return "[文件是扫描版 PDF,未包含可提取的文本层,请上传文字版或使用 OCR]"
    return text


def _extract_docx(content: bytes) -> str:
    """从 .docx 二进制内容提取段落与表格文本。"""
    from docx import Document

    doc = Document(io.BytesIO(content))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append("\t".join(cells))
    return "\n".join(parts).strip()


def _extract_xlsx(content: bytes) -> str:
    """从 .xlsx 二进制内容提取各工作表文本。"""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        parts: list[str] = []
        for ws in wb.worksheets:
            rows = [f"== 工作表: {ws.title} =="]
            for row in ws.iter_rows(values_only=True):
                cells = [_cell_to_str(v) for v in row if v is not None and str(v) != ""]
                if cells:
                    rows.append("\t".join(cells))
            if len(rows) > 1:
                parts.append("\n".join(rows))
    finally:
        wb.close()
    return "\n\n".join(parts).strip()


def _extract_xls(content: bytes) -> str:
    """从 .xls 二进制内容提取各工作表文本。"""
    import xlrd

    wb = xlrd.open_workbook(file_contents=content)
    parts: list[str] = []
    for sheet in wb.sheets():
        rows = [f"== 工作表: {sheet.name} =="]
        for r in range(sheet.nrows):
            cells = [_cell_to_str(v) for v in sheet.row_values(r) if v not in ("", None)]
            if cells:
                rows.append("\t".join(cells))
        if len(rows) > 1:
            parts.append("\n".join(rows))
    return "\n\n".join(parts).strip()


def _extract_pptx(content: bytes) -> str:
    """从 .pptx 二进制内容提取各幻灯片文本。"""
    from pptx import Presentation

    prs = Presentation(io.BytesIO(content))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, start=1):
        texts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    texts.append(t)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    if any(cells):
                        texts.append("\t".join(cells))
        if texts:
            parts.append(f"== 幻灯片 {i} ==\n" + "\n".join(texts))
    return "\n\n".join(parts).strip()


def index_document(filename: str, content: bytes, doc_id: Optional[str] = None) -> dict:
    """切块并索引单个文档,返回索引统计。"""
    doc_id = doc_id or uuid.uuid4().hex
    text = extract_text(content, filename)
    if text.startswith("["):
        return {"doc_id": doc_id, "filename": filename, "chunks": 0, "error": text}

    docs = [{"text": text, "source": filename}]
    chunks = chunk_documents(docs, chunk_size=500, overlap=50)
    embedder = get_embedder()
    store = get_store()

    # 先删除同文档旧索引(同名文件重新上传时替换)
    stale_ids = [
        cid for cid, meta in zip(store._ids, store._metas)  # noqa: SLF001
        if meta.get("source") == filename
    ]
    for cid in stale_ids:
        store.delete(cid)

    added = 0
    for chunk in chunks:
        vec = run_embed(embedder, [chunk["text"]])[0]
        cid = f"{doc_id}:{chunk['metadata']['chunk_index']}"
        store.add(cid, vec, {**chunk["metadata"], "text": chunk["text"], "doc_id": doc_id})
        added += 1

    return {"doc_id": doc_id, "filename": filename, "chunks": added}


def delete_document(source: str) -> dict:
    """删除指定文档(按 source=文件名)的所有向量块。"""
    store = get_store()
    stale_ids = [
        cid for cid, meta in zip(store._ids, store._metas)  # noqa: SLF001
        if meta.get("source") == source
    ]
    for cid in stale_ids:
        store.delete(cid)
    return {"source": source, "deleted_chunks": len(stale_ids)}


def index_text(text: str, source: str = "memory") -> dict:
    """直接索引文本(供代码调用)。"""
    doc_id = uuid.uuid4().hex
    chunks = chunk_documents([{"text": text, "source": source}], chunk_size=500, overlap=50)
    embedder = get_embedder()
    store = get_store()
    added = 0
    for chunk in chunks:
        vec = run_embed(embedder, [chunk["text"]])[0]
        cid = f"{doc_id}:{chunk['metadata']['chunk_index']}"
        store.add(cid, vec, {**chunk["metadata"], "text": chunk["text"], "doc_id": doc_id})
        added += 1
    return {"doc_id": doc_id, "source": source, "chunks": added}


@tool("rag_search", "在已上传的知识库文档中检索相关内容,返回最相关的文本片段(含来源与段落号,可引用)", timeout=15)
def rag_search(query: str = Field(description="检索问题"), top_k: int = Field(5, description="返回片段数")) -> str:
    from app.rag.retriever import get_retriever

    retriever = get_retriever()
    results = retriever.search(query, top_k=top_k)
    if not results:
        return {"query": query, "results": [], "note": "知识库中没有相关内容"}
    out = []
    for r in results:
        meta = r.get("metadata", {})
        out.append({
            "source": meta.get("source", "unknown"),
            "chunk_index": meta.get("chunk_index", 0),
            "doc_id": meta.get("doc_id", ""),
            "text": meta.get("text", ""),
            "score": round(r.get("score", 0), 4),
            "keyword_score": r.get("keyword_score", 0),
        })
    return {"query": query, "results": out}
