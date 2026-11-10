"""الهجرة 006 — جداول الحضور.

تُقاس بثلاثة معايير: أنها تضيف ولا تمسّ، وأنها تُعاد بلا ضرر،
وأن فصل «ما ادّعاه الجهاز» عن «ما حسبه الخادم» محفور في المخطّط
لا متروكاً لانضباط المستدعي.
"""

import sqlite3

import pytest

import build_db
import migrations


def cols(db, table) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def tables(db) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


@pytest.fixture
def fresh(tmp_path):
    db = tmp_path / "m006.db"
    migrations.migrate(db)
    return db


# ---------- التسجيل والترتيب ----------

def test_006_keeps_its_position(fresh):
    """الثبات المقصود ألّا يتغيّر *موضع* هجرة مطبَّقة، لا ألّا تُضاف بعدها."""
    names = [n for n, _ in migrations.MIGRATIONS]
    assert names.index("006_attendance") == 5


def test_migration_names_stay_sorted():
    names = [n for n, _ in migrations.MIGRATIONS]
    assert names == sorted(names)


def test_006_runs_after_core_tables(tmp_path):
    applied = migrations.migrate(tmp_path / "order.db")
    assert applied.index("001_core_tables") < applied.index("006_attendance")


def test_status_reports_nothing_pending(fresh):
    st = migrations.status(fresh)
    assert "006_attendance" in st["applied"]
    assert st["pending"] == []


# ---------- الجداول ----------

def test_the_three_tables_exist(fresh):
    assert {"sites", "attendance_records", "attendance_corrections"} <= tables(fresh)


def test_earlier_tables_survive(fresh):
    assert {"employees", "departments", "leave_requests", "users",
            "events", "schema_migrations"} <= tables(fresh)


def test_site_columns(fresh):
    assert {"site_id", "name", "latitude", "longitude", "radius_m",
            "department_id", "is_active", "created_at"} <= cols(fresh, "sites")


def test_correction_columns(fresh):
    assert {"id", "employee_id", "record_id", "kind", "requested_change",
            "reason", "status", "reviewed_by", "decision_note",
            "decided_at", "created_at"} <= cols(fresh, "attendance_corrections")


# ---------- فصل المصدر ----------

def test_reported_and_computed_columns_are_distinct(fresh):
    """القرار المعماري الأهمّ: ما ادّعاه الجهاز وما حسبه الخادم
    عمودان لا عمود. دمجهما يُفقد القدرة على الإجابة عند النزاع."""
    c = cols(fresh, "attendance_records")
    reported = {"in_reported_lat", "in_reported_lon", "in_reported_accuracy_m",
                "out_reported_lat", "out_reported_lon", "out_reported_accuracy_m"}
    computed = {"in_computed_distance_m", "in_verdict", "in_verdict_reason",
                "out_computed_distance_m", "out_verdict", "out_verdict_reason"}
    assert reported <= c, reported - c
    assert computed <= c, computed - c
    assert not (reported & computed)


def test_request_ip_is_recorded(fresh):
    assert {"in_request_ip", "out_request_ip"} <= cols(fresh, "attendance_records")


def test_check_out_is_nullable(fresh):
    """الجلسة المفتوحة بلا انصراف — حالة طبيعية لا خطأ."""
    conn = sqlite3.connect(fresh)
    try:
        conn.execute(
            "INSERT INTO attendance_records (employee_id, check_in_at, in_verdict)"
            " VALUES (?,?,?)",
            ("EMP-001", "2026-10-12T07:00:00+00:00", "inside"))
        conn.commit()
        assert conn.execute(
            "SELECT check_out_at FROM attendance_records").fetchone()[0] is None
    finally:
        conn.close()


# ---------- الفهارس ----------

def test_expected_indexes_exist(fresh):
    conn = sqlite3.connect(fresh)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()
    assert {"idx_site_active", "idx_att_emp_in", "idx_att_open",
            "idx_corr_status", "idx_corr_employee"} <= names


