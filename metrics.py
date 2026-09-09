"""تسجيل أحداث الاستخدام وتلخيصها."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from logging_config import get_logger

log = get_logger(__name__)
DB_PATH = Path("company.db")


def init_db(path=DB_PATH) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                kind        TEXT    NOT NULL,
                question    TEXT,
                mode        TEXT,
                tools       TEXT,
                sources     INTEGER DEFAULT 0,
                tokens      INTEGER DEFAULT 0,
                duration_ms INTEGER DEFAULT 0,
                outcome     TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        conn.commit()
    finally:
        conn.close()


def record(kind, question="", mode="", tools=None, sources=0, tokens=0,
           duration_ms=0, outcome="ok", path=DB_PATH) -> None:
    try:
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                """INSERT INTO events
                   (ts, kind, question, mode, tools, sources, tokens, duration_ms, outcome)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), kind,
                 question[:300], mode, ",".join(tools or []), sources, tokens,
                 duration_ms, outcome),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("تعذّر تسجيل الحدث: %s", e)


def summary(path=DB_PATH) -> dict:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        totals = conn.execute("""
            SELECT COUNT(*) AS questions,
                   COALESCE(SUM(tokens),0) AS total_tokens,
                   COALESCE(ROUND(AVG(duration_ms)),0) AS avg_ms,
                   COALESCE(MAX(duration_ms),0) AS max_ms,
                   SUM(CASE WHEN outcome='refused' THEN 1 ELSE 0 END) AS refused,
                   SUM(CASE WHEN outcome='blocked' THEN 1 ELSE 0 END) AS blocked,
                   SUM(CASE WHEN outcome='error' THEN 1 ELSE 0 END) AS errors
            FROM events WHERE kind='question'
        """).fetchone()
        by_mode = conn.execute("""
            SELECT mode, COUNT(*) AS n FROM events
            WHERE kind='question' AND mode!='' GROUP BY mode
        """).fetchall()
        tool_rows = conn.execute(
            "SELECT tools FROM events WHERE kind='question' AND tools!=''").fetchall()
        uploads = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind='upload'").fetchone()["n"]
        recent = conn.execute("""
            SELECT ts, question, mode, tools, tokens, duration_ms, outcome
            FROM events WHERE kind='question' ORDER BY id DESC LIMIT 10
        """).fetchall()
    finally:
        conn.close()

    tool_counts = {}
    for row in tool_rows:
        for tool in row["tools"].split(","):
            if tool:
                tool_counts[tool] = tool_counts.get(tool, 0) + 1

    return {
        "questions": totals["questions"],
        "total_tokens": totals["total_tokens"],
        "avg_ms": int(totals["avg_ms"]),
        "max_ms": int(totals["max_ms"]),
        "refused": totals["refused"] or 0,
        "blocked": totals["blocked"] or 0,
        "errors": totals["errors"] or 0,
        "uploads": uploads,
        "by_mode": {r["mode"]: r["n"] for r in by_mode},
        "by_tool": dict(sorted(tool_counts.items(), key=lambda x: -x[1])),
        "recent": [dict(r) for r in recent],
    }


def reset(path=DB_PATH) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM events")
        conn.commit()
    finally:
        conn.close()
