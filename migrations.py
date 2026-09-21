"""هجرات مخطّط قاعدة البيانات.

المشكلة: `build_db.py` يحذف القاعدة ويعيد بناءها. أي جدول جديد
كان يعني فقدان الطلبات والإحصاءات والحسابات.

الحل: هجرات تراكمية تُطبَّق مرة واحدة وتُسجَّل. الإقلاع يطبّق
الناقص فقط — ولا يمسّ ما هو موجود.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from logging_config import get_logger

log = get_logger(__name__)

DB_PATH = Path("company.db")


def _db(path=None) -> Path:
    """يحلّ مسار القاعدة عند الاستدعاء لا عند الاستيراد.

    ربطه كقيمة افتراضية يعني أن إعادة تعيين DB_PATH بعد الاستيراد
    بلا أثر — فتعمل الوحدة على قاعدة غير المقصودة بصمت.
    """
    return Path(path) if path else DB_PATH


def _applied(conn) -> set[str]:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name       TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    return {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}


def _has_column(conn, table: str, column: str) -> bool:
    try:
        return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return False


# ---------- الهجرات ----------
# كل واحدة idempotent: تفحص قبل أن تغيّر، فإعادة التطبيق آمنة.

def m001_core_tables(conn) -> None:
    """الجداول الأساسية — تُنشأ إن غابت فقط."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            employee_id  TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            department   TEXT NOT NULL,
            job_title    TEXT NOT NULL,
            annual_leave INTEGER NOT NULL,
            leave_used   INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS departments (
            name                TEXT PRIMARY KEY,
            headcount           INTEGER NOT NULL,
            transport_allowance INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leave_requests (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id   TEXT NOT NULL,
            employee_name TEXT NOT NULL,
            start_date    TEXT NOT NULL,
            end_date      TEXT NOT NULL,
            days          INTEGER NOT NULL,
            reason        TEXT DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username      TEXT PRIMARY KEY,
            employee_id   TEXT NOT NULL,
            employee_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'employee',
            created_at    TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            kind        TEXT NOT NULL,
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


def m002_event_actor(conn) -> None:
    """هوية الفاعل ومعرّف الطلب في الأحداث.

    كان الجدول يسجّل أن سؤالاً طُرح ولا يعرف من طرحه —
    فدورة التدقيق ناقصة عند آخر خطوة.
    """
    for col, ddl in [
        ("actor_id",   "ALTER TABLE events ADD COLUMN actor_id TEXT"),
        ("actor_role", "ALTER TABLE events ADD COLUMN actor_role TEXT"),
        ("request_id", "ALTER TABLE events ADD COLUMN request_id TEXT"),
    ]:
        if not _has_column(conn, "events", col):
            conn.execute(ddl)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_actor ON events(actor_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_req ON events(request_id)")


def m003_leave_indexes(conn) -> None:
    """فهارس الاستعلامات المتكررة — التصفية بالمعرّف والحالة."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leave_emp ON leave_requests(employee_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leave_status ON leave_requests(status)")


def m004_decision_fields(conn) -> None:
    """يفصل ملاحظة القرار عن سبب الموظف.

    كانت المراجعة تكتب ملاحظة المدير فوق عمود `reason` نفسه،
    فيضيع السبب الذي كتبه الموظف عند تقديم الطلب — ولا يمكن
    تمييز "سبب الطلب" عن "سبب الرفض".
    """
    for col, ddl in [
        ("decision_note", "ALTER TABLE leave_requests ADD COLUMN decision_note TEXT DEFAULT ''"),
        ("decided_at",    "ALTER TABLE leave_requests ADD COLUMN decided_at TEXT"),
        ("decided_by",    "ALTER TABLE leave_requests ADD COLUMN decided_by TEXT"),
    ]:
        if not _has_column(conn, "leave_requests", col):
            conn.execute(ddl)


def m005_organization_structure(conn) -> None:
    """بنية تنظيمية: معرّف للقسم، ورابط المدير، وحقول الموظف الأساسية.

    إضافية بالكامل: لا عمود يُحذف، ولا نوع يتغيّر، ولا صف يُمسّ.

    لماذا معرّف للقسم؟ الجدول مفتاحه اسم القسم العربي، فإعادة
    تسمية قسم تكسر كل مرجع إليه. نضيف معرّفاً مستقراً ونُبقي
    عمود الاسم كما هو — فكل استعلام قائم يواصل العمل.

    manager_id على employees هو العلاقة، و users.role يبقى
    طبقة الصلاحية. سؤالان مختلفان لا يتشاركان عموداً.
    """
    # --- الأقسام ---
    for col, ddl in [
        ("department_id", "ALTER TABLE departments ADD COLUMN department_id TEXT"),
        ("manager_id",    "ALTER TABLE departments ADD COLUMN manager_id TEXT"),
        ("is_active",     "ALTER TABLE departments ADD COLUMN is_active INTEGER DEFAULT 1"),
    ]:
        if not _has_column(conn, "departments", col):
            conn.execute(ddl)

    # --- الموظفون ---
    for col, ddl in [
        ("department_id",     "ALTER TABLE employees ADD COLUMN department_id TEXT"),
        ("manager_id",        "ALTER TABLE employees ADD COLUMN manager_id TEXT"),
        ("email",             "ALTER TABLE employees ADD COLUMN email TEXT"),
        ("employment_status", "ALTER TABLE employees ADD COLUMN employment_status TEXT DEFAULT 'active'"),
        ("hire_date",         "ALTER TABLE employees ADD COLUMN hire_date TEXT"),
        ("created_at",        "ALTER TABLE employees ADD COLUMN created_at TEXT"),
    ]:
        if not _has_column(conn, "employees", col):
            conn.execute(ddl)

    # --- تعبئة معرّفات الأقسام ---
    # ترقيم متسلسل بترتيب الاسم: حتمي على أي جهاز بنفس البيانات،
    # ويُنفَّذ مرة واحدة فقط لأن الهجرة تُسجَّل.
    missing = conn.execute(
        "SELECT name FROM departments WHERE department_id IS NULL OR department_id = ''"
        " ORDER BY name"
    ).fetchall()

    if missing:
        taken = {
            r[0] for r in conn.execute(
                "SELECT department_id FROM departments"
                " WHERE department_id IS NOT NULL AND department_id != ''"
            )
        }
        counter = 1
        for (name,) in missing:
            while f"DEPT-{counter:03d}" in taken:
                counter += 1
            dept_id = f"DEPT-{counter:03d}"
            taken.add(dept_id)
            conn.execute(
                "UPDATE departments SET department_id = ? WHERE name = ?", (dept_id, name)
            )
            counter += 1

    # --- ربط الموظفين بأقسامهم عبر الاسم القائم ---
    conn.execute("""
        UPDATE employees
           SET department_id = (
                 SELECT d.department_id FROM departments d WHERE d.name = employees.department
               )
         WHERE (department_id IS NULL OR department_id = '')
           AND EXISTS (SELECT 1 FROM departments d WHERE d.name = employees.department)
    """)

    # --- الحالة الوظيفية للصفوف القديمة ---
    conn.execute(
        "UPDATE employees SET employment_status = 'active'"
        " WHERE employment_status IS NULL OR employment_status = ''"
    )

    # ملاحظة: created_at و hire_date و email تبقى NULL للصفوف القديمة.
    # تاريخ إنشاء غير معروف؛ اختلاق واحد أسوأ من غيابه.

    # --- الفهارس ---
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_dept_id ON departments(department_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emp_dept ON employees(department_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emp_manager ON employees(manager_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emp_status ON employees(employment_status)")


MIGRATIONS = [
    ("001_core_tables",   m001_core_tables),
    ("002_event_actor",   m002_event_actor),
    ("003_leave_indexes", m003_leave_indexes),
    ("004_decision_fields", m004_decision_fields),
    ("005_organization_structure", m005_organization_structure),
]


def migrate(path=None) -> list[str]:
    """يطبّق الهجرات الناقصة. آمن للتشغيل المتكرر. لا يحذف بيانات."""
    conn = sqlite3.connect(_db(path))
    applied_now = []
    try:
        done = _applied(conn)
        for name, fn in MIGRATIONS:
            if name in done:
                continue
            fn(conn)
            conn.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (?,?)",
                (name, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            applied_now.append(name)
        conn.commit()
    finally:
        conn.close()

    if applied_now:
        log.info("طُبّقت هجرات: %s", ", ".join(applied_now))
    return applied_now


def status(path=None) -> dict:
    conn = sqlite3.connect(_db(path))
    try:
        done = _applied(conn)
        conn.commit()
    finally:
        conn.close()
    return {
        "applied": sorted(done),
        "pending": [n for n, _ in MIGRATIONS if n not in done],
    }


if __name__ == "__main__":
    applied = migrate()
    print("طُبّق:", applied or "لا شيء — المخطّط محدّث")
    print(status())


def m005_organization_structure_backfill(path=None) -> None:
    """يعيد تشغيل تعبئة 005 وحدها.

    الهجرة تُسجَّل فلا تُعاد، لكن الزرع يدرج صفوفاً بعدها —
    فتحتاج الصفوف الجديدة ربطاً بأقسامها.
    """
    conn = None
    try:
        conn = sqlite3.connect(_db(path))
        if not _has_column(conn, "employees", "department_id"):
            return
        m005_organization_structure(conn)
        conn.commit()
    except Exception as e:
        log.warning("تعذّرت إعادة تعبئة البنية التنظيمية: %s", e)
    finally:
        if conn is not None:
            conn.close()


# ---------- فحص سلامة العلاقات ----------
#
# لا مفاتيح أجنبية في SQLite هنا: إضافتها لجدول قائم تتطلب إعادة
# بناءه كاملاً، وهي العملية التدميرية التي أُزيلت في المرحلة الثانية.
# البديل: قواعد تُفرَض في طبقة الخدمة (org.py) ويحرسها هذا الفحص.

def validate_integrity(path=None) -> dict:
    """يفحص العلاقات ويعيد ما وجده. لا يعدّل شيئاً."""
    conn = None
    try:
        conn = sqlite3.connect(_db(path))
        conn.row_factory = sqlite3.Row

        orphan_dept = [
            dict(r) for r in conn.execute("""
                SELECT employee_id, name, department, department_id
                  FROM employees
                 WHERE department_id IS NOT NULL AND department_id != ''
                   AND department_id NOT IN (
                         SELECT department_id FROM departments
                          WHERE department_id IS NOT NULL)
            """)
        ]

        orphan_manager = [
            dict(r) for r in conn.execute("""
                SELECT employee_id, name, manager_id
                  FROM employees
                 WHERE manager_id IS NOT NULL AND manager_id != ''
                   AND manager_id NOT IN (SELECT employee_id FROM employees)
            """)
        ]

        self_manager = [
            dict(r) for r in conn.execute("""
                SELECT employee_id, name FROM employees
                 WHERE manager_id IS NOT NULL AND manager_id = employee_id
            """)
        ]

        unlinked = [
            dict(r) for r in conn.execute("""
                SELECT employee_id, name, department FROM employees
                 WHERE department_id IS NULL OR department_id = ''
            """)
        ]

        dept_manager = [
            dict(r) for r in conn.execute("""
                SELECT name, manager_id FROM departments
                 WHERE manager_id IS NOT NULL AND manager_id != ''
                   AND manager_id NOT IN (SELECT employee_id FROM employees)
            """)
        ]

        return {
            "orphan_department_refs": orphan_dept,
            "orphan_manager_refs": orphan_manager,
            "self_managers": self_manager,
            "employees_without_department": unlinked,
            "orphan_department_managers": dept_manager,
            "ok": not (orphan_dept or orphan_manager or self_manager or dept_manager),
        }
    except Exception as e:
        log.warning("تعذّر فحص سلامة العلاقات: %s", e)
        return {"ok": False, "error": str(e)}
    finally:
        if conn is not None:
            conn.close()
