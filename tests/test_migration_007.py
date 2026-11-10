"""الهجرة 007 — الأعمدة والمُطلِقات والفهرس الفريد.

تُقاس بثلاثة: أنها تضيف ولا تمسّ، وأنها تتوقّف عند التعارض بلا
تعديل سجل، وأن مُطلِقاتها لا تُجمّد بيانات قائمة.
"""

import sqlite3

import pytest

import attendance as att
import build_db
import migrations


def tables(db) -> set[str]:
    c = sqlite3.connect(db)
    try:
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def triggers(db) -> set[str]:
    c = sqlite3.connect(db)
    try:
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
    finally:
        c.close()


def cols(db, table) -> set[str]:
    c = sqlite3.connect(db)
    try:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    finally:
        c.close()


def snapshot(db, table="attendance_records") -> list[tuple]:
    c = sqlite3.connect(db)
    try:
        return c.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    finally:
        c.close()


@pytest.fixture
def fresh(tmp_path):
    db = tmp_path / "m007.db"
    migrations.migrate(db)
    return db


def through_006(tmp_path, name="pre007.db"):
    """قاعدة مطبَّقة حتى 006 فقط — الحالة الواقعية عند التحديث."""
    db = tmp_path / name
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        migrations._ensure_migrations_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        for n, fn in migrations.MIGRATIONS:
            if n == "007_attendance_integrity":
                break
            fn(conn)
            conn.execute("INSERT INTO schema_migrations (name, applied_at) VALUES (?,?)",
                         (n, "2026-01-01T00:00:00+00:00"))
        conn.execute("COMMIT")
    finally:
        conn.close()
    return db


def add_row(db, **kw):
    """إدراج يتجاوز المُطلِقات حين يلزم زرع بيانات قديمة."""
    base = {"employee_id": "EMP-001", "check_in_at": "2026-10-12T07:00:00+00:00",
            "in_verdict": "inside"}
    base.update(kw)
    c = sqlite3.connect(db)
    try:
        c.execute("INSERT INTO attendance_records (%s) VALUES (%s)" % (
            ",".join(base), ",".join("?" * len(base))), tuple(base.values()))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        c.close()


# ---------- التسجيل ----------

def test_007_is_registered_last():
    assert [n for n, _ in migrations.MIGRATIONS][-1] == "007_attendance_integrity"


def test_names_stay_sorted():
    names = [n for n, _ in migrations.MIGRATIONS]
    assert names == sorted(names)


def test_upgrading_a_pre_007_database(tmp_path):
    db = through_006(tmp_path)
    assert "idx_att_one_open" not in triggers(db)
    assert migrations.migrate(db) == ["007_attendance_integrity"]


def test_rerunning_is_a_no_op(fresh):
    assert migrations.migrate(fresh) == []


# ---------- الأعمدة ----------

def test_audit_columns_exist(fresh):
    assert {"original_check_in_at", "original_check_out_at",
            "corrected_by_correction_id", "resolved_by_claimed",
            "resolved_at"} <= cols(fresh, "attendance_records")


def test_requested_time_columns_exist(fresh):
    assert {"requested_check_in_at", "requested_check_out_at"} \
        <= cols(fresh, "attendance_corrections")


def test_resolved_by_is_named_as_a_claim(fresh):
    """الاسم يحمل التحفّظ: إقرار المشغّل لا هوية متحقَّقة."""
    c = cols(fresh, "attendance_records")
    assert "resolved_by_claimed" in c
    assert "resolved_by" not in c


def test_no_earlier_column_was_dropped(tmp_path):
    db = through_006(tmp_path)
    before = cols(db, "attendance_records")
    migrations.migrate(db)
    assert before <= cols(db, "attendance_records")


# ---------- الفهرس ----------

def test_unique_index_exists(fresh):
    c = sqlite3.connect(fresh)
    try:
        assert c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='idx_att_one_open'").fetchone()
    finally:
        c.close()


def test_index_blocks_a_second_open_session(fresh):
    add_row(fresh, employee_id="E1")
    with pytest.raises(sqlite3.IntegrityError):
        add_row(fresh, employee_id="E1", check_in_at="2026-10-12T08:00:00+00:00")


