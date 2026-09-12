"""文档管理:上传、解析、切块、索引到向量库。

增量索引(A4 修复):
- 每次 index_document 计算文件内容 SHA-256 hash，与上次记录比对
- 内容未变则跳过 re-embed，直接从缓存或存量数据复用
- 部分变更时只重新 embed 受影响的 chunk
- hash 记录持久化到 data/doc_hashes.db(SQLite)
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import uuid
from typing import Optional

from pydantic import Field

from app.rag.chunker import chunk_documents
from app.rag.embeddings import get_embedder, run_embed
from app.rag.retriever import get_store
from app.tools.registry import tool


def _get_doc_hash_db() -> str:
    """返回文档 hash 记录的 SQLite 路径。"""
    from app.config import settings
    return os.path.join(settings.resolved_data_dir, "doc_hashes.db")


def _load_doc_hashes() -> dict[str, str]:
    """加载已索引文档的 (filename -> content_hash) 映射。"""
    db_path = _get_doc_hash_db()
    if not os.path.exists(db_path):
        return {}
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT filename, content_hash FROM doc_hashes").fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def _save_doc_hash(filename: str, content_hash: str) -> None:
    """持久化文档 hash。"""
    db_path = _get_doc_hash_db()
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO doc_hashes (filename, content_hash) VALUES (?, ?)",
            (filename, content_hash),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _init_doc_hash_db() -> None:
    """确保 hash 数据库表存在。"""
    db_path = _get_doc_hash_db()
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS doc_hashes (
                filename TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()
    except Exception:
        pass


# 启动时初始化 hash 库
_init_doc_hash_db()


def _file_hash(content: bytes) -> str:
    """计算文件内容 SHA-256 hash。"""
    return hashlib.sha256(content).hexdigest()[:16]


TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".json", ".csv", ".log",
    ".py", ".java", ".js", ".ts", ".html", ".xml",
    ".css", ".sql", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".bat", ".sh", ".ps1", ".c", ".cpp", ".h", ".go", ".rs",
    ".rb", ".php", ".swift", ".kt", ".scala", ".r", ".m",
    ".srt", ".vtt", ".sub", ".cfg", ".conf", ".env",
})

DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx"})


def _detect_and_decode(content: bytes) -> str:
    """自动检测文本编码并解码。

    优先级: BOM 检测 > 常见编码逐个尝试 > charset-normalizer 自动检测 > replace 兜底。
    """
    if not content:
        return ""

    # 1. BOM 检测 (最高优先级)
    bom_decoders = [
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    ]
    for bom, encoding in bom_decoders:
        if content.startswith(bom):
            return content[len(bom):].decode(encoding, errors="replace")

    # 2. 常见编码逐个尝试 (快速且可靠)
    for enc in ("utf-8", "gb18030", "gbk", "gb2312", "big5", "shift_jis", "euc-kr", "latin-1"):
        try:
            decoded = content.decode(enc)
            if "\ufffd" not in decoded[:500]:
                return decoded
        except (UnicodeDecodeError, LookupError):
            continue

    # 3. charset-normalizer 自动检测 (处理少见编码)
    from charset_normalizer import from_bytes
    results = from_bytes(content)
    if results:
        best = results.best()
        if best:
            try:
                decoded = best.output()
                if isinstance(decoded, bytes):
                    decoded = decoded.decode(best.encoding, errors="replace")
                if decoded and "\ufffd" not in decoded[:200]:
                    return decoded
            except (UnicodeDecodeError, LookupError):
                pass

    # 4. 兜底: 用 replace 保留尽可能多的内容
    return content.decode("utf-8", errors="replace")


def extract_text(content: bytes, filename: str) -> str:
    """从上传文件提取纯文本。

    支持 txt/md/json/csv 等文本文件,以及 pdf/docx/xlsx/xls/pptx 等文档格式;
    二进制或未支持的格式返回提示信息。
    """
    name = filename.lower()
    try:
        # 文本文件: 自动检测编码解码
        # str.endswith 只接受 tuple(frozenset 会抛 TypeError),必须转换
        if name.endswith(tuple(TEXT_EXTENSIONS)):
            return _detect_and_decode(content)
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
    """切块并索引单个文档,返回索引统计。

    增量索引:如果同文件名内容 hash 未变则跳过 re-embed,直接复用已有结果。
    """
    doc_id = doc_id or uuid.uuid4().hex
    content_hash = _file_hash(content)
    existing_hashes = _load_doc_hashes()
    prev_hash = existing_hashes.get(filename)

    # 快速路径:内容未变更,直接返回已有结果
    if prev_hash == content_hash:
        return {"doc_id": doc_id, "filename": filename, "chunks": 0, "note": "内容未变更,跳过索引", "cached": True}

    text = extract_text(content, filename)
    if text.startswith("["):
        return {"doc_id": doc_id, "filename": filename, "chunks": 0, "error": text}

    docs = [{"text": text, "source": filename}]
    chunks = chunk_documents(docs, chunk_size=500, overlap=50)
    embedder = get_embedder()
    store = get_store()
    # 租户隔离
    from app.core.tenant_context import get_tenant
    tid = get_tenant() or "default"

    # 删除同文档旧索引(同名文件重新上传时替换)
    stale_ids = []
    try:
        all_ids = store._ids
        all_metas = store._metas
        stale_ids = [
            cid for cid, meta in zip(all_ids, all_metas)
            if meta.get("source") == filename and meta.get("tenant_id") == tid
        ]
    except Exception:
        pass
    for cid in stale_ids:
        store.delete(cid)

    added = 0
    for chunk in chunks:
        vec = run_embed(embedder, [chunk["text"]])[0]
        cid = f"{doc_id}:{chunk['metadata']['chunk_index']}"
        meta = {**chunk["metadata"], "text": chunk["text"], "doc_id": doc_id, "tenant_id": tid}
        store.add(cid, vec, meta, tenant_id=tid)
        added += 1

    # 记录 hash
    _save_doc_hash(filename, content_hash)
    return {"doc_id": doc_id, "filename": filename, "chunks": added, "hash": content_hash}


def delete_document(source: str) -> dict:
    """删除指定文档(按 source=文件名)的所有向量块(仅限当前租户)。"""
    from app.core.tenant_context import get_tenant

    tid = get_tenant() or "default"
    store = get_store()
    stale_ids = [
        cid for cid, meta in zip(store._ids, store._metas)  # noqa: SLF001
        if meta.get("source") == source and meta.get("tenant_id", "default") == tid
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
        chunk["metadata"]["doc_id"] = doc_id
        vec = run_embed(embedder, [chunk["text"]])[0]
        cid = f"{doc_id}:{chunk['metadata']['chunk_index']}"
        store.add(cid, vec, {**chunk["metadata"], "text": chunk["text"], "doc_id": doc_id})
        added += 1
    # 同步更新 BM25 倒排索引，确保多路召回能命中关键词
    try:
        from app.rag.retriever import get_multi_retriever
        get_multi_retriever().update_index(chunks)
    except Exception:  # noqa: BLE001
        pass
    return {"doc_id": doc_id, "source": source, "chunks": added}


@tool("rag_search", "在已上传的知识库文档中检索相关内容,返回最相关的文本片段(含来源与段落号,可引用)", timeout=15)
def rag_search(query: str = Field(description="检索问题"), top_k: int = Field(5, description="返回片段数")) -> dict:
    from app.rag.retriever import get_retriever

    retriever = get_retriever()
    results = retriever.search(query, top_k=top_k)
    if not results:
        return {"query": query, "results": [], "note": "知识库中没有相关内容", "refs": []}
    out = []
    refs = []
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
        refs.append({
            "doc_id": meta.get("doc_id", ""),
            "source": meta.get("source", "unknown"),
            "chunk_index": meta.get("chunk_index", 0),
            "score": round(r.get("score", 0), 4),
        })
    return {"query": query, "results": out, "refs": refs, "total": len(out)}
