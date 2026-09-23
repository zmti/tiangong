"""本地文件工具:按文件名/内容搜索目录、列出目录、读取本地文档(含 Office)、写入文件。"""
from __future__ import annotations

import json
import os
import re

from pydantic import Field

from app.config import settings
from app.tools.project_context import resolve_cwd
from app.tools.registry import tool


def _safe_root(root: str) -> str:
    """校验并解析搜索根目录,防越界。

    空或 "." = 当前项目工作目录(resolve_cwd: 前端选中的项目 > 后端 cwd);
    显式 "~" = 用户主目录(保留浏览整机的用法,但不再作为默认值)。
    """
    r = (root or "").strip()
    if not r or r == ".":
        return resolve_cwd()
    if r == "~":
        return os.path.expanduser("~")
    # 相对子目录(如 "app"、"src")必须相对**选中项目**解析,而不是后端进程 cwd,
    # 否则选了 MES-System 却去 E:\dev\TianGong\app 找,工具"没按选择项目来"。
    base = resolve_cwd()
    root = (
        os.path.abspath(os.path.expanduser(r))
        if os.path.isabs(r)
        else os.path.abspath(os.path.join(base, os.path.expanduser(r)))
    )
    if not os.path.isdir(root):
        raise ValueError(f"目录不存在: {root}")
    return root


def _allowed_write_roots() -> list[str]:
    """write_file 可写根目录:白名单配置 + 数据目录 + 当前工作目录。"""
    roots: list[str] = []
    for d in (settings.allowed_write_dirs or "").split(","):
        d = d.strip()
        if d:
            roots.append(os.path.abspath(os.path.expanduser(d)))
    roots.append(settings.resolved_data_dir)
    try:
        roots.append(os.path.abspath(resolve_cwd()))
    except Exception:  # noqa: BLE001
        pass
    return list(dict.fromkeys(roots))


def _resolve_write_path(path: str) -> str:
    """解析写入路径并校验在可写白名单内(防越界写系统文件)。

    两处都必须 abspath 规范化:白名单比较是字符串前缀匹配,
    resolve_cwd() 若带回正斜杠/尾斜杠(前端 JS 传值),拼接结果会与
    规范化后的 root 前缀对不上,把"项目目录内的相对路径"误判为越界。
    """
    if not os.path.isabs(path):
        full = os.path.abspath(os.path.join(resolve_cwd(), path))
    else:
        full = os.path.abspath(os.path.expanduser(path))
    for root in _allowed_write_roots():
        rp = os.path.abspath(root)
        if full == rp or full.startswith(rp + os.sep):
            return full
    raise ValueError(
        f"路径不在允许写入目录内: {full}。可写目录: {', '.join(_allowed_write_roots())}"
    )


@tool(
    "search_files",
    "按文件名(支持 * 通配)或文件内容搜索文件,返回匹配文件路径列表。默认在当前项目工作目录内搜索(前端选择的项目);需要全盘找用户文件时可显式传 root='~'",
    timeout=30,
)
def search_files(
    query: str = Field(description="搜索关键词(文件名通配如 *.pdf,或内容关键词)"),
    root: str = Field("", description="搜索根目录,留空=当前项目工作目录(默认,推荐);传 '~' 表示用户主目录"),
    search_content: bool = Field(False, description="True 时在文本文件内容中搜索,否则仅按文件名匹配"),
    max_results: int = Field(30, description="最大返回条数"),
) -> str:
    root = _safe_root(root)
    q = query.strip()
    if not q:
        return {"query": query, "results": [], "error": "搜索关键词为空"}
    results: list[dict] = []
    has_wildcard = any(ch in q for ch in "*?")
    if has_wildcard:
        pattern = re.compile("^" + re.escape(q).replace(r"\*", ".*").replace(r"\?", ".") + "$", re.IGNORECASE)
    else:
        pattern = re.compile(re.escape(q), re.IGNORECASE)

    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过常见大目录/隐藏目录
        dirnames[:] = [
            d for d in dirnames
            if d not in (".git", "node_modules", "AppData", ".cache", ".venv", "__pycache__", "site-packages")
        ]
        for fn in filenames:
            matched = bool(pattern.search(fn))
            if not matched and search_content:
                try:
                    fp = os.path.join(dirpath, fn)
                    if os.path.getsize(fp) > 1_000_000:
                        continue  # 跳过 >1MB 的文本
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        head = f.read(5000)
                    matched = bool(pattern.search(head))
                except Exception:  # noqa: BLE001
                    continue
            if matched:
                results.append({"path": os.path.join(dirpath, fn)})
                if len(results) >= max_results:
                    return {"query": query, "results": results, "truncated": True}
    return {"query": query, "results": results}


