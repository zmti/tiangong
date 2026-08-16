import sqlite3, sys
sys.stdout.reconfigure(encoding="utf-8")
for label, path in [
    ("根目录 data", r"E:\dev\mj_asset\agent-service\data\sessions.db"),
    ("exe 的 dist data", r"E:\dev\mj_asset\agent-service\desktop\backend\dist\data\sessions.db"),
]:
    try:
        con = sqlite3.connect(path)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM sessions")
        total = cur.fetchone()[0]
        cur.execute("SELECT id, created_at FROM sessions ORDER BY updated_at DESC LIMIT 8")
        rows = cur.fetchall()
        print(f"--- {label}: 共 {total} 个会话, 最近 8 个:")
        for r in rows:
            print("   ", r)
        con.close()
    except Exception as e:
        print(f"--- {label}: 读取失败 {e}")
