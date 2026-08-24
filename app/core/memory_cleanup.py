# UnifiedMemoryManager is in memory_manager.py
# Old modules kept as aliases for backward compatibility
from app.core.memory_manager import (  # noqa: F401
    UnifiedMemoryManager as MemoryCleanupEngine,
    UnifiedMemoryManager as MemoryTTLManager,
    UnifiedMemoryManager as MemoryConflictResolver,
    UnifiedMemoryManager as MemoryDecay,
    get_memory_manager,
)
