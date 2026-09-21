"""الهجرة 005: البنية التنظيمية.

إضافية بالكامل وقابلة لإعادة التشغيل. هذي الاختبارات تحرس
ثلاثة أشياء: ألا تُفقد بيانات، وأن تُملأ المراجع بصدق، وألا
يتغيّر أي سلوك قائم في هذي الخطوة.
"""

import sqlite3

import pytest

import build_db
import migrations


# ---------- مخطّط ما قبل 005 لاختبار الترقية ----------

LEGACY_SCHEMA = """
CREATE TABLE employees (
    employee_id TEXT PRIMARY KEY, name TEXT NOT NULL, department TEXT NOT NULL,
    job_title TEXT NOT NULL, annual_leave INTEGER NOT NULL, leave_used INTEGER NOT NULL);
CREATE TABLE departments (
    name TEXT PRIMARY KEY, headcount INTEGER NOT NULL, transport_allowance INTEGER NOT NULL);
CREATE TABLE leave_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id TEXT NOT NULL,
    employee_name TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
    days INTEGER NOT NULL, reason TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL);
CREATE TABLE users (
    username TEXT PRIMARY KEY, employee_id TEXT NOT NULL, employee_name TEXT NOT NULL,
    password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'employee',
    created_at TEXT NOT NULL);
"""


def legacy_db(path):
    """قاعدة بالمخطّط القديم وفيها بيانات تشغيل حقيقية."""
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.executemany(
        "INSERT INTO employees VALUES (?,?,?,?,?,?)",
        [("EMP-003", "خالد الدوسري", "المالية", "محاسب أول", 30, 22),
         ("EMP-001", "أحمد الشمري", "تقنية المعلومات", "مهندس نظم", 30, 12),
         ("EMP-099", "سعد الغامدي", "قسم أُلغي", "منسّق", 30, 0)],
    )
    conn.executemany("INSERT INTO departments VALUES (?,?,?)",
                     [("المالية", 11, 800), ("تقنية المعلومات", 24, 800)])
    conn.execute(
        """INSERT INTO leave_requests
           (employee_id, employee_name, start_date, end_date, days, reason, status, created_at)
           VALUES ('EMP-003','خالد الدوسري','2026-10-01','2026-10-03',3,'ظروف عائلية',
                   'pending','2026-09-01T08:00:00+00:00')"""
    )
    conn.execute(
        "INSERT INTO users VALUES ('khalid','EMP-003','خالد الدوسري','$2b$hash','employee','2026-09-01')"
    )
    conn.commit()
    conn.close()
    return path


def cols(path, table):
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def rows(path, sql, params=()):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


# ---------- قاعدة جديدة ----------

def test_fresh_database_has_all_new_columns(tmp_path):
    db = tmp_path / "fresh.db"
    migrations.migrate(db)

    assert {"department_id", "manager_id", "is_active"} <= cols(db, "departments")
    assert {"department_id", "manager_id", "email",
            "employment_status", "hire_date", "created_at"} <= cols(db, "employees")


def test_fresh_seed_links_every_employee(tmp_path):
    db = tmp_path / "fresh.db"
    build_db.build(db)

    unlinked = rows(db, "SELECT name FROM employees WHERE department_id IS NULL")
    assert unlinked == []


def test_fresh_seed_assigns_unique_department_ids(tmp_path):
    db = tmp_path / "fresh.db"
    build_db.build(db)

    ids = [r["department_id"] for r in rows(db, "SELECT department_id FROM departments")]
    assert len(ids) == len(set(ids)) == 5
    assert all(i.startswith("DEPT-") for i in ids)


def test_department_ids_are_deterministic(tmp_path):
    """نفس البيانات تعطي نفس المعرّفات على أي جهاز."""
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    build_db.build(first)
    build_db.build(second)

    pair = lambda p: {r["name"]: r["department_id"]
                      for r in rows(p, "SELECT name, department_id FROM departments")}
    assert pair(first) == pair(second)


# ---------- الترقية من المخطّط القائم ----------

