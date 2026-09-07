"""数据库查询工具:支持多数据源只读查询。"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from typing import Optional

from pydantic import Field

from app.config import settings
from app.tools.registry import tool

logger = logging.getLogger(__name__)


# SQL 安全检查:只允许 SELECT 查询
_FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "GRANT", "REVOKE", "EXEC", "EXECUTE", "CALL", "MERGE", "UPSERT",
    "REPLACE", "LOAD", "INTO", "OUTFILE", "INFILE", "LOCK", "UNLOCK",
)
# 表名/列名安全校验:仅允许合法 SQL 标识符
_TABLE_NAME_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


def _validate_table_name(table_name: str) -> Optional[str]:
    """校验表名是否为合法 SQL 标识符,防止注入。返回错误信息或 None。"""
    if not _TABLE_NAME_RE.match(table_name):
        return f"表名非法: {table_name!r}，仅允许字母、数字、下划线,且必须以字母或下划线开头"
    return None


def _safe_pragma_table_info(conn: sqlite3.Connection, table_name: str) -> list[dict]:
    """安全获取 PRAGMA table_info,表名已通过白名单校验。"""
    cursor = conn.cursor()
    # 表名已做严格正则校验,此处用双引号包裹防止保留字冲突
    cursor.execute(f'PRAGMA table_info("{table_name}")')
    return [
        {
            "name": col[1],
            "type": col[2],
            "not_null": bool(col[3]),
            "default": col[4],
            "primary_key": bool(col[5]),
        }
        for col in cursor.fetchall()
    ]


def _safe_count(conn: sqlite3.Connection, table_name: str) -> int:
    """安全执行 SELECT COUNT(*) FROM <table>,表名已通过白名单校验。"""
    cursor = conn.cursor()
    cursor.execute(f'SELECT COUNT(*) FROM "{table_name}"')
    return cursor.fetchone()[0]


def _safe_select_all(conn: sqlite3.Connection, table_name: str, limit: int = 5) -> list[dict]:
    """安全执行 SELECT * FROM <table> LIMIT N,表名已通过白名单校验,LIMIT 使用参数化。"""
    cursor = conn.cursor()
    cursor.execute(f'SELECT * FROM "{table_name}" LIMIT ?', (limit,))
    cols = [desc[0] for desc in cursor.description] if cursor.description else []
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _list_tables(conn: sqlite3.Connection) -> list[str]:
    """列出数据库所有表名(使用 SQLite 系统表查询,无用户输入)。"""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [row[0] for row in cursor.fetchall()]


def _validate_readonly_sql(sql: str) -> Optional[str]:
    """验证 SQL 是否为只读查询。返回错误信息或 None。"""
    if not sql or not sql.strip():
        return "SQL 语句不能为空"
    normalized = sql.strip().upper()
    # 移除注释
    normalized = re.sub(r"--.*$", "", normalized, flags=re.MULTILINE)
    normalized = re.sub(r"/\*.*?\*/", "", normalized, flags=re.DOTALL)
    normalized = normalized.strip()

    # 必须以 SELECT 开头
    if not normalized.startswith("SELECT"):
        return "只允许 SELECT 查询,不支持 INSERT/UPDATE/DELETE 等写操作"

    # 检查危险关键词
    for keyword in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", normalized):
            return f"检测到危险关键词: {keyword},只允许 SELECT 查询"

    return None


def _format_results(rows: list[dict], columns: list[str], max_rows: int = 100) -> dict:
    """格式化查询结果。"""
    total = len(rows)
    truncated = total > max_rows
    if truncated:
        rows = rows[:max_rows]

    return {
        "columns": columns,
        "rows": rows,
        "total_rows": total,
        "returned_rows": len(rows),
        "truncated": truncated,
    }


# SQLite 连接管理(用于本地文件数据库)
_sqlite_connections: dict[str, sqlite3.Connection] = {}
_sqlite_connections_lock = threading.Lock()


def _validate_db_path(db_path: str) -> Optional[str]:
    """校验数据库路径安全,防止路径穿越。

    规则:
    1. 必须指向一个已存在的 .db 文件
    2. 不允许指向系统敏感目录
    3. 路径必须能被 abspath 解析为绝对路径
    """
    if not db_path or not db_path.strip():
        return None
    try:
        full = os.path.abspath(os.path.expanduser(db_path))
    except (OSError, ValueError):
        return None
    if not os.path.isfile(full):
        return None
    # 禁止指向系统敏感目录
    forbidden_prefixes = ("/etc/", "/proc/", "/sys/", "C:\\Windows\\", "/root/")
    for prefix in forbidden_prefixes:
        if full.startswith(prefix) or full.lower().startswith(prefix.lower()):
            return None
    return full


def _get_sqlite_connection(db_path: str) -> sqlite3.Connection:
    """获取或创建 SQLite 连接(带锁保护线程安全)。"""
    with _sqlite_connections_lock:
        if db_path not in _sqlite_connections:
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            _sqlite_connections[db_path] = conn
        return _sqlite_connections[db_path]


@tool("query_sqlite", "查询 SQLite 数据库(只读)", timeout=30)
def query_sqlite(
    sql: str,
    db_path: str = "data/local.db",
    max_rows: int = 100,
) -> dict:
    """
    查询 SQLite 数据库。

    Args:
        sql: 只允许 SELECT 查询语句
        db_path: SQLite 数据库文件路径
        max_rows: 最大返回行数

    Returns:
        查询结果,包含 columns 和 rows
    """
    # SQL 安全检查
    error = _validate_readonly_sql(sql)
    if error:
        return {"error": error}

    # 校验 db_path 安全性
    safe_path = _validate_db_path(db_path)
    if safe_path is None:
        return {"error": f"数据库路径无效或不存在: {db_path!r}"}

    try:
        conn = _get_sqlite_connection(safe_path)
        cursor = conn.cursor()
        cursor.execute(sql)

        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = [dict(row) for row in cursor.fetchall()]

        return _format_results(rows, columns, max_rows)
    except Exception as e:
        logger.error("SQLite 查询失败: %s", e)
        return {"error": f"查询失败: {e}"}


@tool("list_sqlite_tables", "列出 SQLite 数据库中的所有表", timeout=10)
def list_sqlite_tables(
    db_path: str = "data/local.db",
) -> dict:
    """列出数据库中的所有表及其结构。"""
    safe_path = _validate_db_path(db_path)
    if safe_path is None:
        return {"error": f"数据库路径无效或不存在: {db_path!r}"}
    try:
        conn = _get_sqlite_connection(safe_path)
        tables = _list_tables(conn)

        table_info = {}
        for table in tables:
            # 表名由 sqlite_master 返回且不含用户输入,安全
            columns = _safe_pragma_table_info(conn, table)
            row_count = _safe_count(conn, table)
            table_info[table] = {"columns": columns, "row_count": row_count}

        return {"tables": tables, "table_info": table_info}
    except Exception as e:
        logger.error("列出 SQLite 表失败: %s", e)
        return {"error": f"获取表信息失败: {e}"}


@tool("describe_table", "查看表的详细结构", timeout=10)
def describe_table(
    table_name: str,
    db_path: str = "data/local.db",
) -> dict:
    """查看指定表的详细结构信息。"""
    # 表名安全校验:仅允许合法标识符，防止 SQL 注入
    err = _validate_table_name(table_name)
    if err:
        return {"error": err}
    safe_path = _validate_db_path(db_path)
    if safe_path is None:
        return {"error": f"数据库路径无效或不存在: {db_path!r}"}
    try:
        conn = _get_sqlite_connection(safe_path)
        columns = _safe_pragma_table_info(conn, table_name)

        if not columns:
            return {"error": f"表 {table_name} 不存在"}

        row_count = _safe_count(conn, table_name)
        sample_data = _safe_select_all(conn, table_name, limit=5)

        return {
            "table_name": table_name,
            "columns": columns,
            "row_count": row_count,
            "sample_data": sample_data,
        }
    except Exception as e:
        logger.error("描述表失败: %s", e)
        return {"error": f"获取表结构失败: {e}"}


# PostgreSQL 支持(可选,需要安装 psycopg2)
try:
    import psycopg2
    import psycopg2.extras

    @tool("query_postgres", "查询 PostgreSQL 数据库(只读)", timeout=30)
    def query_postgres(
        sql: str,
        host: str = "localhost",
        port: int = 5432,
        database: str = "",
        user: str = "",
        password: str = "",
        max_rows: int = 100,
    ) -> dict:
        """
        查询 PostgreSQL 数据库。

        Args:
            sql: 只允许 SELECT 查询语句
            host: 数据库主机
            port: 端口
            database: 数据库名
            user: 用户名
            password: 密码
            max_rows: 最大返回行数

        Returns:
            查询结果
        """
        # SQL 安全检查
        error = _validate_readonly_sql(sql)
        if error:
            return {"error": error}

        # 使用默认配置
        host = host or getattr(settings, "postgres_host", "localhost")
        port = port or getattr(settings, "postgres_port", 5432)
        database = database or getattr(settings, "postgres_database", "")
        user = user or getattr(settings, "postgres_user", "")
        password = password or getattr(settings, "postgres_password", "")

        if not all([host, database, user]):
            return {"error": "请配置数据库连接信息(host/database/user)"}

        try:
            conn = psycopg2.connect(
                host=host,
                port=port,
                database=database,
                user=user,
                password=password,
                options="-c statement_timeout=30000",  # 30秒超时
            )
            conn.set_session(readonly=True)  # 设置为只读模式

            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                columns = [desc[0] for desc in cur.description] if cur.description else []
                rows = [dict(row) for row in cur.fetchall()]

            conn.close()

            return _format_results(rows, columns, max_rows)
        except Exception as e:
            logger.error("PostgreSQL 查询失败: %s", e)
            return {"error": f"PostgreSQL 查询失败: {e}"}

    @tool("list_postgres_tables", "列出 PostgreSQL 数据库中的所有表", timeout=10)
    def list_postgres_tables(
        host: str = "localhost",
        port: int = 5432,
        database: str = "",
        user: str = "",
        password: str = "",
        schema: str = "public",
    ) -> dict:
        """列出 PostgreSQL 数据库中的所有表。"""
        host = host or getattr(settings, "postgres_host", "localhost")
        port = port or getattr(settings, "postgres_port", 5432)
        database = database or getattr(settings, "postgres_database", "")
        user = user or getattr(settings, "postgres_user", "")
        password = password or getattr(settings, "postgres_password", "")

        if not all([host, database, user]):
            return {"error": "请配置数据库连接信息(host/database/user)"}

        try:
            conn = psycopg2.connect(
                host=host, port=port, database=database,
                user=user, password=password,
            )
            conn.set_session(readonly=True)

            with conn.cursor() as cur:
                cur.execute("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = %s
                    ORDER BY table_name
                """, (schema,))
                tables = [row[0] for row in cur.fetchall()]

            conn.close()

            return {"tables": tables, "schema": schema}
        except Exception as e:
            logger.error("列出 PostgreSQL 表失败: %s", e)
            return {"error": f"获取表列表失败: {e}"}