@tool(
    "list_directory",
    "列出目录的内容(文件和子目录)。默认列出当前项目工作目录(前端选择的项目),查看项目结构直接调用即可;需要浏览用户电脑其他位置时显式传 path",
    timeout=20,
)
def list_directory(
    path: str = Field("", description="要列出的目录路径,留空=当前项目工作目录(默认,推荐);传 '~' 表示用户主目录"),
    max_items: int = Field(100, description="最大返回条数"),
) -> str:
    root = _safe_root(path)
    entries: list[dict] = []
    try:
        names = sorted(os.listdir(root))
    except OSError as e:
        return {"path": root, "error": str(e)}
    for name in names:
        full = os.path.join(root, name)
        try:
            is_dir = os.path.isdir(full)
            size = 0 if is_dir else os.path.getsize(full)
            entries.append({"name": name, "type": "dir" if is_dir else "file", "size": size})
        except OSError:  # noqa: BLE001
            continue
        if len(entries) >= max_items:
            break
    return {"path": root, "entries": entries}


@tool(
    "read_document",
    "读取本地文件内容(支持 txt/md/pdf/docx/xlsx/xls/pptx/csv/json 等),返回提取的文本。适合直接查看用户电脑上的文档",
    timeout=30,
)
def read_document(
    path: str = Field(description="要读取的本地文件路径"),
    max_chars: int = Field(15000, description="最多返回的字符数"),
) -> str:
    from app.tools.project_context import resolve_cwd
    if not os.path.isabs(path):
        path = os.path.join(resolve_cwd(), path)
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        return {"path": path, "error": f"文件不存在: {path}"}
    try:
        from app.rag.documents import extract_text

        with open(path, "rb") as f:
            data = f.read()
        text = extract_text(data, os.path.basename(path))
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n...[内容过长,已截断,全文共 {len(text)} 字符]"
        return {"path": path, "content": text}
    except Exception as e:  # noqa: BLE001
        return {"path": path, "error": f"读取失败: {e}"}


@tool(
    "write_file",
    "创建或修改本地文件(UTF-8 文本)。仅允许写入配置白名单目录内,用于生成脚本、配置文件、文档、修改项目代码。"
    "覆盖已有文件前会自动备份历史版本,可用 file_history/file_restore/file_undo 查看、回滚或撤销。返回写入结果",
    timeout=20,
)
def write_file(
    path: str = Field(description="要写入的文件完整路径(必须在允许写入目录内)"),
    content: str = Field(description="文件完整内容(UTF-8)"),
    overwrite: bool = Field(True, description="文件已存在时是否允许覆盖,False 则已存在时失败"),
) -> str:
    try:
        full = _resolve_write_path(path)
    except ValueError as e:
        return {"path": path, "error": str(e), "allowed": False}

    existed = os.path.exists(full)
    if os.path.isdir(full):
        return {"path": full, "error": "目标路径是一个目录"}
    if existed and not overwrite:
        return {"path": full, "error": "文件已存在(overwrite=False),如需覆盖请置 overwrite=True"}

    # 写入前备份旧版本(支持回滚/撤销)
    version_id = None
    from app.core.file_versions import file_version_store

    try:
        if existed:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                old = f.read()
            version_id = file_version_store.save_version(full, old, "overwrite")
        else:
            version_id = file_version_store.save_version(full, "", "create")
    except OSError:
        version_id = None

    try:
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return {"path": full, "error": f"写入失败: {e}", "allowed": True}

    return {
        "path": full,
        "status": "ok",
        "action": "overwritten" if existed else "created",
        "bytes": len(content.encode("utf-8")),
        "allowed": True,
        "version_id": version_id,
    }