def test_upgrade_applies_all_migrations(tmp_path):
    db = legacy_db(tmp_path / "old.db")
    applied = migrations.migrate(db)
    assert "005_organization_structure" in applied


def test_upgrade_preserves_employee_rows(tmp_path):
    db = legacy_db(tmp_path / "old.db")
    before = rows(db, "SELECT employee_id, name, annual_leave, leave_used FROM employees")
    migrations.migrate(db)
    after = rows(db, "SELECT employee_id, name, annual_leave, leave_used FROM employees")
    assert before == after


def test_upgrade_preserves_leave_requests(tmp_path):
    """بيانات سير العمل القائم لا تُمسّ."""
    db = legacy_db(tmp_path / "old.db")
    migrations.migrate(db)

    req = rows(db, "SELECT * FROM leave_requests")[0]
    assert req["employee_id"] == "EMP-003"
    assert req["days"] == 3
    assert req["reason"] == "ظروف عائلية"
    assert req["status"] == "pending"


def test_upgrade_preserves_users_and_hashes(tmp_path):
    db = legacy_db(tmp_path / "old.db")
    migrations.migrate(db)

    user = rows(db, "SELECT * FROM users")[0]
    assert user["username"] == "khalid"
    assert user["password_hash"] == "$2b$hash"
    assert user["role"] == "employee"


def test_upgrade_preserves_department_values(tmp_path):
    db = legacy_db(tmp_path / "old.db")
    migrations.migrate(db)

    d = {r["name"]: r for r in rows(db, "SELECT * FROM departments")}
    assert d["المالية"]["headcount"] == 11
    assert d["المالية"]["transport_allowance"] == 800


def test_upgrade_does_not_drop_columns(tmp_path):
    db = legacy_db(tmp_path / "old.db")
    before = cols(db, "employees")
    migrations.migrate(db)
    assert before <= cols(db, "employees")


# ---------- التعبئة ----------

def test_backfill_links_employees_by_department_name(tmp_path):
    db = legacy_db(tmp_path / "old.db")
    migrations.migrate(db)

    by_name = {r["name"]: r for r in rows(
        db, "SELECT e.name, e.department, e.department_id, d.name AS dept_name"
            "  FROM employees e LEFT JOIN departments d"
            "    ON d.department_id = e.department_id")}

    assert by_name["خالد الدوسري"]["dept_name"] == "المالية"
    assert by_name["أحمد الشمري"]["dept_name"] == "تقنية المعلومات"


def test_backfill_leaves_unmatched_department_null(tmp_path):
    """قسم لا وجود له لا يُخترَع له معرّف — يبقى فارغاً ويُبلَّغ عنه."""
    db = legacy_db(tmp_path / "old.db")
    migrations.migrate(db)

    saad = rows(db, "SELECT department_id FROM employees WHERE employee_id='EMP-099'")[0]
    assert saad["department_id"] is None

    report = migrations.validate_integrity(db)
    assert any(e["employee_id"] == "EMP-099"
               for e in report["employees_without_department"])


def test_backfill_keeps_department_name_column(tmp_path):
    """التوافق الخلفي: الاستعلامات القائمة تقرأ الاسم."""
    db = legacy_db(tmp_path / "old.db")
    migrations.migrate(db)
    assert rows(db, "SELECT department FROM employees WHERE employee_id='EMP-003'"
                )[0]["department"] == "المالية"


def test_employment_status_defaults_to_active(tmp_path):
    db = legacy_db(tmp_path / "old.db")
    migrations.migrate(db)
    statuses = {r["employment_status"] for r in rows(db, "SELECT employment_status FROM employees")}
    assert statuses == {"active"}


def test_unknown_fields_stay_null_not_invented(tmp_path):
    """تاريخ توظيف أو إنشاء مختلَق أسوأ من غيابه."""
    db = legacy_db(tmp_path / "old.db")
    migrations.migrate(db)

    r = rows(db, "SELECT hire_date, created_at, email, manager_id FROM employees")[0]
    assert r["hire_date"] is None
    assert r["created_at"] is None
    assert r["email"] is None
    assert r["manager_id"] is None