except ImportError:
    # psycopg2 未安装,跳过 PostgreSQL 工具
    pass


# MySQL 支持(可选,需要安装 pymysql)
try:
    import pymysql
    import pymysql.cursors

    @tool("query_mysql", "查询 MySQL 数据库(只读)", timeout=30)
    def query_mysql(
        sql: str,
        host: str = "localhost",
        port: int = 3306,
        database: str = "",
        user: str = "",
        password: str = "",
        max_rows: int = 100,
    ) -> dict:
        """
        查询 MySQL 数据库。

        Args:
            sql: 只允许 SELECT 查询语句
            host: 数据库主机
            port: 端口
            database: 数据库名
            user: 用户名
            password: 密码
            max_rows: 最大返回行数

        Returns:
            查询结果
        """
        # SQL 安全检查
        error = _validate_readonly_sql(sql)
        if error:
            return {"error": error}

        host = host or getattr(settings, "mysql_host", "localhost")
        port = port or getattr(settings, "mysql_port", 3306)
        database = database or getattr(settings, "mysql_database", "")
        user = user or getattr(settings, "mysql_user", "")
        password = password or getattr(settings, "mysql_password", "")

        if not all([host, database, user]):
            return {"error": "请配置数据库连接信息(host/database/user)"}

        try:
            conn = pymysql.connect(
                host=host, port=port, database=database,
                user=user, password=password,
                cursorclass=pymysql.cursors.DictCursor,
                read_default_file=None,
                connect_timeout=10,
            )

            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [desc[0] for desc in cur.description] if cur.description else []
                rows = [dict(row) for row in cur.fetchall()]

            conn.close()

            return _format_results(rows, columns, max_rows)
        except Exception as e:
            logger.error("MySQL 查询失败: %s", e)
            return {"error": f"MySQL 查询失败: {e}"}

except ImportError:
    # pymysql 未安装,跳过 MySQL 工具
    pass