@tool(
    "edit_file",
    "对本地文本文件做精确的局部修改(查找替换),不重写整个文件。"
    "edits 是 JSON 数组字符串,每项 {old: 要查找的原文, new: 替换成的新文本, replace_all: 是否替换所有匹配(默认仅替换第一处)}。"
    "多个编辑一次性原子应用:任一 old 匹配失败或匹配到多处(且未开 replace_all)则整个操作不落盘,并返回错误原因,避免改坏代码。"
    "修改前自动备份历史版本,可用 file_history/file_restore/file_undo 查看或回滚。"
    "适合改一行 bug、加一个函数、替换配置等局部修改;大段重写请用 write_file",
    timeout=30,
)
def edit_file(
    path: str = Field(description="要编辑的文件完整路径(必须在允许写入目录内)"),
    edits: str = Field(description='JSON 数组字符串,每项 {"old": 原文, "new": 新文本, "replace_all": false}'),
    dry_run: bool = Field(False, description="True 时仅预览差异不写入,返回 diff 与匹配情况"),
) -> str:
    try:
        full = _resolve_write_path(path)
    except ValueError as e:
        return {"path": path, "error": str(e), "allowed": False}
    if os.path.isdir(full):
        return {"path": full, "error": "目标路径是一个目录"}
    if not os.path.isfile(full):
        return {"path": full, "error": f"文件不存在: {full}", "allowed": True}

    try:
        ops = json.loads(edits) if isinstance(edits, str) else edits
    except json.JSONDecodeError as e:
        return {"path": full, "error": f"edits 不是合法 JSON 数组: {e}", "allowed": True}
    if not isinstance(ops, list) or not ops:
        return {"path": full, "error": "edits 必须是非空 JSON 数组", "allowed": True}
    clean_ops: list[dict] = []
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            return {"path": full, "error": f"第 {i + 1} 个编辑项不是 JSON 对象", "allowed": True}
        old = op.get("old")
        if not isinstance(old, str) or not old:
            return {"path": full, "error": f"第 {i + 1} 个编辑项缺少非空 old 文本", "allowed": True}
        clean_ops.append({
            "old": old,
            "new": op.get("new", "") if isinstance(op.get("new", ""), str) else "",
            "replace_all": bool(op.get("replace_all", False)),
        })

    # 二进制读取保留原始行尾,避免 \n/\r\n 被意外改写
    try:
        with open(full, "rb") as f:
            raw = f.read()
    except OSError as e:
        return {"path": full, "error": f"读取失败: {e}", "allowed": True}
    content = None
    encoding = None
    for enc in ("utf-8", "gbk"):
        try:
            content = raw.decode(enc)
            encoding = enc
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if content is None:
        return {"path": full, "error": "文件编码无法识别(仅支持 UTF-8/GBK 文本)", "allowed": True}

    new_content = content
    applied: list[dict] = []
    for i, op in enumerate(clean_ops):
        count = new_content.count(op["old"])
        if count == 0:
            return {
                "path": full,
                "error": f"第 {i + 1} 个编辑项匹配失败: 未找到 old 文本",
                "allowed": True,
                "applied": applied,
            }
        if count > 1 and not op["replace_all"]:
            return {
                "path": full,
                "error": f"第 {i + 1} 个编辑项匹配到 {count} 处,请提供更精确的上下文或设置 replace_all=true",
                "allowed": True,
                "applied": applied,
            }
        if op["replace_all"]:
            new_content = new_content.replace(op["old"], op["new"])
            replaced = count
        else:
            new_content = new_content.replace(op["old"], op["new"], 1)
            replaced = 1
        applied.append({"index": i + 1, "replaced": replaced, "matched": count})

    if new_content == content:
        return {"path": full, "error": "没有任何实际变更(替换前后内容相同)", "allowed": True, "applied": applied}

    if dry_run:
        import difflib

        diff = "".join(difflib.unified_diff(
            content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile="before", tofile="after",
        ))
        return {"path": full, "status": "preview", "applied": applied, "diff": diff[:12000], "allowed": True}

    # 写入前备份旧版本(支持 file_history/file_restore/file_undo 回滚)
    version_id = None
    from app.core.file_versions import file_version_store

    try:
        version_id = file_version_store.save_version(full, content, "overwrite")
    except OSError:
        version_id = None

    try:
        with open(full, "wb") as f:
            f.write(new_content.encode(encoding))
    except OSError as e:
        return {"path": full, "error": f"写入失败: {e}", "allowed": True}

    return {
        "path": full,
        "status": "ok",
        "action": "edited",
        "edits_applied": len(applied),
        "applied": applied,
        "bytes_before": len(content.encode("utf-8")),
        "bytes_after": len(new_content.encode("utf-8")),
        "version_id": version_id,
        "allowed": True,
    }


