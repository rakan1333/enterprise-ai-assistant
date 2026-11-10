"""ذرّية دفعة الهجرات.

الضمان: الهجرات الناقصة تُعتمد معاً أو لا تُعتمد أيّها. وكان
مكسوراً حتى اكتُشف أن `executescript` ينفّذ COMMIT ضمنياً.

حدّ واحد خارج الذرّية، مقصود ومختبَر هنا: جدول schema_migrations
يُنشأ قبل المعاملة، فيبقى فارغاً لو فشلت الدفعة على قاعدة جديدة.
"""

import sqlite3

import pytest

import build_db
import migrations


def objects(db) -> set[str]:
    c = sqlite3.connect(db)
    try:
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type IN ('table','index','trigger')"
            "   AND name NOT LIKE 'sqlite_%'")}
    finally:
        c.close()


def recorded(db) -> list[str]:
    c = sqlite3.connect(db)
    try:
        return [r[0] for r in c.execute(
            "SELECT name FROM schema_migrations ORDER BY name")]
    finally:
        c.close()


class Boom(RuntimeError):
    pass


@pytest.fixture
def exploding(monkeypatch):
    """يضيف هجرة ترمي بعد الهجرات الحقيقية كلها."""
    def add(after=None):
        base = list(migrations.MIGRATIONS)
        if after is not None:
            base = base[:[n for n, _ in base].index(after) + 1]

        def boom(conn):
            raise Boom("فشل مُصطنَع في منتصف الدفعة")

        monkeypatch.setattr(migrations, "MIGRATIONS", base + [("999_boom", boom)])
    return add


# ---------- السبب الجذري ----------

def test_no_migration_uses_executescript():
    """executescript ينفّذ COMMIT ضمنياً فيكسر ذرّية الدفعة."""
    src = open("migrations.py", encoding="utf-8").read()
    code = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#") and "`executescript`" not in line)
    assert "conn.executescript(" not in code


# ---------- قاعدة جديدة ----------

def test_failure_on_a_new_database_leaves_nothing_but_the_empty_log(tmp_path, exploding):
    db = tmp_path / "new.db"
    exploding()
    with pytest.raises(Boom):
        migrations.migrate(db)

    assert objects(db) == {"schema_migrations"}, \
        "لا جدول ولا فهرس ولا مُطلِق — عدا سجلّ الهجرات الفارغ"
    assert recorded(db) == []


def test_the_surviving_log_table_is_empty(tmp_path, exploding):
    db = tmp_path / "empty.db"
    exploding()
    with pytest.raises(Boom):
        migrations.migrate(db)
    c = sqlite3.connect(db)
    try:
        assert c.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
    finally:
        c.close()


def test_retry_after_failure_completes(tmp_path, exploding, monkeypatch):
    db = tmp_path / "retry.db"
    exploding()
    with pytest.raises(Boom):
        migrations.migrate(db)
    monkeypatch.undo()
    applied = migrations.migrate(db)
    assert applied[0] == "001_core_tables"
    assert applied[-1] == "007_attendance_integrity"
    assert migrations.validate_integrity(db)["ok"] is True


# ---------- قاعدة قائمة ----------

@pytest.mark.parametrize("stop_after", ["005_organization_structure", "006_attendance"])
def test_failure_on_an_existing_database_changes_nothing(tmp_path, monkeypatch, stop_after):
    db = tmp_path / f"{stop_after}.db"
    base = list(migrations.MIGRATIONS)
    upto = base[:[n for n, _ in base].index(stop_after) + 1]
    monkeypatch.setattr(migrations, "MIGRATIONS", upto)
    migrations.migrate(db)

    before_objects, before_log = objects(db), recorded(db)

    def boom(conn):
        raise Boom("فشل بعد قاعدة قائمة")

    monkeypatch.setattr(migrations, "MIGRATIONS", upto + [("999_boom", boom)])
    with pytest.raises(Boom):
        migrations.migrate(db)

    assert objects(db) == before_objects
    assert recorded(db) == before_log


def test_existing_rows_are_untouched_by_a_failed_batch(tmp_path, monkeypatch):
    db = tmp_path / "rows.db"
    build_db.build(db)
    migrations.migrate(db)
    c = sqlite3.connect(db)
    before = c.execute("SELECT * FROM employees ORDER BY employee_id").fetchall()
    c.close()

    def boom(conn):
        raise Boom("x")

    monkeypatch.setattr(migrations, "MIGRATIONS",
                        list(migrations.MIGRATIONS) + [("999_boom", boom)])
    with pytest.raises(Boom):
        migrations.migrate(db)

    c = sqlite3.connect(db)
    try:
        assert c.execute("SELECT * FROM employees ORDER BY employee_id").fetchall() == before
    finally:
        c.close()


# ---------- التسجيل ----------

def test_running_three_times_records_each_migration_once(tmp_path):
    db = tmp_path / "thrice.db"
    migrations.migrate(db)
    migrations.migrate(db)
    migrations.migrate(db)
    names = recorded(db)
    assert len(names) == len(set(names)) == len(migrations.MIGRATIONS)


def test_duplicate_registration_is_blocked_by_the_primary_key(tmp_path):
    db = tmp_path / "dup.db"
    migrations.migrate(db)
    c = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO schema_migrations (name, applied_at)"
                      " VALUES ('006_attendance','x')")
    finally:
        c.close()


def test_applied_list_is_read_inside_the_lock():
    """قراءتها قبل القفل تجعل عمليتين تطبّقان الهجرة نفسها."""
    import inspect
    src = inspect.getsource(migrations.migrate)
    assert src.index("BEGIN IMMEDIATE") < src.index("done = _applied(conn)")


def test_m006_is_equivalent_to_the_scripted_form(tmp_path):
    """التحويل مكانيكي — المخطّط الناتج نفسه."""
    db = tmp_path / "shape.db"
    migrations.migrate(db)
    c = sqlite3.connect(db)
    try:
        shape = {r[0]: r[1] for r in c.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IN"
            " ('sites','attendance_records','attendance_corrections')")}
    finally:
        c.close()
    assert set(shape) == {"sites", "attendance_records", "attendance_corrections"}
    assert "in_reported_lat" in shape["attendance_records"]
    assert "in_computed_distance_m" in shape["attendance_records"]


def test_migrate_rolls_back_explicitly_not_by_closing(tmp_path):
    """مالك المعاملة يتراجع صراحةً، لا اعتماداً على سلوك الإغلاق."""
    import inspect
    src = inspect.getsource(migrations.migrate)
    assert "ROLLBACK" in src
    assert "conn.in_transaction" in src