def test_index_allows_many_rejected_attempts(fresh):
    for i in range(3):
        add_row(fresh, employee_id="E2", in_verdict="outside",
                check_in_at=f"2026-10-12T0{i}:00:00+00:00")
    assert len(snapshot(fresh)) == 3


def test_index_allows_a_new_session_after_checkout(fresh):
    add_row(fresh, employee_id="E3")
    c = sqlite3.connect(fresh)
    c.execute("UPDATE attendance_records SET check_out_at='2026-10-12T15:00:00+00:00',"
              " out_verdict='inside' WHERE employee_id='E3'")
    c.commit(); c.close()
    add_row(fresh, employee_id="E3", check_in_at="2026-10-13T07:00:00+00:00")


def test_manually_added_does_not_occupy_the_index(fresh):
    """السجل الإداري ليس جلسة حيّة، فلا يمنع حضوراً جغرافياً."""
    add_row(fresh, employee_id="E4", in_verdict="manually_added",
            check_out_at="2026-10-12T15:00:00+00:00", out_verdict="correction_closed")
    add_row(fresh, employee_id="E4", check_in_at="2026-10-12T16:00:00+00:00")


# ---------- التعارض يوقف الهجرة ----------

def test_conflict_stops_the_migration_without_touching_rows(tmp_path):
    db = through_006(tmp_path, "conflict.db")
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO attendance_records (employee_id, check_in_at, in_verdict)"
        " VALUES (?,?,?)",
        [("EMP-311", "2026-10-12T07:00:00+00:00", "inside"),
         ("EMP-311", "2026-10-12T07:00:02+00:00", "inside"),
         ("EMP-311", "2026-10-12T07:00:05+00:00", "inside")])
    conn.commit(); conn.close()

    before = snapshot(db)
    with pytest.raises(migrations.MigrationConflict) as exc:
        migrations.migrate(db)

    assert "EMP-311" in str(exc.value)
    assert snapshot(db) == before, "لم يتغيّر أي سجل"
    assert "idx_att_one_open" not in triggers(db)
    conn = sqlite3.connect(db)
    recorded = [r[0] for r in conn.execute("SELECT name FROM schema_migrations")]
    conn.close()
    assert "007_attendance_integrity" not in recorded


def test_conflict_rolls_back_columns_and_triggers(tmp_path):
    db = through_006(tmp_path, "rollback.db")
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO attendance_records (employee_id, check_in_at, in_verdict)"
        " VALUES (?,?,?)",
        [("E", "t1", "inside"), ("E", "t2", "inside")])
    conn.commit(); conn.close()

    with pytest.raises(migrations.MigrationConflict):
        migrations.migrate(db)

    assert "resolved_by_claimed" not in cols(db, "attendance_records")
    assert not any(t.startswith("trg_att_") for t in triggers(db))


def test_migration_passes_after_explicit_resolution(tmp_path, monkeypatch):
    db = through_006(tmp_path, "resolved.db")
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO attendance_records (employee_id, check_in_at, in_verdict)"
        " VALUES (?,?,?)",
        [("E", "2026-10-12T07:00:00+00:00", "inside"),
         ("E", "2026-10-12T07:00:02+00:00", "inside")])
    conn.commit()
    extra = conn.execute(
        "SELECT MAX(id) FROM attendance_records").fetchone()[0]
    conn.close()

    with pytest.raises(migrations.MigrationConflict):
        migrations.migrate(db)

    # المعالجة الصريحة قبل المُطلِقات: إغلاق يدوي موثَّق
    conn = sqlite3.connect(db)
    conn.execute("UPDATE attendance_records SET check_out_at=?, out_verdict=?,"
                 " out_verdict_reason=?, resolved_at=? WHERE id=?"
                 if "resolved_at" in cols(db, "attendance_records") else
                 "UPDATE attendance_records SET check_out_at=?, out_verdict=?,"
                 " out_verdict_reason=? WHERE id=?",
                 ("2026-10-12T09:00:00+00:00", "conflict_closed",
                  "سباق تزامن — عولج يدوياً", extra))
    conn.commit(); conn.close()

    assert migrations.migrate(db) == ["007_attendance_integrity"]