@tool(
    "file_history",
    "查看文件的历史版本列表(每次 write_file 覆盖/创建前自动备份)。返回版本号、时间、大小。用于了解文件变更轨迹",
    timeout=10,
)
def file_history(
    path: str = Field(description="要查询历史版本的本地文件路径"),
) -> str:
    from app.tools.project_context import resolve_cwd
    full = (
        os.path.abspath(os.path.expanduser(path))
        if os.path.isabs(path)
        else os.path.abspath(os.path.join(resolve_cwd(), path))
    )
    from app.core.file_versions import file_version_store

    versions = file_version_store.list_versions(full)
    return {"path": full, "versions": versions}


@tool(
    "file_restore",
    "把文件恢复到指定历史版本。version_id 来自 file_history 返回的 id。恢复前会自动备份当前版本。"
    "用于撤回错误的写入、找回被覆盖的旧内容",
    timeout=15,
)
def file_restore(
    path: str = Field(description="要恢复的文件路径"),
    version_id: int = Field(description="要恢复到的版本 id(来自 file_history)"),
) -> str:
    try:
        full = _resolve_write_path(path)
    except ValueError as e:
        return {"path": path, "error": str(e), "allowed": False}
    from app.core.file_versions import file_version_store

    ver = file_version_store.get_version(version_id)
    if ver is None:
        return {"path": full, "error": f"版本不存在: {version_id}"}
    if ver["path"] != full:
        return {"path": full, "error": f"版本 {version_id} 不属于该文件"}
    # 先备份当前内容,便于再次回滚
    if os.path.isfile(full):
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                cur = f.read()
            file_version_store.save_version(full, cur, "overwrite")
        except OSError:
            pass
    try:
        if ver["action"] == "create" and not ver["content"]:
            # 恢复到"文件创建前"状态 = 删除文件
            if os.path.isfile(full):
                os.remove(full)
            return {"path": full, "restored_version": ver["id"], "action": "removed"}
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(ver["content"])
    except OSError as e:
        return {"path": full, "error": f"恢复失败: {e}"}
    return {"path": full, "restored_version": ver["id"], "bytes": len(ver["content"].encode("utf-8"))}


@tool(
    "file_undo",
    "撤销对文件的上一次写入,恢复到上一个版本(无历史版本则无法撤销;若文件刚被创建,则删除该文件)。用于快速回退",
    timeout=15,
)
def file_undo(
    path: str = Field(description="要撤销写入的文件路径"),
) -> str:
    try:
        full = _resolve_write_path(path)
    except ValueError as e:
        return {"path": path, "error": str(e), "allowed": False}
    from app.core.file_versions import file_version_store

    ver = file_version_store.latest_version(full)
    if ver is None:
        return {"path": full, "error": "没有可撤销的历史版本"}
    try:
        if ver["action"] == "create" and not ver["content"]:
            # 文件是在最后一次写入时新建的,撤销 = 删除该文件
            if os.path.isfile(full):
                os.remove(full)
            file_version_store.delete_version(ver["id"])
            return {"path": full, "action": "removed", "restored_version": ver["id"]}
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(ver["content"])
        file_version_store.delete_version(ver["id"])
    except OSError as e:
        return {"path": full, "error": f"撤销失败: {e}"}
    return {"path": full, "action": "restored", "restored_version": ver["id"], "bytes": len(ver["content"].encode("utf-8"))}
