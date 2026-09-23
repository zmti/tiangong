"""智能体规则 API:/v1/rules/*

命名、持久化的规则集(rule set)。每个规则集含多条规则,单条可独立开关:
- 软规则 instruction: 自定义指令,拼进 Agent 系统提示(注入由 /v1/chat 的 _build_agent 完成)
- 硬规则 tool_block / term_block / path_block: 工具拦截 + 输入/输出护栏(guardrails.py)

数据模型:单表 rules,以 set_id 分组,冗余存 set_name 便于直接列表。
无启用规则时,/active 全空,后端行为与改动前一致(回归红线)。
"""
from __future__ import annotations

import time
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.storage_backend import get_storage_backend

router = APIRouter(prefix="/v1", tags=["rules"])

# ---- 存储 ----
_db = get_storage_backend()
_db.init_db({
    "rules": [
        {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
        {"name": "set_id", "type": "TEXT", "constraints": "NOT NULL", "index": True},
        {"name": "set_name", "type": "TEXT", "constraints": "NOT NULL"},  # 冗余;改名时 UPDATE
        {"name": "name", "type": "TEXT", "constraints": "NOT NULL"},
        # 类型: instruction / tool_block / term_block / path_block
        {"name": "type", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'instruction'"},
        # instruction: 指令正文;tool_block: 逗号分隔工具名;term/path_block: 逗号或换行分隔
        {"name": "content", "type": "TEXT", "constraints": "NOT NULL DEFAULT ''"},
        {"name": "enabled", "type": "INTEGER", "constraints": "NOT NULL DEFAULT 1"},
        # 硬规则命中动作: block(拦截)/ warn(记录放行)。instruction 忽略
        {"name": "action", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'block'"},
        {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
        {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
    ],
})

_VALID_TYPES = ("instruction", "tool_block", "term_block", "path_block")
_VALID_ACTIONS = ("block", "warn")


# ---- 模型 ----
class SetCreate(BaseModel):
    name: str = Field(..., min_length=1, description="规则集名称")


class SetRename(BaseModel):
    name: str = Field(..., min_length=1, description="新名称")


class RuleCreate(BaseModel):
    set_id: str = Field(..., description="所属规则集 ID")
    set_name: str = Field(..., description="所属规则集名称(冗余)")
    name: str = Field(..., min_length=1, description="规则名")
    type: str = Field("instruction", description="instruction|tool_block|term_block|path_block")
    content: str = Field("", description="规则内容")
    enabled: bool = Field(True, description="是否启用")
    action: str = Field("block", description="硬规则命中动作 block|warn")


class RuleUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    content: Optional[str] = None
    enabled: Optional[bool] = None
    action: Optional[str] = None


def _new_rule_id() -> str:
    return f"rule_{uuid.uuid4().hex[:16]}"


def _new_set_id() -> str:
    return f"rs_{uuid.uuid4().hex[:16]}"


def _validate_rule_fields(type: Optional[str], action: Optional[str]) -> None:
    if type is not None and type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail=f"非法规则类型: {type}")
    if action is not None and action not in _VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"非法动作: {action}")


def _row_to_view(r: dict) -> dict:
    return {
        "id": r["id"],
        "set_id": r["set_id"],
        "set_name": r.get("set_name", ""),
        "name": r["name"],
        "type": r.get("type", "instruction"),
        "content": r.get("content", ""),
        "enabled": bool(r.get("enabled", 1)),
        "action": r.get("action", "block"),
    }


def _split_content(content: str) -> list[str]:
    """把 content 按逗号/换行/中文顿号/分号切分为条目,去空去重(保序)。

    注意: 不切斜杠 `/` 与反斜杠 `\\`——路径类内容(如 C:\\secrets、/etc/passwd)
    若按 `/` 切分会被拆碎。路径/工具名/词条统一按 逗号(中/英)、顿号、分号、换行 分隔。
    """
    import re
    parts = re.split(r"[\n,，、;；]+", content or "")
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _select_enabled(rule_ids: list[str] | None = None) -> list[dict]:
    """取启用中的规则行;rule_ids 非空时仅过滤这些 ID。"""
    rows = _db.select("rules", "enabled = ?", (1,))
    if rule_ids:
        keep = set(rule_ids)
        rows = [r for r in rows if r["id"] in keep]
    return rows


# ---- 注入/护栏共用的分桶源 ----
def get_active_rules(rule_ids: list[str] | None = None) -> dict:
    """按 type + action 分桶,返回当前启用的规则。

    - instructions: 启用的 instruction 规则 [{name, content}]
    - blocked_tools / warn_tools: tool_block(content 逗号分隔工具名)
    - blocked_terms / warn_terms: term_block(逗号/换行分隔)
    - blocked_paths / warn_paths: path_block
    各桶 content 切分合并去重保序。action=warn 的进 warn_* 桶,否则 blocked_*。
    """
    buckets = {
        "instructions": [],
        "blocked_tools": [], "warn_tools": [],
        "blocked_terms": [], "warn_terms": [],
        "blocked_paths": [], "warn_paths": [],
    }
    seen = {"tools": set(), "terms": set(), "paths": set()}
    for r in _select_enabled(rule_ids):
        t = r.get("type", "instruction")
        warn = (r.get("action", "block") == "warn")
        if t == "instruction":
            if (r.get("content") or "").strip():
                buckets["instructions"].append({"name": r["name"], "content": r["content"]})
            continue
        items = _split_content(r.get("content", ""))
        key = "tools" if t == "tool_block" else ("terms" if t == "term_block" else "paths")
        target_blocked = {"tools": "blocked_tools", "terms": "blocked_terms", "paths": "blocked_paths"}[key]
        target_warn = {"tools": "warn_tools", "terms": "warn_terms", "paths": "warn_paths"}[key]
        target = buckets[target_warn] if warn else buckets[target_blocked]
        for it in items:
            if it not in seen[key]:
                seen[key].add(it)
                target.append(it)
    return buckets


def get_active_instructions(rule_ids: list[str] | None = None) -> str:
    """把启用的 instruction 规则拼成系统提示块;无则返回空串。"""
    rows = [r for r in _select_enabled(rule_ids) if r.get("type", "instruction") == "instruction"]
    lines = [f"[规则 {r['name']}]\n{r['content']}" for r in rows if (r.get("content") or "").strip()]
    if not lines:
        return ""
    return "以下为用户自定义指令,请严格遵守:\n\n" + "\n\n".join(lines)


# ---- 端点 ----
@router.get("/rules")
async def list_rule_sets() -> dict:
    """按 set_id 分组返回全部规则集。跳过建集时的占位行(name='(empty)')。"""
    rows = _db.select("rules", "", order_by="created_at ASC")
    by_set: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        sid = r["set_id"]
        if r.get("name") == "(empty)":
            # 占位行: 仍确保该 set 出现在分组里,但不计入规则
            if sid not in by_set:
                by_set[sid] = {"set_id": sid, "set_name": r.get("set_name", ""), "rules": []}
                order.append(sid)
            continue
        if sid not in by_set:
            by_set[sid] = {"set_id": sid, "set_name": r.get("set_name", ""), "rules": []}
            order.append(sid)
        by_set[sid]["rules"].append(_row_to_view(r))
    sets = [by_set[sid] for sid in order]
    for s in sets:
        s["rule_count"] = len(s["rules"])
        s["enabled_count"] = sum(1 for r in s["rules"] if r["enabled"])
    return {"sets": sets, "total": len(sets)}


@router.post("/rules/sets")
async def create_rule_set(req: SetCreate) -> dict:
    """创建空规则集。"""
    set_id = _new_set_id()
    name = req.name.strip()
    now = time.time()
    _db.insert("rules", {
        "id": _new_rule_id(),
        "set_id": set_id,
        "set_name": name,
        "name": "(empty)",
        "type": "instruction",
        "content": "",
        "enabled": 0,  # 占位行:仅用于记录 set,不启用
        "action": "block",
        "created_at": now,
        "updated_at": now,
    })
    return {"set_id": set_id, "set_name": name}


@router.post("/rules/sets/{set_id}/rename")
async def rename_rule_set(set_id: str, req: SetRename) -> dict:
    """改名:更新该集所有行的 set_name。"""
    name = req.name.strip()
    _db.update("rules", {"set_name": name}, "set_id = ?", (set_id,))
    return {"ok": True, "set_id": set_id, "set_name": name}


@router.delete("/rules/sets/{set_id}")
async def delete_rule_set(set_id: str) -> dict:
    """删除规则集及其全部规则。"""
    _db.delete("rules", "set_id = ?", (set_id,))
    return {"ok": True, "set_id": set_id}


@router.post("/rules/sets/{set_id}")
async def add_rule(set_id: str, req: RuleCreate) -> dict:
    """在指定集内新增规则。"""
    _validate_rule_fields(req.type, req.action)
    if not _db.exists("rules", "set_id = ?", (set_id,)):
        raise HTTPException(status_code=404, detail=f"规则集不存在: {set_id}")
    rule_id = _new_rule_id()
    now = time.time()
    _db.insert("rules", {
        "id": rule_id,
        "set_id": set_id,
        "set_name": req.set_name,
        "name": req.name.strip(),
        "type": req.type,
        "content": req.content,
        "enabled": 1 if req.enabled else 0,
        "action": req.action,
        "created_at": now,
        "updated_at": now,
    })
    return _row_to_view(_db.select("rules", "id = ?", (rule_id,), limit=1)[0])


@router.patch("/rules/{rule_id}")
async def update_rule(rule_id: str, req: RuleUpdate) -> dict:
    """更新/开关单条规则(全字段可选)。"""
    _validate_rule_fields(req.type, req.action)
    existing = _db.select("rules", "id = ?", (rule_id,), limit=1)
    if not existing:
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    patch: dict = {"updated_at": time.time()}
    if req.name is not None:
        patch["name"] = req.name.strip()
    if req.type is not None:
        patch["type"] = req.type
    if req.content is not None:
        patch["content"] = req.content
    if req.enabled is not None:
        patch["enabled"] = 1 if req.enabled else 0
    if req.action is not None:
        patch["action"] = req.action
    _db.update("rules", patch, "id = ?", (rule_id,))
    return _row_to_view(_db.select("rules", "id = ?", (rule_id,), limit=1)[0])


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str) -> dict:
    """删除单条规则。"""
    if not _db.exists("rules", "id = ?", (rule_id,)):
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    _db.delete("rules", "id = ?", (rule_id,))
    return {"ok": True, "id": rule_id}


@router.get("/rules/active")
async def active_rules() -> dict:
    """当前启用的规则分桶(注入 & 护栏共用数据源)。"""
    return get_active_rules(None)