def test_conflict_message_names_the_record_ids(tmp_path):
    db = through_006(tmp_path, "named.db")
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO attendance_records (employee_id, check_in_at, in_verdict)"
        " VALUES (?,?,?)", [("Z", "t1", "inside"), ("Z", "t2", "inside")])
    conn.commit(); conn.close()
    with pytest.raises(migrations.MigrationConflict) as exc:
        migrations.migrate(db)
    assert "resolve_duplicate_session" in str(exc.value)


# ---------- المُطلِقات ----------

def test_nine_triggers_are_created(fresh):
    created = {t for t in triggers(fresh) if t.startswith(("trg_att_", "trg_corr_"))}
    assert len(created) == 9, sorted(created)


def test_in_verdict_is_required(fresh):
    c = sqlite3.connect(fresh)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO attendance_records (employee_id, check_in_at)"
                      " VALUES ('E','t')")
    finally:
        c.close()


@pytest.mark.parametrize("value", ["inside", "outside", "rejected", "manually_added"])
def test_known_in_verdicts_are_accepted(fresh, value):
    kw = {"employee_id": "E" + value, "in_verdict": value}
    if value == "manually_added":
        kw |= {"check_out_at": "2026-10-12T15:00:00+00:00",
               "out_verdict": "correction_closed"}
    add_row(fresh, **kw)


def test_unknown_in_verdict_is_rejected(fresh):
    with pytest.raises(sqlite3.IntegrityError):
        add_row(fresh, in_verdict="شيء_ما")


def test_closure_verdict_is_rejected_in_the_in_column(fresh):
    """المفردات مفصولة بالعمود: الإغلاق الإداري ليس حكم بداية."""
    with pytest.raises(sqlite3.IntegrityError):
        add_row(fresh, in_verdict="conflict_closed")


def test_manually_added_is_rejected_in_the_out_column(fresh):
    with pytest.raises(sqlite3.IntegrityError):
        add_row(fresh, check_out_at="2026-10-12T15:00:00+00:00",
                out_verdict="manually_added")


def test_correction_status_vocabulary(fresh):
    c = sqlite3.connect(fresh)
    try:
        c.execute("INSERT INTO attendance_corrections (employee_id, kind, status)"
                  " VALUES ('E','other','pending')")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO attendance_corrections (employee_id, kind, status)"
                      " VALUES ('E','other','مجهولة')")
    finally:
        c.close()


# ---------- اقتران الوقت بالحكم ----------

def test_time_without_verdict_is_rejected(fresh):
    with pytest.raises(sqlite3.IntegrityError):
        add_row(fresh, check_out_at="2026-10-12T15:00:00+00:00")


def test_verdict_without_time_is_rejected(fresh):
    with pytest.raises(sqlite3.IntegrityError):
        add_row(fresh, out_verdict="inside")


def test_both_null_is_an_open_session(fresh):
    add_row(fresh, employee_id="OPEN")


def test_both_set_is_a_closed_session(fresh):
    add_row(fresh, employee_id="CLOSED", check_out_at="2026-10-12T15:00:00+00:00",
            out_verdict="inside")


def test_emptying_the_verdict_alone_is_rejected(fresh):
    add_row(fresh, employee_id="P1", check_out_at="2026-10-12T15:00:00+00:00",
            out_verdict="inside")
    c = sqlite3.connect(fresh)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE attendance_records SET out_verdict=NULL"
                      " WHERE employee_id='P1'")
    finally:
        c.close()


def test_updating_an_unrelated_column_passes(fresh):
    add_row(fresh, employee_id="P2", check_out_at="2026-10-12T15:00:00+00:00",
            out_verdict="inside")
    c = sqlite3.connect(fresh)
    try:
        c.execute("UPDATE attendance_records SET in_request_ip='1.1.1.1'"
                  " WHERE employee_id='P2'")
        c.commit()
    finally:
        c.close()


# ---------- السجلات القديمة لا تتجمّد ----------

@pytest.fixture
def legacy(tmp_path):
    """قاعدة فيها صفّان غير متّسقين، زُرعا قبل المُطلِقات."""
    db = through_006(tmp_path, "legacy.db")
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO attendance_records (id, employee_id, check_in_at, in_verdict,"
        " check_out_at, out_verdict) VALUES (?,?,?,?,?,?)",
        [(1, "L1", "t1", "inside", "t2", None),            # وقت بلا حكم
         (2, "L2", "t1", "inside", "t2", "قيمة_قديمة")])   # حكم مجهول
    conn.commit(); conn.close()
    migrations.migrate(db)
    return db