def test_earlier_indexes_survive(fresh):
    conn = sqlite3.connect(fresh)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()
    assert {"idx_dept_id", "idx_emp_dept", "idx_emp_manager"} <= names


# ---------- عدم المساس ----------

def test_rerunning_is_a_no_op(fresh):
    assert migrations.migrate(fresh) == []


def test_running_006_twice_directly_does_not_raise(fresh):
    """الحارس الحقيقي: الهجرة نفسها تتحمّل الإعادة، لا سجلّها فقط."""
    conn = sqlite3.connect(fresh)
    try:
        migrations.m006_attendance(conn)
        migrations.m006_attendance(conn)
        conn.commit()
    finally:
        conn.close()


def test_existing_rows_are_untouched(tmp_path):
    db = tmp_path / "rows.db"
    build_db.build(db)
    conn = sqlite3.connect(db)
    try:
        before = conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
        names = {r[0] for r in conn.execute("SELECT name FROM employees")}
    finally:
        conn.close()

    migrations.migrate(db)

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == before
        assert {r[0] for r in conn.execute("SELECT name FROM employees")} == names
    finally:
        conn.close()


def test_no_employee_or_department_column_was_dropped(tmp_path):
    db = tmp_path / "keep.db"
    build_db.build(db)
    before_emp, before_dept = cols(db, "employees"), cols(db, "departments")
    migrations.migrate(db)
    assert before_emp <= cols(db, "employees")
    assert before_dept <= cols(db, "departments")


def test_upgrading_a_pre_006_database(tmp_path):
    """القاعدة القائمة عند المستخدم تُرقّى، لا تُعاد بناؤها."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    try:
        migrations._applied(conn)          # ينشئ جدول سجلّ الهجرات
        for name, fn in migrations.MIGRATIONS:
            if name == "006_attendance":
                break
            fn(conn)
            conn.execute("INSERT INTO schema_migrations (name, applied_at) VALUES (?,?)",
                         (name, "2026-01-01"))
        conn.commit()
    finally:
        conn.close()

    assert "attendance_records" not in tables(db)
    assert migrations.migrate(db) == ["006_attendance", "007_attendance_integrity"]
    assert "attendance_records" in tables(db)


# ---------- فحص السلامة ----------

def test_integrity_is_ok_on_a_fresh_database(tmp_path):
    db = tmp_path / "ok.db"
    build_db.build(db)
    migrations.migrate(db)
    assert migrations.validate_integrity(db)["ok"] is True


def test_integrity_reports_an_orphan_site_reference(tmp_path):
    db = tmp_path / "orphan.db"
    build_db.build(db)
    migrations.migrate(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """INSERT INTO attendance_records (employee_id, site_id, check_in_at, in_verdict)
               VALUES ('EMP-001', 'SITE-GHOST', '2026-10-12T07:00:00+00:00', 'inside')""")
        conn.commit()
    finally:
        conn.close()

    res = migrations.validate_integrity(db)
    assert res["ok"] is False
    assert res["orphan_attendance_sites"][0]["site_id"] == "SITE-GHOST"


def test_integrity_reports_an_orphan_employee_reference(tmp_path):
    db = tmp_path / "ghost.db"
    build_db.build(db)
    migrations.migrate(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """INSERT INTO attendance_records (employee_id, check_in_at, in_verdict)
               VALUES ('EMP-GHOST', '2026-10-12T07:00:00+00:00', 'inside')""")
        conn.commit()
    finally:
        conn.close()
    assert migrations.validate_integrity(db)["ok"] is False


def test_integrity_survives_a_database_without_006(tmp_path):
    """الفحص يعمل على قاعدة قديمة بدل أن يسقط كله."""
    db = tmp_path / "pre006.db"
    conn = sqlite3.connect(db)
    try:
        for name, fn in migrations.MIGRATIONS:
            if name == "006_attendance":
                break
            fn(conn)
        conn.commit()
    finally:
        conn.close()

    res = migrations.validate_integrity(db)
    assert "error" not in res
    assert res["orphan_attendance_sites"] == []
