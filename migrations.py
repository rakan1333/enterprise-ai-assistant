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


class MigrationConflict(RuntimeError):
    """بيانات قائمة تمنع تطبيق هجرة.

    تُميَّز عن أخطاء القاعدة ليعرف المستدعي أن العلّة في البيانات لا
    في المخطّط، وأن المعالجة قرار إنسان لا إعادة محاولة.
    """


def _ensure_migrations_table(conn) -> None:
    """ينشئ سجلّ الهجرات. يُستدعى قبل المعاملة الذرّية — انظر migrate."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name       TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)


def _applied(conn) -> set[str]:
    _ensure_migrations_table(conn)
    return {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}


def _has_table(conn, table: str) -> bool:
    """هل الجدول موجود؟ تُستخدم لفحوص تخصّ هجرة قد لا تكون طُبّقت."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


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


def m006_attendance(conn) -> None:
    """الحضور بمواقع ميدانية متعدّدة.

    إضافية بالكامل: ثلاثة جداول جديدة، ولا عمود قائم يُمسّ.

    أهم قرار هنا فصل الأعمدة بمصدرها:

        in_reported_*    ← ما أبلغ به جهاز الموظف
        in_computed_*    ← ما حسبه الخادم
        in_verdict       ← ما قرّره الخادم

    دمجها في حقل واحد يُفقد القدرة على الإجابة عند النزاع: ماذا
    ادّعى الجهاز، وماذا استنتج الخادم منه؟ وبدون هذا التمييز
    يصبح سجلّ الحضور رواية الجهاز لا حكم النظام.

    attendance_records يحفظ كل محاولة بحكمها، لا الجلسات المقبولة
    فقط. محاولة مرفوضة خارج النطاق تبقى مرئية ليقدّم صاحبها طلب
    تصحيح، وليرى المسؤول نمطاً إن تكرّر.
    """
    # عبارات مفردة لا executescript: الأخير ينفّذ COMMIT ضمنياً
    # فيكسر ذرّية الدفعة التي يضمنها migrate.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sites (
            site_id       TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            latitude      REAL NOT NULL,
            longitude     REAL NOT NULL,
            radius_m      INTEGER NOT NULL DEFAULT 150,
            department_id TEXT,
            is_active     INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS attendance_records (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id   TEXT NOT NULL,
            site_id       TEXT,

            check_in_at   TEXT NOT NULL,
            check_out_at  TEXT,

            -- ما أبلغ به الجهاز
            in_reported_lat        REAL,
            in_reported_lon        REAL,
            in_reported_accuracy_m REAL,
            out_reported_lat        REAL,
            out_reported_lon        REAL,
            out_reported_accuracy_m REAL,

            -- ما حسبه الخادم
            in_computed_distance_m  REAL,
            in_verdict              TEXT,
            in_verdict_reason       TEXT,
            out_computed_distance_m REAL,
            out_verdict             TEXT,
            out_verdict_reason      TEXT,

            in_request_ip  TEXT,
            out_request_ip TEXT,
            created_at     TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS attendance_corrections (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id      TEXT NOT NULL,
            record_id        INTEGER,
            kind             TEXT NOT NULL,
            requested_change TEXT NOT NULL DEFAULT '',
            reason           TEXT NOT NULL DEFAULT '',
            status           TEXT NOT NULL DEFAULT 'pending',
            reviewed_by      TEXT,
            decision_note    TEXT DEFAULT '',
            decided_at       TEXT,
            created_at       TEXT
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_site_active ON sites(is_active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_att_emp_in"
                 " ON attendance_records(employee_id, check_in_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_att_open"
                 " ON attendance_records(employee_id, check_out_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_corr_status"
                 " ON attendance_corrections(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_corr_employee"
                 " ON attendance_corrections(employee_id)")


# ===================== مفردات الأحكام =====================
#
# قيمة خارج هذه المفردات لا تدخل القاعدة بعد 007. والمفردات
# مفصولة بالعمود: الإضافة اليدوية ليست إغلاقاً، والإغلاق الإداري
# ليس حكم سياج — قائمة واحدة للعمودين تقبل خلطاً بلا معنى.

IN_VERDICTS = ("inside", "outside", "rejected", "manually_added")
OUT_VERDICTS = ("inside", "outside", "rejected",
                "correction_closed", "conflict_closed")
CORRECTION_STATUSES = ("pending", "approved", "rejected")


def _quoted(values) -> str:
    return ", ".join("'" + v + "'" for v in values)


def _vocabulary_triggers(conn) -> None:
    """مُطلِقات تحرس المفردات والاتّساق.

    لماذا مُطلِقات لا CHECK؟ لأن SQLite لا يضيف قيد CHECK لجدول قائم
    إلا بإعادة بنائه، وإعادة البناء ممنوعة في كل هجرة هنا. والمُطلِق
    إضافي بحت، ويُسقَط بـDROP فيعود الحال تماماً.

    ولماذا `IS NOT OLD` في مُطلِقات التحديث؟ لأن فحص NEW وحده يُسقط
    الشرط على كل تحديث لصفّ قديم غير مطابق — ولو كان التحديث على
    عمود لا علاقة له. فيتجمّد الصفّ: لا يُصلَح ولا يُمسّ. والحارس
    يجعل الفحص عند التغيير وحده.

    والصفوف القائمة لا تُفحص: المُطلِقات BEFORE INSERT/UPDATE فقط،
    فقيمة قديمة خارج المفردات تبقى وتُقرأ، ويُرفض كتابة مثلها.
    """
    IN_, OUT_, ST_ = _quoted(IN_VERDICTS), _quoted(OUT_VERDICTS), _quoted(CORRECTION_STATUSES)

    for name in ("trg_att_in_verdict_insert", "trg_att_in_verdict_update",
                 "trg_att_out_verdict_insert", "trg_att_out_verdict_update",
                 "trg_att_closure_pairing_insert", "trg_att_closure_pairing_update",
                 "trg_att_no_reopen",
                 "trg_corr_status_insert", "trg_corr_status_update"):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")

    # in_verdict: إلزامي. كل صفّ له check_in_at، فله حكم.
    conn.execute(f"""
        CREATE TRIGGER trg_att_in_verdict_insert BEFORE INSERT ON attendance_records
          WHEN NEW.in_verdict IS NULL OR NEW.in_verdict NOT IN ({IN_})
          BEGIN SELECT RAISE(ABORT, 'in_verdict ناقص أو غير معروف'); END
    """)
    conn.execute(f"""
        CREATE TRIGGER trg_att_in_verdict_update BEFORE UPDATE ON attendance_records
          WHEN NEW.in_verdict IS NOT OLD.in_verdict
           AND (NEW.in_verdict IS NULL OR NEW.in_verdict NOT IN ({IN_}))
          BEGIN SELECT RAISE(ABORT, 'in_verdict ناقص أو غير معروف'); END
    """)

    # out_verdict: NULL مشروعة — تعني أنه لم ينصرف بعد.
    conn.execute(f"""
        CREATE TRIGGER trg_att_out_verdict_insert BEFORE INSERT ON attendance_records
          WHEN NEW.out_verdict IS NOT NULL AND NEW.out_verdict NOT IN ({OUT_})
          BEGIN SELECT RAISE(ABORT, 'out_verdict غير معروف'); END
    """)
    conn.execute(f"""
        CREATE TRIGGER trg_att_out_verdict_update BEFORE UPDATE ON attendance_records
          WHEN NEW.out_verdict IS NOT OLD.out_verdict
           AND NEW.out_verdict IS NOT NULL AND NEW.out_verdict NOT IN ({OUT_})
          BEGIN SELECT RAISE(ABORT, 'out_verdict غير معروف'); END
    """)

    # الاقتران: وقت الانصراف وحكمه يُكتبان معاً أو يُتركان معاً.
    # المطلوب أن تكون النتيجة متّسقة، لا أن يُكتب العمودان معاً:
    # صفّ ناقصه الحكم وحده يُصلَح بكتابة الحكم وحده.
    PAIRING = "(NEW.check_out_at IS NULL) IS NOT (NEW.out_verdict IS NULL)"
    conn.execute(f"""
        CREATE TRIGGER trg_att_closure_pairing_insert BEFORE INSERT ON attendance_records
          WHEN {PAIRING}
          BEGIN SELECT RAISE(ABORT, 'وقت الانصراف وحكمه يُكتبان معاً أو يُتركان معاً'); END
    """)
    conn.execute(f"""
        CREATE TRIGGER trg_att_closure_pairing_update BEFORE UPDATE ON attendance_records
          WHEN (NEW.check_out_at IS NOT OLD.check_out_at
                OR NEW.out_verdict IS NOT OLD.out_verdict)
           AND {PAIRING}
          BEGIN SELECT RAISE(ABORT, 'وقت الانصراف وحكمه يُكتبان معاً أو يُتركان معاً'); END
    """)

    # منع إعادة الفتح: الاقتران وحده يسمح بإفراغ الطرفين معاً، وهو
    # إعادة فتح جلسة مغلقة. منطق التطبيق يمنعها، وهذا حزام ثانٍ
    # يغلق الطريق على SQL المباشرة. الشرط على OLD فلا يمسّ الإغلاق.
    conn.execute("""
        CREATE TRIGGER trg_att_no_reopen BEFORE UPDATE ON attendance_records
          WHEN OLD.check_out_at IS NOT NULL AND NEW.check_out_at IS NULL
          BEGIN SELECT RAISE(ABORT, 'لا يمكن إعادة فتح جلسة مغلقة'); END
    """)

    conn.execute(f"""
        CREATE TRIGGER trg_corr_status_insert BEFORE INSERT ON attendance_corrections
          WHEN NEW.status IS NULL OR NEW.status NOT IN ({ST_})
          BEGIN SELECT RAISE(ABORT, 'حالة طلب التصحيح غير معروفة'); END
    """)
    conn.execute(f"""
        CREATE TRIGGER trg_corr_status_update BEFORE UPDATE ON attendance_corrections
          WHEN NEW.status IS NOT OLD.status
           AND (NEW.status IS NULL OR NEW.status NOT IN ({ST_}))
          BEGIN SELECT RAISE(ABORT, 'حالة طلب التصحيح غير معروفة'); END
    """)


def m007_attendance_integrity(conn) -> None:
    """يُحكم سلامة الحضور: أعمدة التدقيق، والمفردات، وجلسة واحدة مفتوحة.

    الترتيب مقصود:
      1) الأعمدة  — قبل كل شيء، فالفهرس والمُطلِقات تُبنى على مخطّط تامّ
      2) المُطلِقات — لا تعتمد على نظافة البيانات فتُنشأ قبل الفحص
      3) تقرير القيم المجهولة — إبلاغ لا تعديل
      4) فحص التعارض — يوقف الهجرة إن وُجدت جلستان مفتوحتان لموظف
      5) الفهرس الفريد — آخر شيء: عقد لا يُفرض إلا على بيانات صالحة

    الجلسات المتعارضة **لا تُغلق آلياً**. وقت الانصراف لا يُختلق،
    ولو اختُلق لفسد السجل الذي من أجل نزاهته بُني النظام. تُكشف
    وتُسمّى، ويعالجها إنسان عبر attendance.resolve_duplicate_session.
    """
    # المعاملة ملك migrate دائماً. والشرط احتياط لمن يستدعي الهجرة
    # مباشرةً في اختبار: من يفتح هو من يغلق.
    opened = False
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            opened = True

        # --- 1) الأعمدة ---
        for table, column, ddl in (
            ("attendance_records", "original_check_in_at",
             "ALTER TABLE attendance_records ADD COLUMN original_check_in_at TEXT"),
            ("attendance_records", "original_check_out_at",
             "ALTER TABLE attendance_records ADD COLUMN original_check_out_at TEXT"),
            ("attendance_records", "corrected_by_correction_id",
             "ALTER TABLE attendance_records ADD COLUMN corrected_by_correction_id INTEGER"),
            # إقرار المشغّل بهويته، لا هوية متحقَّقة. لا آلية تحقّق في
            # هذه الطبقة؛ ومسار مصادَق عليه لاحقاً يكتب في عمود آخر.
            ("attendance_records", "resolved_by_claimed",
             "ALTER TABLE attendance_records ADD COLUMN resolved_by_claimed TEXT"),
            ("attendance_records", "resolved_at",
             "ALTER TABLE attendance_records ADD COLUMN resolved_at TEXT"),
            ("attendance_corrections", "requested_check_in_at",
             "ALTER TABLE attendance_corrections ADD COLUMN requested_check_in_at TEXT"),
            ("attendance_corrections", "requested_check_out_at",
             "ALTER TABLE attendance_corrections ADD COLUMN requested_check_out_at TEXT"),
        ):
            if not _has_column(conn, table, column):
                conn.execute(ddl)

        # --- 2) المُطلِقات ---
        _vocabulary_triggers(conn)

        # --- 3) تقرير القيم المجهولة: إبلاغ لا تعديل ---
        unknown = conn.execute(f"""
            SELECT 'in_verdict' AS col, in_verdict AS value, COUNT(*) AS n
              FROM attendance_records
             WHERE in_verdict IS NOT NULL AND in_verdict NOT IN ({_quoted(IN_VERDICTS)})
             GROUP BY in_verdict
            UNION ALL
            SELECT 'out_verdict', out_verdict, COUNT(*)
              FROM attendance_records
             WHERE out_verdict IS NOT NULL AND out_verdict NOT IN ({_quoted(OUT_VERDICTS)})
             GROUP BY out_verdict
        """).fetchall()
        if unknown:
            log.warning(
                "قيم حكم خارج المفردات في صفوف قائمة — تُقرأ ولا تُكتب مثلها: %s",
                "، ".join(f"{c}={v!r} ({n})" for c, v, n in unknown))

        # --- 4) فحص التعارض ---
        dupes = conn.execute("""
            SELECT employee_id, COUNT(*) AS n, GROUP_CONCAT(id) AS ids
              FROM attendance_records
             WHERE in_verdict = 'inside' AND check_out_at IS NULL
             GROUP BY employee_id HAVING n > 1
        """).fetchall()
        if dupes:
            detail = " · ".join(f"{e}: {n} جلسات (سجلات {ids})" for e, n, ids in dupes)
            raise MigrationConflict(
                "تعذّر تطبيق 007: جلسات مفتوحة متعدّدة لموظف واحد.\n"
                f"{detail}\n"
                "لن تُغلق آلياً — وقت الانصراف لا يُختلق. افحصها بـ"
                "attendance.inspect_duplicate_sessions ثم عالج كل زائدة بـ"
                "attendance.resolve_duplicate_session، وأعد التشغيل.")

        # --- 5) الفهرس ---
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_att_one_open
                ON attendance_records(employee_id)
             WHERE check_out_at IS NULL AND in_verdict = 'inside'
        """)

        if opened:
            conn.execute("COMMIT")
    except Exception:
        if opened and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


MIGRATIONS = [
    ("001_core_tables",   m001_core_tables),
    ("002_event_actor",   m002_event_actor),
    ("003_leave_indexes", m003_leave_indexes),
    ("004_decision_fields", m004_decision_fields),
    ("005_organization_structure", m005_organization_structure),
    ("006_attendance", m006_attendance),
    ("007_attendance_integrity", m007_attendance_integrity),
]


def migrate(path=None) -> list[str]:
    """يطبّق الهجرات الناقصة. آمن للتشغيل المتكرر. لا يحذف بيانات.

    الدفعة ذرّية: كل الهجرات الناقصة تُعتمد معاً أو لا تُعتمد أيّها.
    فشل هجرة متأخّرة يُرجع ما قبلها في التشغيل نفسه.

    كان هذا الضمان مكسوراً حتى اكتُشف سببه: `executescript` في
    sqlite3 ينفّذ COMMIT ضمنياً قبل نصّه، فكان DDL الهجرة السابقة
    يُثبَّت قبل أن تفشل التالية. أُزيل آخر استعمال له من m006.

    حدّ واحد خارج الذرّية، مقصود وموثَّق: جدول `schema_migrations`
    يُنشأ قبل المعاملة، لأن قائمة المطبَّق تُقرأ من داخلها فيلزم
    وجوده. فلو فشلت الدفعة على قاعدة جديدة بقي الجدول فارغاً —
    وجدول خالٍ لا يحمل بياناً ولا يغيّر سلوكاً، والتشغيل التالي
    يستعمله كما هو.
    """
    conn = sqlite3.connect(_db(path), isolation_level=None)
    applied_now = []
    try:
        _ensure_migrations_table(conn)      # خارج المعاملة — الحدّ أعلاه
        conn.execute("BEGIN IMMEDIATE")     # قفل كتابة فوري يُسلسل المُرحِّلين

        # القائمة تُقرأ داخل القفل لا قبله: عمليتان تبدآن معاً كانتا
        # تقرآن القائمة نفسها فتحاول كلتاهما تطبيق الهجرة ذاتها.
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

        conn.execute("COMMIT")
    except Exception:
        # مالك المعاملة هو من يتراجع عنها، صراحةً لا اعتماداً على الإغلاق.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
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

        # --- الحضور (006) ---
        # القاعدة قد تسبق هذه الهجرة، فنفحص وجود الجدول أولاً بدل
        # أن يسقط الفحص كله على قاعدة قديمة.
        orphan_site = []
        orphan_att_emp = []
        if _has_table(conn, "attendance_records"):
            orphan_site = [
                dict(r) for r in conn.execute("""
                    SELECT id, employee_id, site_id FROM attendance_records
                     WHERE site_id IS NOT NULL AND site_id != ''
                       AND site_id NOT IN (SELECT site_id FROM sites)
                """)
            ] if _has_table(conn, "sites") else []

            orphan_att_emp = [
                dict(r) for r in conn.execute("""
                    SELECT id, employee_id FROM attendance_records
                     WHERE employee_id NOT IN (SELECT employee_id FROM employees)
                """)
            ]

        # قيم حكم خارج المفردات — تُكشف ولا تُصلَح. المُطلِقات تمنع
        # كتابة مثلها، لكن ما سبقها يبقى حتى يُصحَّح بقرار صريح.
        unknown_verdicts = []
        if _has_table(conn, "attendance_records"):
            unknown_verdicts = [
                dict(r) for r in conn.execute(f"""
                    SELECT id, 'in_verdict' AS column_name, in_verdict AS value
                      FROM attendance_records
                     WHERE in_verdict IS NULL
                        OR in_verdict NOT IN ({_quoted(IN_VERDICTS)})
                    UNION ALL
                    SELECT id, 'out_verdict', out_verdict
                      FROM attendance_records
                     WHERE out_verdict IS NOT NULL
                       AND out_verdict NOT IN ({_quoted(OUT_VERDICTS)})
                """)
            ]

        # صفوف مغلقة بلا حكم أو العكس — يمنعها مُطلِق الاقتران الآن،
        # وما سبقه يُكشف هنا.
        inconsistent_closures = []
        if _has_table(conn, "attendance_records"):
            inconsistent_closures = [
                dict(r) for r in conn.execute("""
                    SELECT id, employee_id, check_out_at, out_verdict
                      FROM attendance_records
                     WHERE (check_out_at IS NULL) != (out_verdict IS NULL)
                """)
            ]

        return {
            "orphan_department_refs": orphan_dept,
            "orphan_manager_refs": orphan_manager,
            "self_managers": self_manager,
            "employees_without_department": unlinked,
            "orphan_department_managers": dept_manager,
            "orphan_attendance_sites": orphan_site,
            "orphan_attendance_employees": orphan_att_emp,
            "unknown_verdicts": unknown_verdicts,
            "inconsistent_closures": inconsistent_closures,
            "ok": not (orphan_dept or orphan_manager or self_manager or dept_manager
                       or orphan_site or orphan_att_emp
                       or unknown_verdicts or inconsistent_closures),
        }
    except Exception as e:
        log.warning("تعذّر فحص سلامة العلاقات: %s", e)
        return {"ok": False, "error": str(e)}
    finally:
        if conn is not None:
            conn.close()
