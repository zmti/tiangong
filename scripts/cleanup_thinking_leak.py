"""清理历史会话里泄漏到正文的思考内容。

背景:旧版后端未剥离模型写在正文里的 <思考要点>/<thinking> 块,导致:
  1. 历史回放时正文出现裸标签;
  2. 这些内容每一轮都会被回喂给模型,上下文雪球式膨胀(单会话曾达 87K 字符),
     首字延迟极高,表现为"点了发送但没内容输出"。

本脚本把成对标签内的内容转存到 message['meta']['thinking'](不丢数据),
并从 content 中移除;对未成对的孤立标签只移除标签本身,避免误删正文。

用法:
    python scripts/cleanup_thinking_leak.py [--dry-run]
"""

import argparse
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "sessions.db"

# 中文标签可能是 <思考要点>/<思考过程>/<思考> 等,统一按"思考 + 0~4 个汉字"匹配
TAG = r"(?:思考[\u4e00-\u9fa5]{0,4}|thinking|think)"
PAIRED = re.compile(rf"<\s*{TAG}\s*>([\s\S]*?)<\s*/\s*{TAG}\s*>", re.IGNORECASE)
STRAY = re.compile(rf"</?\s*{TAG}\s*>", re.IGNORECASE)


def clean(text: str) -> tuple[str, str]:
    """返回 (清洗后的正文, 提取到的思考内容)。"""
    if not text:
        return text, ""
    chunks: list[str] = []

    def take(m):
        inner = (m.group(1) or "").strip()
        if inner:
            chunks.append(inner)
        return ""

    body = PAIRED.sub(take, text)
    # 未成对的孤立标签:只摘掉标签本身,正文保留(无法可靠切分思考与答案)
    body = STRAY.sub("", body)
    # 连续空行收敛,避免剥离后留下大片空行
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body, "\n\n".join(chunks)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = ap.parse_args()

    if not DB.exists():
        print(f"找不到数据库: {DB}")
        return 1

    if not args.dry_run:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = DB.with_suffix(f".db.bak-{stamp}")
        shutil.copy2(DB, backup)
        print(f"已备份: {backup}")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, messages FROM sessions").fetchall()

    hit_sessions = 0
    hit_msgs = 0
    removed_chars = 0

    for row in rows:
        try:
            msgs = json.loads(row["messages"] or "[]")
        except Exception:
            continue
        if not isinstance(msgs, list):
            continue

        changed = False
        for m in msgs:
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if not isinstance(content, str) or not STRAY.search(content):
                continue
            body, thinking = clean(content)
            if thinking:
                meta = m.get("meta") or {}
                if not isinstance(meta, dict):
                    meta = {}
                meta["thinking"] = (meta.get("thinking", "") + "\n\n" + thinking).strip() if meta.get("thinking") else thinking
                m["meta"] = meta
            if body != content:
                removed_chars += len(content) - len(body)
                m["content"] = body
                changed = True
                hit_msgs += 1

        if changed:
            hit_sessions += 1
            if not args.dry_run:
                conn.execute(
                    "UPDATE sessions SET messages = ? WHERE id = ?",
                    (json.dumps(msgs, ensure_ascii=False), row["id"]),
                )

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"会话命中: {hit_sessions} | 消息清洗: {hit_msgs} | 正文减少字符: {removed_chars}")
    print("模式: dry-run" if args.dry_run else "模式: 已写入")
    return 0


if __name__ == "__main__":
    sys.exit(main())
