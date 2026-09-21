"""تسجيل أحداث الاستخدام وتلخيصها."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from logging_config import get_logger

log = get_logger(__name__)
DB_PATH = Path("company.db")


def _db(path=None) -> Path:
    """يحلّ مسار القاعدة عند الاستدعاء لا عند الاستيراد.

    ربطه كقيمة افتراضية يعني أن إعادة تعيين DB_PATH بلا أثر —
    فتكتب الاختبارات في قاعدة التطوير بصمت.
    """
    return Path(path) if path else DB_PATH


def init_db(path=None) -> None:
    """يفوّض للهجرات — مصدر واحد للمخطّط.

    يُبقي إنشاءً احتياطياً للجدول لتوافق الاستدعاءات القديمة.
    """
    try:
        import migrations
        migrations.migrate(_db(path))
        return
    except Exception as e:
        log.warning("تعذّر تطبيق الهجرات (%s) — إنشاء احتياطي.", e)

    conn = sqlite3.connect(_db(path))
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
           duration_ms=0, outcome="ok", actor=None, request_id="", path=None) -> None:
    """يسجّل حدثاً مع هوية الفاعل ومعرّف الطلب.

    actor: قاموس الهوية من الرمز. نخزّن المعرّف والدور فقط —
    لا الاسم ولا اسم الدخول: المعرّف يكفي للتدقيق، والباقي بيانات
    شخصية بلا حاجة.

    لا يرمي استثناءً أبداً — القياس لا يُسقط ما يقيسه.
    """
    actor_id = actor_role = ""
    if actor:
        actor_id = str(actor.get("employee_id") or actor.get("sub") or "")[:40]
        actor_role = str(actor.get("role") or "")[:20]

    try:
        conn = sqlite3.connect(_db(path))
        try:
            conn.execute(
                """INSERT INTO events
                   (ts, kind, question, mode, tools, sources, tokens, duration_ms,
                    outcome, actor_id, actor_role, request_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), kind,
                 question[:300], mode, ",".join(tools or []), sources, tokens,
                 duration_ms, outcome, actor_id, actor_role, str(request_id)[:40]),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("تعذّر تسجيل الحدث: %s", e)


def summary(path=None) -> dict:
    conn = sqlite3.connect(_db(path))
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
            SELECT ts, question, mode, tools, tokens, duration_ms, outcome,
                   COALESCE(actor_id,'') AS actor_id,
                   COALESCE(actor_role,'') AS actor_role,
                   COALESCE(request_id,'') AS request_id
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


def reset(path=None) -> None:
    conn = sqlite3.connect(_db(path))
    try:
        conn.execute("DELETE FROM events")
        conn.commit()
    finally:
        conn.close()


# ---------- تصنيف نتيجة الإجابة ----------

# عبارات يستخدمها النموذج عند الاعتراف بعدم المعرفة.
# مصدرها SYSTEM_PROMPT، فهي متوقّعة لا مخمّنة.
_REFUSAL_MARKERS = (
    "لا تحتوي",
    "لا توجد معلومات",
    "لا توجد مقاطع",
    "المستندات المتوفرة لا",
    "المستندات المتاحة لا",
    "لا يوجد موظف",
    "تعذّر الوصول لإجابة",
    "يرجى تزويدي",
    "أرجو توضيح",
    "يلزم تحديد",
    "تحتاج إلى تحديد",
)


def classify_outcome(answer: str, tools: list[str] | None, sources: int) -> str:
    """يصنّف النتيجة من مخرَج الإجابة لا من مسارها.

    استدعاء أداة ليس نجاحاً — البحث قد يستدعى ولا يجد شيئاً.
    المعيار: هل أنتج النظام إجابة مفيدة فعلاً؟
    """
    text = (answer or "").strip()

    if not text:
        return "error"

    if text.startswith("⚠️"):
        return "error"

    for marker in _REFUSAL_MARKERS:
        if marker in text:
            return "refused"

    if not tools and sources == 0:
        return "refused"

    return "ok"


def audit_trail(actor_id=None, request_id=None, limit=50, path=None) -> list[dict]:
    """سجل التدقيق: من فعل ماذا ومتى.

    بلا نص الأسئلة — التدقيق يحتاج الفعل والفاعل لا المحتوى.
    """
    where, params = ["1=1"], []
    if actor_id:
        where.append("actor_id = ?"); params.append(actor_id)
    if request_id:
        where.append("request_id = ?"); params.append(request_id)

    conn = sqlite3.connect(_db(path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, ts, kind, mode, tools, outcome, "
            "COALESCE(actor_id,'') AS actor_id, COALESCE(actor_role,'') AS actor_role, "
            "COALESCE(request_id,'') AS request_id "
            f"FROM events WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("تعذّر قراءة سجل التدقيق: %s", e)
        return []
    finally:
        conn.close()


def upload_history(path=None) -> dict:
    """آخر حدث رفع ناجح لكل ملف — من سجل الأحداث القائم.

    لا جدول جديد ولا عمود: تاريخ الرفع ومن رفعه مسجّلان أصلاً
    في `events` منذ المرحلة الثانية. نقرأهما بدل تخزينهما مرتين.
    """
    conn = None
    try:
        # الاتصال نفسه داخل الحراسة: قائمة المستندات يجب ألا تسقط
        # لأن قراءة سجل تكميلي فشلت.
        conn = sqlite3.connect(_db(path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT question AS filename, MAX(ts) AS ts,
                      COALESCE(actor_id, '') AS actor_id
               FROM events
               WHERE kind = 'upload' AND outcome = 'added' AND question != ''
               GROUP BY question"""
        ).fetchall()
        return {r["filename"]: {"ts": r["ts"], "actor_id": r["actor_id"]} for r in rows}
    except Exception as e:
        log.warning("تعذّرت قراءة سجل الرفع: %s", e)
        return {}
    finally:
        if conn is not None:
            conn.close()
