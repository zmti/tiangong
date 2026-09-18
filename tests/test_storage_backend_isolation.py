"""验证 StorageBackend 按 db_path 隔离:不同数据库文件的后端各自独立。

修复 P0 bug:旧实现 get_storage_backend 用全局单例,第一次调用后缓存,
后续所有不同 db_path 的调用全部返回第一个后端,导致 sessions.db /
checkpoints.db / memory.db 数据散落到同一物理文件。
"""
import os
import tempfile

from app.core import storage_backend as sb


def test_distinct_db_paths_get_distinct_backends():
    """不同 db_path 应返回不同的 SqliteBackend 实例(各自独立文件)。"""
    sb.reset_storage_backend()
    tmp = tempfile.mkdtemp()
    a = os.path.join(tmp, "a.db")
    b = os.path.join(tmp, "b.db")

    backend_a = sb.get_storage_backend(db_path=a)
    backend_b = sb.get_storage_backend(db_path=b)

    assert backend_a is not backend_b, "不同 db_path 不应共享同一后端实例"
    assert backend_a._db_path == a
    assert backend_b._db_path == b

    # 同一 db_path 复用同一实例(缓存生效)
    backend_a2 = sb.get_storage_backend(db_path=a)
    assert backend_a2 is backend_a

    sb.reset_storage_backend()


def test_same_backend_different_path_no_cross_writes():
    """写入 a.db 不影响 b.db(隔离正确性)。"""
    sb.reset_storage_backend()
    tmp = tempfile.mkdtemp()
    a = os.path.join(tmp, "a.db")
    b = os.path.join(tmp, "b.db")

    ba = sb.get_storage_backend(db_path=a)
    bb = sb.get_storage_backend(db_path=b)

    ba.init_db({"t": [{"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"}]})
    ba.insert("t", {"id": "x"})

    # b.db 不应看到 a.db 的数据
    bb.init_db({"t": [{"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"}]})
    rows = bb.select("t", "1 = 1")
    assert rows == [], f"b.db 不应包含 a.db 的数据, 实际: {rows}"

    sb.reset_storage_backend()


def test_memory_mode_isolated_from_disk():
    """:memory: 后端独立于磁盘后端。"""
    sb.reset_storage_backend()
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "disk.db")

    mem_backend = sb.get_storage_backend(db_path=":memory:", memory_mode=True)
    disk_backend = sb.get_storage_backend(db_path=path)

    assert mem_backend is not disk_backend
    assert mem_backend._memory_mode is True
    assert disk_backend._memory_mode is False

    sb.reset_storage_backend()
