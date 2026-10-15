"""会话附件存储:为当前会话暂存上传的本地文件,供聊天时注入上下文。

- 以 session_id 为键,文件保存在 data/attachments/<session_id>/ 下。
- 每条附件记录 {id, name, size, mime, created_at}。
- 索引持久化:每会话 data/attachments/<session_id>/meta.json 为权威,内存 dict
  仅作缓存;后端重启后自动从磁盘恢复,会话历史附件不再"消失"。
- 纯文本/文档注入:聊天时把附件内容拼进用户消息;超大文件截断。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid

from app.config import settings

# 单附件最大字节(1MB);注入时再按字符截断
MAX_ATTACHMENT_BYTES = 1 * 1024 * 1024
# 单个会话最多附件数
MAX_ATTACHMENTS_PER_SESSION = 8
# 注入上下文的单附件最大字符数
MAX_INJECT_CHARS = 30000
# 附件总注入上限
MAX_TOTAL_INJECT_CHARS = 60000

_LOCK = threading.RLock()
_index: dict[str, list[dict]] = {}  # session_id -> [attachment dict]


class AttachmentStore:
    def _dir(self, session_id: str) -> str:
        base = os.path.join(settings.resolved_data_dir, "attachments")
        d = os.path.join(base, session_id)
        os.makedirs(d, exist_ok=True)
        return d

    def _meta_path(self, session_id: str) -> str:
        return os.path.join(self._dir(session_id), "meta.json")

    def _meta_from_disk(self, session_id: str) -> list[dict]:
        """从磁盘恢复索引:优先读 meta.json,缺失时按目录内 att_id_ 前缀文件重建。"""
        path = self._meta_path(session_id)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    items = json.load(f)
                if isinstance(items, list):
                    return [it for it in items if isinstance(it, dict) and it.get("id")]
            except (OSError, json.JSONDecodeError):
                pass
        # meta.json 缺失/损坏 → 扫描实际文件重建(att_id_ 前缀,跳过 meta.json 本身)
        base = self._dir(session_id)
        items: list[dict] = []
        hexdigits = set("0123456789abcdef")
        if os.path.isdir(base):
            for fn in os.listdir(base):
                if fn == "meta.json" or not os.path.isfile(os.path.join(base, fn)):
                    continue
                # 文件名形如 <12位hex att_id>_<原文件名>;att_id 为 uuid hex 前 12 位
                prefix, sep, rest = fn.partition("_")
                if not sep or len(prefix) != 12 or any(c not in hexdigits for c in prefix):
                    continue
                p = os.path.join(base, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                items.append({
                    "id": prefix,
                    "name": rest or fn,
                    "size": st.st_size,
                    "mime": "application/octet-stream",
                    "created_at": st.st_mtime,
                })
        return items

    def _read_meta(self, session_id: str) -> list[dict]:
        if session_id not in _index:
            # 懒恢复:内存无缓存时,从磁盘权威层重建(重启后仍能列到历史附件)
            _index[session_id] = self._meta_from_disk(session_id)
        return _index[session_id]

    def _write_meta(self, session_id: str, items: list[dict]) -> None:
        _index[session_id] = items
        try:
            path = self._meta_path(session_id)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False)
        except OSError:
            # 磁盘写失败不阻断内存操作(最坏退化为旧行为:重启丢失)
            pass

    def add(self, session_id: str, filename: str, content: bytes, mime: str = "") -> dict:
        """保存一个附件,返回记录 dict。超限返回 error dict。"""
        if not filename or not content:
            return {"error": "文件为空"}
        if len(content) > MAX_ATTACHMENT_BYTES:
            return {"error": f"附件超过 {MAX_ATTACHMENT_BYTES // 1024 // 1024}MB 限制"}
        with _LOCK:
            items = self._read_meta(session_id)
            if len(items) >= MAX_ATTACHMENTS_PER_SESSION:
                return {"error": f"单个会话最多 {MAX_ATTACHMENTS_PER_SESSION} 个附件,请先移除旧的"}
            att_id = uuid.uuid4().hex[:12]
            safe = os.path.basename(filename)[:120]
            d = self._dir(session_id)
            path = os.path.join(d, f"{att_id}_{safe}")
            with open(path, "wb") as f:
                f.write(content)
            rec = {
                "id": att_id,
                "name": safe,
                "size": len(content),
                "mime": mime or "application/octet-stream",
                "created_at": time.time(),
            }
            items.append(rec)
            self._write_meta(session_id, items)
            return rec

    def list(self, session_id: str) -> list[dict]:
        with _LOCK:
            return list(self._read_meta(session_id))

    def get_path(self, session_id: str, att_id: str) -> str | None:
        with _LOCK:
            rec = next((a for a in self._read_meta(session_id) if a["id"] == att_id), None)
            if rec is None:
                return None
            safe = os.path.basename(rec["name"])
            return os.path.join(self._dir(session_id), f"{att_id}_{safe}")

    def remove(self, session_id: str, att_id: str) -> bool:
        with _LOCK:
            items = self._read_meta(session_id)
            rec = next((a for a in items if a["id"] == att_id), None)
            if rec is None:
                return False
            safe = os.path.basename(rec["name"])
            path = os.path.join(self._dir(session_id), f"{att_id}_{safe}")
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
            self._write_meta(session_id, [a for a in items if a["id"] != att_id])
            return True

    def clear(self, session_id: str) -> int:
        with _LOCK:
            items = self._read_meta(session_id)
            n = len(items)
            d = self._dir(session_id)
            if os.path.isdir(d):
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass
            self._write_meta(session_id, [])
            return n

    def read_content(self, session_id: str, att_id: str) -> tuple[str, str] | None:
        """读取附件文本内容,返回 (文件名, 文本)。二进制/读取失败返回 None。"""
        path = self.get_path(session_id, att_id)
        if path is None:
            return None
        rec = next((a for a in self.list(session_id) if a["id"] == att_id), None)
        name = rec["name"] if rec else "附件"
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        # 简单二进制检测:含 NUL 或大量不可解码字节视为二进制,跳过注入
        try:
            text = data.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            return None
        return name, text

    def get_bytes(self, session_id: str, att_id: str) -> tuple[str, str, bytes] | None:
        """读取附件原始二进制,返回 (文件名, mime, bytes)。不存在返回 None。"""
        path = self.get_path(session_id, att_id)
        if path is None:
            return None
        rec = next((a for a in self.list(session_id) if a["id"] == att_id), None)
        if rec is None:
            return None
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        return rec["name"], rec.get("mime", "") or "application/octet-stream", data

    def inject_text(self, session_id: str, ids: list[str]) -> str:
        """把附件内容拼成一块注入文本,置于用户消息之前。"""
        if not ids:
            return ""
        total = 0
        blocks: list[str] = []
        for att_id in ids:
            got = self.read_content(session_id, att_id)
            if got is None:
                continue
            name, text = got
            body = text[:MAX_INJECT_CHARS]
            total += len(body)
            if total > MAX_TOTAL_INJECT_CHARS:
                break
            blocks.append(f"[附件: {name}]\n```\n{body}\n```")
        if not blocks:
            return ""
        return "\n\n".join(blocks) + "\n\n"


attachment_store = AttachmentStore()