# ---------- إعادة التشغيل ----------

def test_migration_is_idempotent(tmp_path):
    db = legacy_db(tmp_path / "old.db")
    first = migrations.migrate(db)
    second = migrations.migrate(db)
    third = migrations.migrate(db)

    assert "005_organization_structure" in first
    assert second == [] and third == []


def test_repeated_migration_does_not_change_data(tmp_path):
    db = legacy_db(tmp_path / "old.db")
    migrations.migrate(db)
    snapshot = rows(db, "SELECT * FROM employees ORDER BY employee_id")

    for _ in range(3):
        migrations.migrate(db)

    assert rows(db, "SELECT * FROM employees ORDER BY employee_id") == snapshot


def test_repeated_migration_does_not_renumber_departments(tmp_path):
    db = legacy_db(tmp_path / "old.db")
    migrations.migrate(db)
    before = {r["name"]: r["department_id"]
              for r in rows(db, "SELECT name, department_id FROM departments")}

    migrations.migrate(db)
    after = {r["name"]: r["department_id"]
             for r in rows(db, "SELECT name, department_id FROM departments")}
    assert before == after


def test_backfill_helper_is_safe_to_repeat(tmp_path):
    db = tmp_path / "fresh.db"
    build_db.build(db)
    snapshot = rows(db, "SELECT * FROM employees ORDER BY employee_id")

    migrations.m005_organization_structure_backfill(db)
    migrations.m005_organization_structure_backfill(db)

    assert rows(db, "SELECT * FROM employees ORDER BY employee_id") == snapshot


def test_backfill_helper_on_pre005_database_is_noop(tmp_path):
    """قاعدة بلا العمود لا تنهار — تُتجاهَل بهدوء."""
    db = legacy_db(tmp_path / "old.db")
    migrations.m005_organization_structure_backfill(db)
    assert "department_id" not in cols(db, "employees")


# ---------- الترتيب والفهارس ----------

def test_migration_order_is_stable(tmp_path):
    names = [n for n, _ in migrations.MIGRATIONS]
    assert names == sorted(names)
    assert names[-1] == "005_organization_structure"


def test_005_runs_after_core_tables(tmp_path):
    db = tmp_path / "fresh.db"
    applied = migrations.migrate(db)
    assert applied.index("001_core_tables") < applied.index("005_organization_structure")


def test_expected_indexes_exist(tmp_path):
    db = tmp_path / "fresh.db"
    migrations.migrate(db)
    names = {r["name"] for r in rows(
        db, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_dept_id", "idx_emp_dept", "idx_emp_manager"} <= names


def test_department_id_index_is_unique(tmp_path):
    db = tmp_path / "fresh.db"
    build_db.build(db)

    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO departments (name, headcount, transport_allowance,"
                         " department_id) VALUES ('مكرر', 1, 1, 'DEPT-001')")
            conn.commit()
    finally:
        conn.close()


def test_status_recorded_in_schema_migrations(tmp_path):
    db = tmp_path / "fresh.db"
    migrations.migrate(db)
    status = migrations.status(db)
    assert "005_organization_structure" in status["applied"]
    assert status["pending"] == []


# ---------- مسار القاعدة ----------

def test_migrate_respects_reassigned_db_path(tmp_path, monkeypatch):
    target = tmp_path / "custom.db"
    monkeypatch.setattr(migrations, "DB_PATH", target)
    migrations.migrate()
    assert target.exists()
    assert "department_id" in cols(target, "employees")


def test_validate_integrity_respects_db_path(tmp_path, monkeypatch):
    target = tmp_path / "custom.db"
    build_db.build(target)
    monkeypatch.setattr(migrations, "DB_PATH", target)
    assert migrations.validate_integrity()["ok"]


def test_validate_integrity_survives_bad_path():
    from pathlib import Path
    report = migrations.validate_integrity(Path("/nonexistent/x.db"))
    assert report["ok"] is False and "error" in report
