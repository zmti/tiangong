"""Checkpoint 批量写入管理器:减少磁盘 I/O 操作。

设计思路:
1. 内存缓冲:每个 run_id 的状态缓存在内存中
2. 延迟写入:等待一小段时间(默认 2 秒)批量合并写入
3. 刷新触发:达到阈值或超时时强制刷新
4. 进程退出:确保所有缓冲数据被写入

性能提升:
- 减少 SQLite 写操作次数(从 N 次降到 1 次)
- 降低磁盘 I/O 压力
- 保持 checkpoint 恢复能力(最后的状态始终可用)
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from app.runtime.state import AgentState
from app.runtime.checkpoint import CheckpointManager

logger = logging.getLogger(__name__)


@dataclass
class BufferedState:
    """待写入的状态缓冲。"""
    state: AgentState
    last_modified: float  # 最后修改时间
    modify_count: int = 0  # 修改次数


class BatchCheckpointManager:
    """批量 Checkpoint 写入管理器。"""
    
    def __init__(
        self,
        base_manager: CheckpointManager,
        flush_interval: float = 2.0,
        max_buffer_size: int = 10,
        auto_flush_on_exit: bool = True,
    ):
        """初始化批量写入管理器。
        
        Args:
            base_manager: 基础 CheckpointManager 实例
            flush_interval: 刷新间隔(秒)，默认 2 秒
            max_buffer_size: 最大缓冲区大小，默认 10 个 run_id
            auto_flush_on_exit: 退出时自动刷新，默认 True
        """
        self._base = base_manager
        self._flush_interval = flush_interval
        self._max_buffer_size = max_buffer_size
        self._auto_flush_on_exit = auto_flush_on_exit
        
        # 缓冲字典: run_id -> BufferedState
        self._buffer: dict[str, BufferedState] = {}
        self._lock = asyncio.Lock()
        
        # 后台刷新任务
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        
        # 统计
        self._stats = {
            "buffered_count": 0,
            "flushed_count": 0,
            "skipped_count": 0,  # 跳过重复写入
            "total_write_time": 0.0,
        }
    
    async def start(self) -> None:
        """启动后台刷新任务。"""
        if self._running:
            return
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("批量 Checkpoint 管理器已启动 (interval=%.1fs, max_buffer=%d)",
                   self._flush_interval, self._max_buffer_size)
    
    async def stop(self) -> None:
        """停止后台刷新任务并刷新所有缓冲。"""
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self.flush_all()
        logger.info("批量 Checkpoint 管理器已停止")
    
    async def save(self, state: AgentState) -> str:
        """保存状态到缓冲区(非阻塞)。
        
        Args:
            state: Agent 状态
            
        Returns:
            run_id
        """
        async with self._lock:
            run_id = state.run_id
            now = time.time()
            
            if run_id in self._buffer:
                # 更新已有缓冲
                entry = self._buffer[run_id]
                entry.state = state
                entry.last_modified = now
                entry.modify_count += 1
            else:
                # 新建缓冲
                if len(self._buffer) >= self._max_buffer_size:
                    # 缓冲区满，强制刷新最旧的一个
                    await self._evict_oldest()
                self._buffer[run_id] = BufferedState(
                    state=state,
                    last_modified=now,
                    modify_count=1,
                )
            
            self._stats["buffered_count"] += 1
            logger.debug("Checkpoint 缓冲写入: %s (buffer_size=%d)", run_id[:8], len(self._buffer))
            
            # 如果修改次数较多，立即刷新
            if state.status in ("done", "failed", "interrupted", "cancelled"):
                await self.flush(run_id)
            
            return run_id
    
    async def flush(self, run_id: str) -> None:
        """刷新指定 run_id 的缓冲到磁盘。
        
        Args:
            run_id: 运行 ID
        """
        async with self._lock:
            if run_id not in self._buffer:
                return
            
            entry = self._buffer.pop(run_id)
            t0 = time.time()
            
            try:
                await self._base.save(entry.state)
                self._stats["flushed_count"] += 1
                self._stats["total_write_time"] += time.time() - t0
                logger.debug("Checkpoint 刷盘成功: %s (耗时=%.3fs)", run_id[:8], time.time() - t0)
            except Exception as e:
                logger.error("Checkpoint 刷盘失败: %s, 错误: %s", run_id[:8], e)
                # 重新放回缓冲区
                self._buffer[run_id] = entry
    
    async def flush_all(self) -> None:
        """刷新所有缓冲到磁盘。"""
        async with self._lock:
            run_ids = list(self._buffer.keys())
        
        for run_id in run_ids:
            await self.flush(run_id)
        
        logger.info("Checkpoint 全部刷盘完成 (%d 条)", len(run_ids))
    
    async def _evict_oldest(self) -> None:
        """淘汰最旧的缓冲条目(强制刷盘)。"""
        if not self._buffer:
            return
        
        oldest_id = min(self._buffer.keys(), key=lambda rid: self._buffer[rid].last_modified)
        logger.warning("Checkpoint 缓冲区满，强制刷盘最旧条目: %s", oldest_id[:8])
        await self.flush(oldest_id)
        self._stats["skipped_count"] += 1
    
    async def _flush_loop(self) -> None:
        """后台刷新循环。"""
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                if self._buffer:
                    await self.flush_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Checkpoint 后台刷新出错: %s", e)
    
    def get_stats(self) -> dict:
        """获取统计信息。"""
        avg_write_time = 0.0
        if self._stats["flushed_count"] > 0:
            avg_write_time = self._stats["total_write_time"] / self._stats["flushed_count"]
        
        return {
            "buffer_size": len(self._buffer),
            "max_buffer_size": self._max_buffer_size,
            "flush_interval": self._flush_interval,
            "buffered_count": self._stats["buffered_count"],
            "flushed_count": self._stats["flushed_count"],
            "skipped_count": self._stats["skipped_count"],
            "avg_write_time": round(avg_write_time, 3),
            "running": self._running,
        }
    
    @property
    def buffer_size(self) -> int:
        """当前缓冲区大小。"""
        return len(self._buffer)


# 全局单例
_batch_manager: Optional[BatchCheckpointManager] = None
_batch_manager_lock = asyncio.Lock()


async def get_batch_checkpoint_manager(base_manager: CheckpointManager) -> BatchCheckpointManager:
    """获取批量 Checkpoint 管理器单例。"""
    global _batch_manager
    async with _batch_manager_lock:
        if _batch_manager is None or _batch_manager._base is not base_manager:
            if _batch_manager:
                await _batch_manager.stop()
            _batch_manager = BatchCheckpointManager(base_manager)
            await _batch_manager.start()
        return _batch_manager


async def stop_batch_checkpoint_manager() -> None:
    """停止批量 Checkpoint 管理器。"""
    global _batch_manager
    async with _batch_manager_lock:
        if _batch_manager:
            await _batch_manager.stop()
            _batch_manager = None
