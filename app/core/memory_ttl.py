# Memory TTL manager - unified in memory_manager.py
from app.core.memory_manager import MemoryTTLManager, get_memory_manager, UnifiedMemoryManager  # noqa: F401

# Alias for backward compatibility
def start_memory_ttl_cleanup():
    """启动记忆 TTL 清理 (已合并到 UnifiedMemoryManager)"""
    get_memory_manager()

def stop_memory_ttl_cleanup():
    """停止记忆 TTL 清理"""
    pass
