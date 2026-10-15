"""数据清理模块：自动清理过期会话、检查和点、日志等。"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)


def cleanup_old_sessions(days: int = 30, checkpoint_days: int = None) -> dict:
    """清理过期会话及其关联的检查点数据。

    Args:
        days: 会话保留天数，默认 30 天
        checkpoint_days: 检查点保留天数，默认与 days 相同；可单独设更短
            (如用 settings.checkpoint_ttl_days=7)以更早回收 checkpoint 大 blob

    Returns:
        清理统计信息: {cleaned(会话数), checkpoint_rows, version_rows,
                       freed_mb, days, checkpoint_days}
    """
    import sqlite3
    import os

    data_dir = Path(settings.resolved_data_dir)
    sessions_db = data_dir / "sessions.db"
    checkpoints_db = data_dir / "checkpoints.db"

    if not sessions_db.exists():
        return {"cleaned": 0, "reason": "sessions.db 不存在"}

    cutoff = time.time() - (days * 86400)
    cp_days = checkpoint_days if checkpoint_days else days
    cp_cutoff = time.time() - (cp_days * 86400)
    cleaned = 0
    checkpoint_rows = 0
    version_rows = 0
    size_before = 0
    size_after = 0

    try:
        # 记录清理前涉及的两个库体积
        size_before = sessions_db.stat().st_size
        if checkpoints_db.exists():
            size_before += checkpoints_db.stat().st_size

        with sqlite3.connect(str(sessions_db)) as conn:
            # 删除过期会话
            result = conn.execute(
                "DELETE FROM sessions WHERE updated_at < ?",
                (cutoff,)
            )
            cleaned = result.rowcount
            conn.commit()
            # VACUUM 回收删除后留下的空页(否则只删数据不释放磁盘)
            conn.execute("VACUUM")

        # 清理过期检查点: checkpoints 表(按 updated_at) + checkpoint_versions 表
        # (按 created_at; 该表存各节点状态快照的 state blob, 是 checkpoints.db 体积大头)
        if checkpoints_db.exists():
            with sqlite3.connect(str(checkpoints_db)) as cp_conn:
                checkpoint_rows = cp_conn.execute(
                    "DELETE FROM checkpoints WHERE updated_at < ?",
                    (cp_cutoff,),
                ).rowcount
                try:
                    version_rows = cp_conn.execute(
                        "DELETE FROM checkpoint_versions WHERE created_at < ?",
                        (cp_cutoff,),
                    ).rowcount
                except sqlite3.OperationalError:
                    version_rows = 0  # 旧库可能没有 checkpoint_versions 表
                cp_conn.commit()
                cp_conn.execute("VACUUM")

        size_after = sessions_db.stat().st_size
        if checkpoints_db.exists():
            size_after += checkpoints_db.stat().st_size
        freed_mb = max(0.0, round((size_before - size_after) / 1024 / 1024, 2))

        logger.info(
            f"清理 {cleaned} 个过期会话、{checkpoint_rows} 个检查点、"
            f"{version_rows} 个检查点版本(会话>{days}d, 检查点>{cp_days}d), 释放 {freed_mb}MB"
        )
        return {
            "cleaned": cleaned,
            "checkpoint_rows": checkpoint_rows,
            "version_rows": version_rows,
            "freed_mb": freed_mb,
            "days": days,
            "checkpoint_days": cp_days,
        }

    except Exception as e:
        logger.error(f"清理会话失败: {e}")
        return {"cleaned": 0, "error": str(e)}


def cleanup_old_logs(days: int = 7) -> dict:
    """清理过期日志文件。
    
    Args:
        days: 保留天数，默认 7 天
        
    Returns:
        清理统计信息
    """
    data_dir = Path(settings.resolved_data_dir)
    logs_dir = data_dir / "logs"
    
    if not logs_dir.exists():
        return {"cleaned": 0, "reason": "logs/ 目录不存在"}
    
    cutoff = time.time() - (days * 86400)
    cleaned = 0
    
    try:
        for f in logs_dir.glob("*.log"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                cleaned += 1
        
        logger.info(f"清理 {cleaned} 个过期日志文件（{days} 天前）")
        return {"cleaned": cleaned, "days": days}
        
    except Exception as e:
        logger.error(f"清理日志失败: {e}")
        return {"cleaned": 0, "error": str(e)}


def cleanup_temp_files() -> dict:
    """清理临时文件。
    
    Returns:
        清理统计信息
    """
    data_dir = Path(settings.resolved_data_dir)
    cleaned = 0
    freed_bytes = 0
    
    try:
        # 清理临时文件
        for ext in ["*.tmp", "*.temp", "*.bak"]:
            for f in data_dir.rglob(ext):
                freed_bytes += f.stat().st_size
                f.unlink()
                cleaned += 1
        
        # 清理空目录
        for root, dirs, files in os.walk(data_dir, topdown=False):
            for d in dirs:
                dir_path = Path(root) / d
                if not any(dir_path.iterdir()):
                    dir_path.rmdir()
        
        logger.info(f"清理 {cleaned} 个临时文件，释放 {freed_bytes / 1024 / 1024:.2f} MB")
        return {
            "cleaned": cleaned,
            "freed_bytes": freed_bytes,
            "freed_mb": round(freed_bytes / 1024 / 1024, 2)
        }
        
    except Exception as e:
        logger.error(f"清理临时文件失败: {e}")
        return {"cleaned": 0, "error": str(e)}


def get_cleanup_stats() -> dict:
    """获取清理统计信息。
    
    Returns:
        清理统计信息
    """
    import sqlite3
    from pathlib import Path
    
    data_dir = Path(settings.resolved_data_dir)
    stats = {
        "sessions_db": {},
        "checkpoints_db": {},
        "tasks_db": {},
        "memory_db": {},
        "disk_usage_mb": 0,
    }
    
    for db_name in ["sessions.db", "checkpoints.db", "tasks.db", "memory.db"]:
        db_path = data_dir / db_name
        if db_name == "sessions.db":
            table = "sessions"
        elif db_name == "checkpoints.db":
            table = "checkpoints"
        else:
            table = db_name.replace(".db", "")
        
        if db_path.exists():
            try:
                with sqlite3.connect(str(db_path)) as conn:
                    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    size = db_path.stat().st_size
                    stats[f"{db_name.replace('.db', '')}_records"] = count
                    stats[f"{db_name.replace('.db', '')}_size_mb"] = round(size / 1024 / 1024, 2)
            except Exception as e:
                stats[f"{db_name.replace('.db', '')}_error"] = str(e)
    
    # 计算总磁盘使用
    total_size = sum(f.stat().st_size for f in data_dir.rglob("*") if f.is_file())
    stats["disk_usage_mb"] = round(total_size / 1024 / 1024, 2)
    
    return stats


def run_cleanup(days: int = 30, checkpoint_days: int = None) -> dict:
    """运行完整的清理流程。

    Args:
        days: 会话/日志保留天数
        checkpoint_days: 检查点保留天数，默认读 settings.checkpoint_ttl_days(=7)，
            可单独传更短值以更早回收 checkpoints.db 的大 state blob

    Returns:
        清理结果汇总
    """
    logger.info(f"开始数据清理（保留 {days} 天）")

    results = {
        "sessions": cleanup_old_sessions(days, checkpoint_days=checkpoint_days),
        "logs": cleanup_old_logs(7),
        "temp": cleanup_temp_files(),
    }

    logger.info("数据清理完成")
    return results
