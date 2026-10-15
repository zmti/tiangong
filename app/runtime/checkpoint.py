"""Checkpoint 管理器:把图运行状态快照持久化到存储后端,支持暂停/恢复/后台运行。

表 checkpoints 以 run_id 为主键,每次节点边界保存最新快照(upsert)。
- 进程重启后按 run_id 恢复,从头跑还是断点续跑由调用方决定。
- 保留 version 字段,未来可做时间旅行/分支回滚。
- 支持 SQLite / Redis / PostgreSQL 后端切换。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

from app.runtime.state import AgentState
from app.core.storage_backend import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)

# 使用 asyncio.Lock 替代 threading.Lock，避免阻塞事件循环
# SQLite backend 内部已使用 threading.RLock，此处只需保护 checkpoint 操作本身的原子性
_DB_LOCK = asyncio.Lock()


class CheckpointManager:
    # 终态集合: 这些状态的 run 已不再推进, 可被 TTL 清理。
    # 注意必须含 'interrupted' —— 启动清扫(_sweep_orphan_runs)会把上一进程崩溃
    # 遗留的 running run 改写为 interrupted, 若清理集合漏掉它, 这些孤儿 checkpoint
    # 永远留在主表/版本表, 只增不减(本次会话卡死/膨胀的根因之一)。
    _TERMINAL_STATUSES = ("done", "failed", "cancelled", "interrupted")

    def __init__(self, db_path: Optional[str] = None, backend: Optional[StorageBackend] = None):
        """初始化 CheckpointManager
        
        Args:
            db_path: 数据库路径（SQLite 模式）
            backend: 存储后端实例，优先使用
        """
        if backend:
            self._backend = backend
            self.db_path = None
        else:
            if not db_path:
                from app.config import settings
                db_path = os.path.join(settings.resolved_data_dir, "checkpoints.db")
            self.db_path = db_path
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
            self._backend = get_storage_backend(db_path=db_path)
        
        # 初始化数据库需要在事件循环中运行
        import asyncio
        try:
            asyncio.get_running_loop()
            # 有运行中的事件循环，使用 create_task
            asyncio.create_task(self._init_db_async())
        except RuntimeError:
            # 没有运行中的事件循环，同步初始化
            self._init_db_sync()

    def _init_db_sync(self) -> None:
        """同步初始化数据库(用于启动阶段)。"""
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._init_db_async())
        finally:
            loop.close()

    async def _init_db_async(self) -> None:
        async with _DB_LOCK:
            schema = {
                "checkpoints": [
                    {"name": "run_id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                    {"name": "node", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "status", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "session_id", "type": "TEXT", "constraints": "NOT NULL DEFAULT ''"},
                    {"name": "state", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "version", "type": "INTEGER", "constraints": "NOT NULL DEFAULT 1"},
                    {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                    {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
                ],
                "checkpoint_versions": [
                    {"name": "run_id", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "version", "type": "INTEGER", "constraints": "NOT NULL"},
                    {"name": "node", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "status", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "state", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                ],
            }
            self._backend.init_db(schema)
            # 创建索引
            self._backend.execute(
                "CREATE INDEX IF NOT EXISTS idx_ckpt_status ON checkpoints(status)"
            )
            self._backend.execute(
                "CREATE INDEX IF NOT EXISTS idx_ckpt_session ON checkpoints(session_id)"
            )
            self._backend.execute(
                "CREATE INDEX IF NOT EXISTS idx_ckptv_run ON checkpoint_versions(run_id)"
            )

    # ---- 写 ----

    async def save(self, state: AgentState) -> str:
        """保存/更新一次运行的状态快照。返回 run_id。"""
        async with _DB_LOCK:
            return await self._save_locked(state)

    def _build_payload(self, state: AgentState) -> str:
        """序列化状态并做大消息截断(CPU 密集,不碰 IO)。"""
        payload = state.to_json()

        # 大消息截断保护:防止超长 messages 导致 SQLite 写入失败
        from app.config import settings as _cfg
        cap = int(getattr(_cfg, "checkpoint_message_char_cap", 0) or 0)
        if cap > 0 and payload and len(payload) > cap * 4:
            # 逐条截断 messages 中的大字段
            try:
                data = json.loads(payload)
                if isinstance(data, dict) and "messages" in data:
                    truncated = []
                    for m in data["messages"]:
                        mm = dict(m)
                        content = mm.get("content", "")
                        if isinstance(content, str) and len(content) > cap:
                            mm["content"] = content[:cap] + f"...[已截断,{len(content)}字符]"
                        truncated.append(mm)
                    data["messages"] = truncated
                    payload = json.dumps(data, ensure_ascii=False)
                    logger.debug("Checkpoint: messages truncated to cap=%d, total_len=%d", cap, len(payload))
            except Exception:  # noqa: BLE001
                pass  # 截断失败不影响落盘
        return payload

    def _write_locked_sync(self, state: AgentState, payload: str, now: float) -> int:
        """真正落盘(同步)。调用方负责放到线程里,避免阻塞事件循环。"""
        # 使用事务包裹双写,防止 checkpoint_versions 写入成功但 checkpoints 失败导致版本不一致
        with self._backend.transaction():
            # 获取当前版本号
            rows = self._backend.select(
                "checkpoint_versions",
                "run_id = ?",
                (state.run_id,),
                order_by="version DESC",
                limit=1
            )
            version = (rows[0]["version"] + 1) if rows else 1

            # 插入版本历史
            self._backend.upsert(
                "checkpoint_versions",
                {
                    "run_id": state.run_id,
                    "version": version,
                    "node": state.node,
                    "status": state.status,
                    "state": payload,
                    "created_at": now,
                },
                conflict_columns=["run_id", "version"]
            )

            # 插入或更新 checkpoints
            self._backend.upsert(
                "checkpoints",
                {
                    "run_id": state.run_id,
                    "node": state.node,
                    "status": state.status,
                    "session_id": state.session_id,
                    "state": payload,
                    "version": version,
                    "created_at": now,
                    "updated_at": now,
                },
                conflict_columns=["run_id"]
            )

        # 版本历史保留策略:checkpoint_versions 每行都是一份完整 state 快照,
        # 不设上限会让该表无限膨胀(实测近 1GB)并拖慢所有 checkpoint 读写。
        self._trim_versions_sync(state.run_id)
        return version

    def _trim_versions_sync(self, run_id: str) -> None:
        """只保留该 run 最近 N 个历史版本(同步)。"""
        from app.config import settings as _cfg
        keep = int(getattr(_cfg, "agent_checkpoint_max_versions_per_run", 0) or 0)
        if keep <= 0:
            return
        try:
            # 按 version 倒序取第 keep 个的版本号,小于它的全部删除。
            # 用 execute 只取 version 列,避免把整份 state blob 读进内存。
            rows = self._backend.execute(
                "SELECT version FROM checkpoint_versions WHERE run_id = ? "
                "ORDER BY version DESC LIMIT 1 OFFSET ?",
                (run_id, keep),
                fetch=True,
            )
            if not rows:
                return
            cutoff = int(rows[0]["version"])
            self._backend.delete(
                "checkpoint_versions",
                "run_id = ? AND version <= ?",
                (run_id, cutoff),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkpoint 版本裁剪失败 run=%s: %s", run_id[:8], exc)

    async def _save_locked(self, state: AgentState) -> str:
        """实际保存逻辑(需已持有 _DB_LOCK)。"""
        from app.config import settings as _cfg
        now = time.time()

        offload = bool(getattr(_cfg, "agent_checkpoint_offload_to_thread", True))
        if offload:
            # 序列化 + 双写都是 CPU/IO 密集的同步操作(state 常达数百 KB)。
            # 直接在事件循环里做会阻塞整个 loop,表现为 UI/流式输出卡顿,
            # 因此整体卸载到线程池执行。
            try:
                await asyncio.to_thread(self._save_sync_body, state, now)
            except Exception as exc:  # noqa: BLE001
                logger.warning("checkpoint 落盘失败 run=%s: %s", state.run_id[:8], exc)
            return state.run_id

        payload = self._build_payload(state)
        self._write_locked_sync(state, payload, now)
        return state.run_id

    def _save_sync_body(self, state: AgentState, now: float) -> None:
        """在线程中执行:序列化 + 双写 + 版本裁剪。"""
        payload = self._build_payload(state)
        self._write_locked_sync(state, payload, now)

    async def delete(self, run_id: str) -> bool:
        async with _DB_LOCK:
            self._backend.delete("checkpoints", "run_id = ?", (run_id,))
            self._backend.delete("checkpoint_versions", "run_id = ?", (run_id,))
            return True

    # ---- 读 ----

    async def load(self, run_id: str) -> Optional[AgentState]:
        async with _DB_LOCK:
            # 读 + JSON 解码都卸载到线程:state 常达数百 KB,
            # 在事件循环里解码会阻塞流式输出。
            return await asyncio.to_thread(self._load_sync, run_id)

    def _load_sync(self, run_id: str) -> Optional[AgentState]:
        rows = self._backend.select(
            "checkpoints",
            "run_id = ?",
            (run_id,),
            limit=1
        )

        if not rows:
            return None
        try:
            return AgentState.from_json(rows[0]["state"])
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    async def exists(self, run_id: str) -> bool:
        async with _DB_LOCK:
            return self._backend.exists("checkpoints", "run_id = ?", (run_id,))

    # ---- 时间旅行(按版本加载 / 分支回滚) ----

    async def versions(self, run_id: str) -> list[dict]:
        """列出某次运行的全部历史版本(时间旅行视图)。"""
        async with _DB_LOCK:
            rows = self._backend.select(
                "checkpoint_versions",
                "run_id = ?",
                (run_id,),
                order_by="version"
            )
        return [
            {"version": r["version"], "node": r["node"], "status": r["status"], "created_at": r["created_at"]}
            for r in rows
        ]

    async def load_version(self, run_id: str, version: int) -> Optional[AgentState]:
        """加载指定历史版本的运行状态(用于时间旅行/分支回滚)。"""
        async with _DB_LOCK:
            rows = self._backend.select(
                "checkpoint_versions",
                "run_id = ? AND version = ?",
                (run_id, int(version)),
                limit=1
            )
        
        if not rows:
            return None
        try:
            return AgentState.from_json(rows[0]["state"])
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    async def rollback(self, run_id: str, version: int) -> Optional[AgentState]:
        """把最新指针回滚到历史版本(时间旅行分支起点)。"""
        async with _DB_LOCK:
            rows = self._backend.select(
                "checkpoint_versions",
                "run_id = ? AND version = ?",
                (run_id, int(version)),
                limit=1
            )
            if not rows:
                return None
            try:
                st = AgentState.from_json(rows[0]["state"])
            except (json.JSONDecodeError, TypeError, ValueError):
                return None

            now = time.time()
            self._backend.upsert(
                "checkpoints",
                {
                    "run_id": st.run_id,
                    "node": st.node,
                    "status": st.status,
                    "session_id": st.session_id,
                    "state": st.to_json(),
                    "version": int(version),
                    "created_at": now,
                    "updated_at": now,
                },
                conflict_columns=["run_id"]
            )
        
        return st

    async def list(self, limit: int = 100, offset: int = 0, status: Optional[str] = None,
             session_id: Optional[str] = None, tenant_id: Optional[str] = None) -> list[dict]:
        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if tenant_id:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)

        where = " AND ".join(conditions) if conditions else ""

        async with _DB_LOCK:
            # 一次要解码 limit 条(默认 100)完整 state,整体卸载到线程
            return await asyncio.to_thread(
                self._list_sync, where, tuple(params), limit, offset
            )

    def _list_sync(self, where: str, params: tuple, limit: int, offset: int) -> list[dict]:
        rows = self._backend.select(
            "checkpoints",
            where,
            params,
            order_by="updated_at DESC",
            limit=limit,
            offset=offset,
        )

        out: list[dict] = []
        for r in rows:
            try:
                st = AgentState.from_json(r["state"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("checkpoint %s state decode failed: %s", r["run_id"], exc)
                st = None
            base = {
                "run_id": r["run_id"],
                "node": r["node"],
                "status": r["status"],
                "session_id": r["session_id"],
                "version": r["version"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            if st is not None:
                base["summary"] = st.summary()
            out.append(base)
        return out

    async def count(self) -> int:
        async with _DB_LOCK:
            return self._backend.count("checkpoints")

    async def clear(self, status: Optional[str] = None) -> int:
        async with _DB_LOCK:
            if status:
                rows = self._backend.execute(
                    "SELECT run_id FROM checkpoints WHERE status = ?", (status,), fetch=True
                )
                run_ids = [r["run_id"] for r in rows]
                self._backend.delete("checkpoints", "status = ?", (status,))
                if run_ids:
                    placeholders = ",".join("?" * len(run_ids))
                    self._backend.delete(
                        "checkpoint_versions",
                        f"run_id IN ({placeholders})",
                        tuple(run_ids),
                    )
            else:
                self._backend.delete("checkpoints", "1 = 1")
                self._backend.delete("checkpoint_versions", "1 = 1")
            return 1

    async def cleanup_expired(self, ttl_days: float = 7.0) -> dict:
        """清理超过 TTL 的已结束 checkpoint(主表 + 版本表), 并 VACUUM 回收空页。

        - 主表 checkpoints: 删 status 为终态(done/failed/cancelled/interrupted)
          且 updated_at 超 ttl_days 的行。正在 running 的绝不清理。
        - 版本表 checkpoint_versions: 按自身 created_at < cutoff 直接删。
          版本表是膨胀大头(每行一份完整 state 快照), 旧实现用
          run_id IN(主表终态且过期) 关联删, 会漏掉主表已删/非终态的孤儿版本;
          且主表终态集合漏了 'interrupted'(启动清扫把孤儿 running 改写成的状态,
          否则永远清不掉)。这里直接按版本自身时间戳删, 跨 run 统一老化、不漏删。

        返回 {"checkpoints": 删主表行数, "versions": 删版本行数, "vacuum": 是否 VACUUM 成功}。
        """
        import time as _time

        cutoff = _time.time() - ttl_days * 86400
        statuses = ",".join("?" for _ in self._TERMINAL_STATUSES)
        async with _DB_LOCK:
            # 主表: 先 count 要删的行数(用于统计/返回值), 再删
            removed_main = self._backend.count(
                "checkpoints",
                f"status IN ({statuses}) AND updated_at < ?",
                (*self._TERMINAL_STATUSES, cutoff),
            )
            self._backend.delete(
                "checkpoints",
                f"status IN ({statuses}) AND updated_at < ?",
                (*self._TERMINAL_STATUSES, cutoff),
            )
            # 版本表: 按自身时间戳直接删过期版本(含孤儿), 不漏删
            removed_versions = self._backend.count(
                "checkpoint_versions", "created_at < ?", (cutoff,)
            )
            self._backend.delete("checkpoint_versions", "created_at < ?", (cutoff,))
        # VACUUM 回收空页: 删掉的数据若不 VACUUM, SQLite 文件不收缩,
        # 下次读写仍付整库代价。临时文件落有空间的盘(见下), 避免 C 盘满时失败。
        vacuumed = self._vacuum_locked()
        return {"checkpoints": removed_main, "versions": removed_versions, "vacuum": vacuumed}

    def _vacuum_locked(self) -> bool:
        """执行 VACUUM 回收空页, 返回是否成功。

        SQLite 删数据后不 VACUUM, 文件不收缩, 下次读写仍付整库代价。VACUUM 需
        独占、重写整个库文件(写回原文件, 数据在 E 盘 data/, 空间充足)。
        backend.connect() 已对连接设 PRAGMA temp_store=MEMORY, VACUUM 的排序/聚合
        临时文件走内存, 不依赖 C 盘 TEMP —— 因此 C 盘近满也不会让 VACUUM 失败。

        不支持 connect 的后端(Postgres/Redis)直接跳过 VACUUM(数据删除已生效,
        仅文件收缩做不了), 记日志返回 False。任何异常都不让清理报错。
        """
        backend = self._backend
        if not hasattr(backend, "connect") or not hasattr(backend, "close"):
            return False
        try:
            conn = backend.connect()
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("VACUUM")
                conn.commit()
            finally:
                backend.close(conn)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkpoint VACUUM 失败(数据已删, 文件需停后端后手动收缩): %s", exc)
            return False
