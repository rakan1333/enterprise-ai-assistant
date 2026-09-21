"""بنية تحتية: المفتاح · الهجرات · تحديد المعدل · التدقيق · سقف الرفع."""

import importlib
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest


# ---------- AUTH_SECRET ----------

def _reload_auth(tmp_path, **env):
    for k in ("AUTH_SECRET", "APP_ENV"):
        os.environ.pop(k, None)
    os.environ.update({k: v for k, v in env.items() if v is not None})
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        sys.modules.pop("auth", None)
        return importlib.import_module("auth")
    finally:
        os.chdir(cwd)


@pytest.fixture(autouse=True)
def _restore_auth():
    """يعيد كائن الوحدة الأصلي نفسه — لا نسخة جديدة.

    ⚠️ كانت هذي التجهيزة تستورد نسخة جديدة من auth بعد كل اختبار.
    الوحدات التي استوردت auth قبلها (api مثلاً) تبقى ممسكة بالأصلية،
    فيتعايش كائنان: تجهيزات الملفات اللاحقة تضبط الجديد، والـ API
    يقرأ الأصلي — فتصل طلبات الدخول إلى قاعدة ملف سابق. ظلّت خفية
    لأن تلك القاعدة القديمة احتوت الحسابات نفسها بالمصادفة.
    """
    original = sys.modules.get("auth")
    yield
    for k in ("AUTH_SECRET", "APP_ENV"):
        os.environ.pop(k, None)
    if original is not None:
        sys.modules["auth"] = original
    else:
        sys.modules.pop("auth", None)
        importlib.import_module("auth")


def test_secret_is_stable_across_restarts(tmp_path):
    """المفتاح العشوائي في الذاكرة كان يُبطل كل الجلسات عند كل إقلاع."""
    a = _reload_auth(tmp_path)
    first = a.SECRET_KEY
    b = _reload_auth(tmp_path)
    assert b.SECRET_KEY == first


def test_token_survives_restart(tmp_path):
    a = _reload_auth(tmp_path)
    db = tmp_path / "s.db"
    a.init_db(db)
    a.create_user("user1", "EMP-1", "مستخدم", "LongEnough1", "employee", db)
    token = a.create_token(a.get_user("user1", db))

    b = _reload_auth(tmp_path)
    assert b.decode_token(token)["employee_id"] == "EMP-1"


def test_environment_secret_wins(tmp_path):
    a = _reload_auth(tmp_path, AUTH_SECRET="e" * 48)
    assert a.SECRET_KEY == "e" * 48


def test_short_secret_is_rejected(tmp_path):
    with pytest.raises(Exception) as exc:
        _reload_auth(tmp_path, AUTH_SECRET="tooshort")
    assert "AUTH_SECRET" in str(exc.value)


def test_production_requires_explicit_secret(tmp_path):
    """الإنتاج لا يُقلع بمفتاح مولَّد — فشل صريح خير من أمان وهمي."""
    with pytest.raises(Exception):
        _reload_auth(tmp_path, APP_ENV="production")


def test_production_accepts_configured_secret(tmp_path):
    a = _reload_auth(tmp_path, APP_ENV="production", AUTH_SECRET="p" * 48)
    assert a.SECRET_KEY == "p" * 48


def test_secret_never_logged(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.DEBUG):
        a = _reload_auth(tmp_path)
    assert a.SECRET_KEY not in caplog.text


# ---------- الهجرات ----------

def test_migrations_are_idempotent(tmp_path):
    import migrations
    db = tmp_path / "m.db"
    first = migrations.migrate(db)
    second = migrations.migrate(db)
    assert first and not second


def test_migrations_create_expected_tables(tmp_path):
    import migrations
    db = tmp_path / "m.db"
    migrations.migrate(db)
    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"employees", "departments", "leave_requests", "users",
            "events", "schema_migrations"} <= names


def test_migration_adds_audit_columns(tmp_path):
    import migrations
    db = tmp_path / "m.db"
    migrations.migrate(db)
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    conn.close()
    assert {"actor_id", "actor_role", "request_id"} <= cols