def test_legacy_rows_survive_the_migration(legacy):
    rows = snapshot(legacy)
    assert len(rows) == 2
    assert rows[1][snapshot_index(legacy, "out_verdict")] == "قيمة_قديمة"


def snapshot_index(db, column) -> int:
    c = sqlite3.connect(db)
    try:
        return [r[1] for r in c.execute(
            "PRAGMA table_info(attendance_records)")].index(column)
    finally:
        c.close()


@pytest.mark.parametrize("rid", [1, 2])
def test_unrelated_update_to_a_legacy_row_is_not_frozen(legacy, rid):
    """الحارس IS NOT OLD يمنع تجميد الصفّ القديم."""
    c = sqlite3.connect(legacy)
    try:
        c.execute("UPDATE attendance_records SET in_request_ip='9.9.9.9' WHERE id=?",
                  (rid,))
        c.commit()
    finally:
        c.close()


def test_repairing_a_legacy_row_with_a_known_value_passes(legacy):
    c = sqlite3.connect(legacy)
    try:
        c.execute("UPDATE attendance_records SET out_verdict='outside' WHERE id=1")
        c.commit()
    finally:
        c.close()


def test_repairing_a_legacy_row_with_an_unknown_value_is_rejected(legacy):
    """المُطلِقان معاً: الاتّساق والمفردات."""
    c = sqlite3.connect(legacy)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="out_verdict"):
            c.execute("UPDATE attendance_records SET out_verdict='أخرى' WHERE id=1")
    finally:
        c.close()


def test_repairing_both_columns_together_passes(legacy):
    c = sqlite3.connect(legacy)
    try:
        c.execute("UPDATE attendance_records SET check_out_at='t5',"
                  " out_verdict='correction_closed' WHERE id=1")
        c.commit()
    finally:
        c.close()


def test_integrity_reports_legacy_values_without_fixing_them(legacy):
    res = migrations.validate_integrity(legacy)
    assert res["ok"] is False
    assert any(u["value"] == "قيمة_قديمة" for u in res["unknown_verdicts"])
    assert any(c["id"] == 1 for c in res["inconsistent_closures"])
    assert snapshot(legacy), "البيانات ما زالت كما هي"


# ---------- منع إعادة الفتح ----------

def test_reopening_is_blocked_by_direct_sql_after_migration(fresh):
    """الدفاع في القاعدة لا في التطبيق — تحديث مباشر بعد الترحيل."""
    add_row(fresh, employee_id="R1", check_out_at="2026-10-12T15:00:00+00:00",
            out_verdict="inside")
    c = sqlite3.connect(fresh)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="إعادة فتح"):
            c.execute("UPDATE attendance_records SET check_out_at=NULL,"
                      " out_verdict=NULL WHERE employee_id='R1'")
    finally:
        c.close()


def test_closing_an_open_session_still_works(fresh):
    add_row(fresh, employee_id="R2")
    c = sqlite3.connect(fresh)
    try:
        c.execute("UPDATE attendance_records SET check_out_at='t9',"
                  " out_verdict='inside' WHERE employee_id='R2'")
        c.commit()
    finally:
        c.close()


def test_no_attendance_path_sets_check_out_to_null():
    """تفتيش نصّي: لا مسار تصحيح يُفرّغ وقت الانصراف."""
    src = open("attendance.py", encoding="utf-8").read()
    assert "check_out_at = NULL" not in src
    assert "check_out_at=NULL" not in src


# ---------- سلامة المخطّط ----------

def test_integrity_is_ok_on_a_fresh_database(tmp_path):
    db = tmp_path / "ok.db"
    build_db.build(db)
    migrations.migrate(db)
    assert migrations.validate_integrity(db)["ok"] is True


def test_attendance_module_sees_the_new_columns(fresh, monkeypatch):
    monkeypatch.setattr(att, "DB_PATH", fresh)
    assert att.inspect_duplicate_sessions(path=fresh) == []


# ========================= النداء المباشر =========================
#
# migrate تملك المعاملة دائماً، فالشرط `if not conn.in_transaction`
# في m007 لا يُستعمل في المسار الحقيقي. اختبار الطفرات أظهر أن
# إسقاطه لا يُسقط أي اختبار — أي أن ملكية المعاملة عند النداء
# المباشر كانت غير مُختبَرة. هذه الاختبارات تغلق الثغرة: من يستدعي
# m007 على اتصال بلا معاملة يحصل على ذرّية كاملة أيضاً.

def _upto_006(db, monkeypatch):
    """قاعدة عند 006 بالضبط — لتُستدعى m007 عليها مباشرةً.

    بلا build_db: هي تشغّل الهجرات كلها بما فيها 007، فلا تُبقي
    شيئاً ليُختبَر. الهجرات وحدها تُنشئ ما تحتاجه.
    """
    names = [n for n, _ in migrations.MIGRATIONS]
    prefix = migrations.MIGRATIONS[:names.index("006_attendance") + 1]
    monkeypatch.setattr(migrations, "MIGRATIONS", prefix)
    migrations.migrate(db)
    monkeypatch.undo()

    assert "original_check_in_at" not in cols(db, "attendance_records"), \
        "التجهيز يجب أن يتوقّف عند 006 — وإلا لم يختبر شيئاً"


def test_a_direct_call_opens_its_own_transaction(tmp_path, monkeypatch):
    """بلا معاملة محيطة، m007 تفتح واحدة وتغلقها."""
    db = tmp_path / "direct.db"
    _upto_006(db, monkeypatch)

    conn = sqlite3.connect(db, isolation_level=None)
    try:
        assert not conn.in_transaction
        migrations.m007_attendance_integrity(conn)
        assert not conn.in_transaction, "من فتح هو من أغلق"
    finally:
        conn.close()

    assert "original_check_in_at" in cols(db, "attendance_records")
    assert "trg_att_no_reopen" in triggers(db)


def test_a_direct_call_that_conflicts_leaves_no_partial_schema(tmp_path, monkeypatch):
    """التعارض يوقف النداء المباشر، ولا يترك عموداً ولا مُطلِقاً.

    هذا ما يحميه القفل الشرطي: بلا معاملة يملكها النداء، كان كل
    ALTER وكل CREATE TRIGGER يُثبَّت وحده قبل أن يُرفع التعارض.
    """
    db = tmp_path / "direct_conflict.db"
    _upto_006(db, monkeypatch)

    seed = sqlite3.connect(db)
    seed.executemany(
        "INSERT INTO attendance_records (employee_id, check_in_at, in_verdict)"
        " VALUES (?,?,?)",
        [("EMP-001", "2026-10-12T07:00:00+00:00", "inside"),
         ("EMP-001", "2026-10-12T08:00:00+00:00", "inside")])
    seed.commit(); seed.close()

    before_cols = cols(db, "attendance_records")
    before_triggers = triggers(db)

    conn = sqlite3.connect(db, isolation_level=None)
    try:
        with pytest.raises(migrations.MigrationConflict):
            migrations.m007_attendance_integrity(conn)
        assert not conn.in_transaction, "تراجع صراحةً لا بالإغلاق"
    finally:
        conn.close()

    assert cols(db, "attendance_records") == before_cols, "لا عمود نجا"
    assert triggers(db) == before_triggers, "لا مُطلِق نجا"
    assert "idx_att_one_open" not in {r[0] for r in sqlite3.connect(db).execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


def test_a_direct_call_inside_a_transaction_does_not_commit_it(tmp_path, monkeypatch):
    """المعاملة ملك من فتحها: m007 لا تُثبّت عمل مستدعيها."""
    db = tmp_path / "owned.db"
    _upto_006(db, monkeypatch)

    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO attendance_records (employee_id, check_in_at,"
                     " in_verdict) VALUES ('EMP-009','2026-10-12T07:00:00+00:00','inside')")
        migrations.m007_attendance_integrity(conn)
        assert conn.in_transaction, "لم تُثبّت معاملة غيرها"
        conn.execute("ROLLBACK")
    finally:
        conn.close()

    c = sqlite3.connect(db)
    try:
        assert c.execute("SELECT COUNT(*) FROM attendance_records").fetchone()[0] == 0, \
            "تراجع المستدعي أرجع كل شيء"
    finally:
        c.close()
    assert "original_check_in_at" not in cols(db, "attendance_records")