def test_migration_preserves_existing_rows(tmp_path):
    """الهجرة تضيف أعمدة ولا تمسّ البيانات."""
    import migrations
    db = tmp_path / "m.db"
    migrations.migrate(db)

    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO employees
           (employee_id, name, department, job_title, annual_leave, leave_used)
           VALUES ('E1','اسم','قسم','مسمى',30,5)"""
    )
    conn.commit(); conn.close()

    migrations.migrate(db)

    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    conn.close()
    assert n == 1


def test_migration_status_reports_pending(tmp_path):
    import migrations
    db = tmp_path / "m.db"
    assert migrations.status(db)["pending"]
    migrations.migrate(db)
    assert not migrations.status(db)["pending"]


# ---------- build_db غير تدميري ----------

def test_startup_build_preserves_data(tmp_path):
    """الإقلاع المتكرر كان يمسح الطلبات والحسابات."""
    import build_db
    db = tmp_path / "b.db"
    build_db.build(db)

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO leave_requests (employee_id,employee_name,start_date,end_date,"
        "days,status,created_at) VALUES ('EMP-003','خالد','2026-10-01','2026-10-03',3,'pending','x')"
    )
    conn.commit(); conn.close()

    build_db.build(db)
    build_db.build(db)

    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0]
    conn.close()
    assert n == 1


def test_repeated_build_does_not_duplicate_seed(tmp_path):
    import build_db
    db = tmp_path / "b.db"
    build_db.build(db)
    build_db.build(db)
    assert build_db._count(db, "employees") == 6


def test_force_is_explicitly_destructive(tmp_path):
    import build_db
    db = tmp_path / "b.db"
    build_db.build(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO leave_requests (employee_id,employee_name,start_date,end_date,"
        "days,status,created_at) VALUES ('E','ن','2026-10-01','2026-10-03',3,'pending','x')"
    )
    conn.commit(); conn.close()

    build_db.build(db, force=True)
    assert build_db._count(db, "leave_requests") == 0


def test_has_data_detects_operational_rows(tmp_path):
    import build_db
    db = tmp_path / "b.db"
    assert not build_db.has_data(db)
    build_db.build(db)
    assert build_db.has_data(db)


# ---------- تحديد المعدل ----------

def test_limiter_allows_up_to_limit():
    from ratelimit import SlidingWindow
    w = SlidingWindow(limit=3, window=60)
    for _ in range(3):
        w.check("k")


def test_limiter_blocks_beyond_limit():
    from ratelimit import RateLimitExceeded, SlidingWindow
    w = SlidingWindow(limit=2, window=60)
    w.check("k"); w.check("k")
    with pytest.raises(RateLimitExceeded) as exc:
        w.check("k")
    assert exc.value.retry_after > 0


def test_limiter_isolates_keys():
    """موظف يستنفد حده لا يمنع زميله."""
    from ratelimit import SlidingWindow
    w = SlidingWindow(limit=1, window=60)
    w.check("a")
    w.check("b")


def test_limiter_window_slides():
    from ratelimit import RateLimitExceeded, SlidingWindow
    w = SlidingWindow(limit=1, window=1)
    w.check("k")
    with pytest.raises(RateLimitExceeded):
        w.check("k")
    time.sleep(1.05)
    w.check("k")


def test_limiter_reset_clears_key():
    from ratelimit import SlidingWindow
    w = SlidingWindow(limit=1, window=60)
    w.check("k")
    w.reset("k")
    w.check("k")


def test_client_key_prefers_identity():
    import ratelimit
    req = type("R", (), {"client": type("C", (), {"host": "10.0.0.1"})()})()
    assert ratelimit.client_key(req, {"employee_id": "EMP-003"}) == "user:EMP-003"
    assert ratelimit.client_key(req) == "ip:10.0.0.1"


def test_client_key_handles_missing_client():
    import ratelimit
    assert ratelimit.client_key(type("R", (), {"client": None})()) == "ip:unknown"


# ---------- التدقيق ----------

def test_event_records_actor_and_request(tmp_path):
    import metrics
    db = tmp_path / "e.db"
    metrics.init_db(db)
    actor = {"employee_id": "EMP-003", "role": "employee", "sub": "khalid",
             "name": "خالد الدوسري"}
    metrics.record("question", "سؤال", "agent", actor=actor, request_id="r1", path=db)

    row = metrics.summary(db)["recent"][0]
    assert row["actor_id"] == "EMP-003"
    assert row["actor_role"] == "employee"
    assert row["request_id"] == "r1"


def test_event_does_not_store_personal_name(tmp_path):
    """المعرّف يكفي للتدقيق — الاسم بيانات شخصية بلا حاجة."""
    import metrics
    db = tmp_path / "e.db"
    metrics.init_db(db)
    metrics.record("question", "سؤال", "agent",
                   actor={"employee_id": "EMP-003", "role": "employee",
                          "name": "خالد الدوسري"},
                   path=db)
    assert "خالد الدوسري" not in str(metrics.summary(db)["recent"][0])


def test_audit_trail_filters_by_actor(tmp_path):
    import metrics
    db = tmp_path / "e.db"
    metrics.init_db(db)
    metrics.record("question", "أ", "agent", actor={"employee_id": "EMP-1", "role": "employee"}, path=db)
    metrics.record("question", "ب", "agent", actor={"employee_id": "EMP-2", "role": "employee"}, path=db)

    assert len(metrics.audit_trail(actor_id="EMP-1", path=db)) == 1
    assert len(metrics.audit_trail(path=db)) == 2


def test_audit_trail_omits_question_text(tmp_path):
    """التدقيق يحتاج الفعل والفاعل لا المحتوى."""
    import metrics
    db = tmp_path / "e.db"
    metrics.init_db(db)
    metrics.record("question", "سؤال حسّاس جداً", "agent",
                   actor={"employee_id": "EMP-1", "role": "employee"}, path=db)
    assert "question" not in metrics.audit_trail(path=db)[0]


def test_audit_trail_correlates_by_request(tmp_path):
    import metrics
    db = tmp_path / "e.db"
    metrics.init_db(db)
    metrics.record("question", "أ", "agent", request_id="req-9", path=db)
    metrics.record("upload", "ملف", request_id="req-9", path=db)
    metrics.record("question", "ب", "agent", request_id="req-other", path=db)
    assert len(metrics.audit_trail(request_id="req-9", path=db)) == 2


def test_record_without_actor_still_works(tmp_path):
    """التوافق الخلفي — الاستدعاءات القديمة بلا actor."""
    import metrics
    db = tmp_path / "e.db"
    metrics.init_db(db)
    metrics.record("question", "سؤال", "agent", path=db)
    assert metrics.summary(db)["questions"] == 1


def test_record_never_raises_on_bad_path():
    import metrics
    metrics.record("question", "س", path=Path("/nonexistent/dir/x.db"))


# ---------- حلّ مسار القاعدة عند الاستدعاء ----------
#
# النمط الخطر: `def f(path=DB_PATH)` يُقيَّم مرة عند الاستيراد،
# فإعادة تعيين DB_PATH بعدها بلا أثر — وتعمل الوحدة على قاعدة
# غير المقصودة بصمت. نفس العلّة التي أُصلحت في auth و metrics.

@pytest.fixture
def isolated_db_path(tmp_path, monkeypatch):
    """يعيد تعيين DB_PATH على مستوى الوحدة ويستعيده بعد الاختبار."""
    def _set(module, name="db.sqlite"):
        target = tmp_path / name
        monkeypatch.setattr(module, "DB_PATH", target)
        return target
    return _set


def test_migrate_respects_reassigned_db_path(isolated_db_path):
    import migrations
    target = isolated_db_path(migrations)

    migrations.migrate()                      # بلا تمرير مسار

    assert target.exists(), "الهجرة كتبت في قاعدة أخرى"
    assert not migrations.status()["pending"]


def test_migration_status_respects_reassigned_db_path(isolated_db_path):
    import migrations
    target = isolated_db_path(migrations)

    assert migrations.status()["pending"]     # قاعدة جديدة: كلها معلّقة
    migrations.migrate(target)
    assert not migrations.status()["pending"]


def test_build_respects_reassigned_db_path(isolated_db_path):
    import build_db
    target = isolated_db_path(build_db)

    build_db.build()                          # بلا تمرير مسار

    assert target.exists(), "الزرع كتب في قاعدة أخرى"
    assert build_db._count(target, "employees") == 6


def test_has_data_respects_reassigned_db_path(isolated_db_path):
    import build_db
    target = isolated_db_path(build_db)

    assert not build_db.has_data()            # لم تُنشأ بعد
    build_db.build(target)
    assert build_db.has_data()


def test_explicit_path_still_overrides_module_value(isolated_db_path, tmp_path):
    """التمرير الصريح يبقى أقوى من قيمة الوحدة."""
    import build_db
    isolated_db_path(build_db)
    explicit = tmp_path / "explicit.sqlite"

    build_db.build(explicit)

    assert explicit.exists()
    assert build_db._count(explicit, "employees") == 6


def test_default_arguments_are_not_bound_at_import():
    """فحص بنيوي: لا دالة تلتقط DB_PATH كقيمة افتراضية."""
    import ast
    import pathlib

    offenders = []
    for name in ("build_db.py", "migrations.py", "auth.py", "metrics.py"):
        tree = ast.parse(pathlib.Path(name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for default in node.args.defaults + [d for d in (node.args.kw_defaults or []) if d]:
                if isinstance(default, ast.Name) and default.id == "DB_PATH":
                    offenders.append(f"{name}:{node.name}")

    assert not offenders, f"قيم افتراضية مربوطة عند الاستيراد: {offenders}"
