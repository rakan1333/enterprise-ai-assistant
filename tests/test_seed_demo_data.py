"""بيانات المؤسسة التجريبية: الحتمية والتكرار والعلاقات.

الغرض من الزرع أن تصير الخطوة التالية قابلة للبناء والاختبار:
تضييق صلاحية المدير إلى فريقه يحتاج فِرقاً موجودة فعلاً.
"""

import sqlite3

import pytest

import auth
import build_db
import migrations
import org
import seed_demo_data as seed


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "seed.db"
    build_db.build(path)
    monkeypatch.setattr(org, "DB_PATH", path)
    monkeypatch.setattr(seed, "DB_PATH", path)
    return path


@pytest.fixture
def seeded(db):
    seed.seed(db)
    return db


def q(path, sql, params=()):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def demo_ids(path):
    return [r["employee_id"] for r in q(
        path, "SELECT employee_id FROM employees"
              " WHERE CAST(SUBSTR(employee_id,5) AS INTEGER) >= ?", (seed.DEMO_ID_START,))]


def manager_ids(path):
    return sorted({r["manager_id"] for r in q(
        path, "SELECT DISTINCT manager_id FROM employees WHERE manager_id IS NOT NULL")})


# ---------- الخطة قبل الكتابة ----------

def test_plan_is_pure_and_deterministic():
    """نفس البذرة تعطي نفس المؤسسة — بلا لمس القاعدة."""
    a, b = seed.plan(), seed.plan()
    assert [p["name"] for p in a["roster"]] == [p["name"] for p in b["roster"]]
    assert [p["employee_id"] for p in a["roster"]] == [p["employee_id"] for p in b["roster"]]


def test_plan_targets_twelve_departments():
    assert len(seed.plan()["departments"]) == 12


def test_plan_totals_about_180_employees():
    total = seed.plan()["total"]
    assert 170 <= total <= 190


def test_plan_names_are_unique():
    names = [p["name"] for p in seed.plan()["roster"]]
    assert len(names) == len(set(names))


def test_plan_reuses_existing_department_names():
    """لا قسم مكرّر بمعنى مختلف."""
    planned = {d["name"] for d in seed.plan()["departments"]}
    existing = {name for name, _, _ in build_db.DEPARTMENTS}
    assert existing <= planned


def test_scale_shrinks_the_organization():
    assert seed.plan(0.5)["total"] < seed.plan(1.0)["total"]


def test_scale_never_drops_below_manager_count():
    """قسم لا يصغر حتى يفقد مديره."""
    for dept in seed.plan(0.05)["departments"]:
        assert dept["size"] > dept["managers"]


# ---------- الزرع ----------

def test_seed_creates_twelve_departments(seeded):
    assert len(org.departments(path=seeded)) == 12


def test_seed_creates_expected_employee_count(seeded):
    assert len(q(seeded, "SELECT employee_id FROM employees")) == 186   # 180 + 6 قائمين


def test_seed_creates_twelve_to_fifteen_managers(seeded):
    assert 12 <= len(manager_ids(seeded)) <= 15


def test_every_seeded_employee_has_a_department(seeded):
    missing = q(seeded, "SELECT employee_id FROM employees WHERE department_id IS NULL")
    assert missing == []


def test_every_manager_has_at_least_one_report(seeded):
    for mid in manager_ids(seeded):
        assert org.direct_reports(mid, path=seeded), mid


def test_department_managers_are_assigned(seeded):
    assigned = [d for d in org.departments(path=seeded) if d["manager_id"]]
    assert len(assigned) == 12


def test_department_manager_is_a_real_employee(seeded):
    for d in org.departments(path=seeded):
        assert org.employee_by_id(d["manager_id"], path=seeded) is not None


def test_seeded_employees_have_email_and_hire_date(seeded):
    rows = q(seeded, "SELECT email, hire_date FROM employees"
                     " WHERE CAST(SUBSTR(employee_id,5) AS INTEGER) >= ?",
             (seed.DEMO_ID_START,))
    assert all(r["email"] and r["hire_date"] for r in rows)


# ---------- فصل الفرق ----------

def test_two_managers_share_a_department_with_separate_teams(seeded):
    """نفس القسم، خطّا رفع مختلفان — شرط اختبار الخطوة 3."""
    shared = None
    for d in org.departments(path=seeded):
        members = org.employees_in_department(d["department_id"], path=seeded)
        leads = {m["employee_id"] for m in members
                 if org.direct_reports(m["employee_id"], path=seeded)}
        if len(leads) >= 2:
            shared = (d, sorted(leads))
            break

    assert shared, "لا قسم فيه مديران — الخطوة 3 لن تكون قابلة للاختبار"
    dept, leads = shared
    team_a = set(org.team_member_ids(leads[0], path=seeded))
    team_b = set(org.team_member_ids(leads[1], path=seeded))
    assert team_a and team_b
    assert not (team_a & team_b)


def test_manager_does_not_manage_whole_department(seeded):
    """المدير يدير فريقه لا قسمه كاملاً."""
    for d in org.departments(path=seeded):
        members = org.employees_in_department(d["department_id"], path=seeded)
        leads = [m["employee_id"] for m in members
                 if org.direct_reports(m["employee_id"], path=seeded)]
        if len(leads) < 2:
            continue
        for lead in leads:
            assert len(org.team_member_ids(lead, path=seeded)) < len(members)


def test_teams_do_not_span_departments(seeded):
    """تقارير المدير في قسمه — لا خلط بين الأقسام."""
    for mid in manager_ids(seeded):
        mgr = org.employee_by_id(mid, path=seeded)
        if mgr["employee_id"] in ("EMP-001", "EMP-002", "EMP-003",
                                  "EMP-004", "EMP-005", "EMP-006"):
            continue
        for r in org.direct_reports(mid, path=seeded):
            assert r["department_id"] == mgr["department_id"]


def test_multiple_job_titles_per_department(seeded):
    for d in org.departments(path=seeded):
        titles = {m["job_title"] for m in
                  org.employees_in_department(d["department_id"], path=seeded)}
        assert len(titles) >= 2, d["name"]


# ---------- الحتمية ----------

def test_two_runs_produce_identical_organization(tmp_path, monkeypatch):
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    for path in (first, second):
        build_db.build(path)
        monkeypatch.setattr(org, "DB_PATH", path)
        seed.seed(path)

    shape = lambda p: q(p, "SELECT employee_id, name, department, job_title, manager_id"
                           " FROM employees ORDER BY employee_id")
    assert shape(first) == shape(second)


def test_department_ids_match_across_runs(tmp_path, monkeypatch):
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    for path in (first, second):
        build_db.build(path)
        monkeypatch.setattr(org, "DB_PATH", path)
        seed.seed(path)

    pair = lambda p: {d["name"]: d["department_id"] for d in org.departments(path=p)}
    assert pair(first) == pair(second)


# ---------- التكرار ----------

def test_seed_is_idempotent(seeded):
    before = q(seeded, "SELECT * FROM employees ORDER BY employee_id")
    seed.seed(seeded)
    seed.seed(seeded)
    assert q(seeded, "SELECT * FROM employees ORDER BY employee_id") == before


def test_repeated_seed_does_not_duplicate_departments(seeded):
    seed.seed(seeded)
    names = [d["name"] for d in org.departments(path=seeded)]
    assert len(names) == len(set(names)) == 12


def test_repeated_seed_keeps_manager_links(seeded):
    before = {m: sorted(org.team_member_ids(m, path=seeded)) for m in manager_ids(seeded)}
    seed.seed(seeded)
    after = {m: sorted(org.team_member_ids(m, path=seeded)) for m in manager_ids(seeded)}
    assert before == after


# ---------- حفظ البيانات القائمة ----------

def test_existing_employees_survive(seeded):
    originals = {e[0] for e in build_db.EMPLOYEES}
    present = {r["employee_id"] for r in q(seeded, "SELECT employee_id FROM employees")}
    assert originals <= present


def test_existing_employee_facts_are_untouched(db):
    before = q(db, "SELECT employee_id, name, department, job_title, annual_leave,"
                   " leave_used FROM employees ORDER BY employee_id")
    seed.seed(db)
    after = q(db, "SELECT employee_id, name, department, job_title, annual_leave,"
                  " leave_used FROM employees"
                  " WHERE CAST(SUBSTR(employee_id,5) AS INTEGER) < ?"
                  " ORDER BY employee_id", (seed.DEMO_ID_START,))
    assert before == after


def test_existing_employees_get_no_invented_details(seeded):
    """بريد أو تاريخ توظيف مختلَق لموظف حقيقي = بيانات كاذبة."""
    rows = q(seeded, "SELECT email, hire_date FROM employees"
                     " WHERE CAST(SUBSTR(employee_id,5) AS INTEGER) < ?",
             (seed.DEMO_ID_START,))
    assert all(r["email"] is None and r["hire_date"] is None for r in rows)


def test_existing_employees_are_linked_to_a_manager(seeded):
    """الإسناد بيانات تجريبية موثّقة — لكنه يجعلهم جزءاً من الهيكل."""
    linked = q(seeded, "SELECT employee_id FROM employees"
                       " WHERE CAST(SUBSTR(employee_id,5) AS INTEGER) < ?"
                       "   AND manager_id IS NOT NULL", (seed.DEMO_ID_START,))
    assert len(linked) >= 5


def test_existing_manager_account_gets_no_superior(db):
    """من دوره مدير في users يبقى بلا مدير أعلى — حقيقة قائمة."""
    auth.init_db(db)
    auth.create_user("noura", "EMP-002", "نورة العتيبي", "Manager@123", "manager", db)
    seed.seed(db)
    assert org.employee_by_id("EMP-002", path=db)["manager_id"] is None


def test_existing_leave_requests_survive(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO leave_requests (employee_id, employee_name, start_date,"
                 " end_date, days, reason, status, created_at) VALUES"
                 " ('EMP-003','خالد الدوسري','2026-10-01','2026-10-03',3,"
                 "  'ظروف عائلية','pending','2026-09-01')")
    conn.commit(); conn.close()

    seed.seed(db)

    req = q(db, "SELECT * FROM leave_requests")
    assert len(req) == 1
    assert req[0]["reason"] == "ظروف عائلية" and req[0]["status"] == "pending"


def test_existing_users_survive(db):
    auth.init_db(db)
    auth.create_user("khalid", "EMP-003", "خالد الدوسري", "Employee@123", "employee", db)
    seed.seed(db)
    assert auth.authenticate("khalid", "Employee@123", db)["employee_name"] == "خالد الدوسري"


# ---------- السلامة ----------

def test_integrity_is_clean_after_seed(seeded):
    assert migrations.validate_integrity(seeded)["ok"]


def test_no_self_managers(seeded):
    assert q(seeded, "SELECT employee_id FROM employees"
                     " WHERE manager_id = employee_id") == []


def test_no_management_cycles(seeded):
    for r in q(seeded, "SELECT employee_id FROM employees"):
        chain = org.management_chain(r["employee_id"], path=seeded)
        ids = [m["employee_id"] for m in chain]
        assert len(ids) == len(set(ids))
        assert r["employee_id"] not in ids


def test_every_manager_reference_resolves(seeded):
    ids = {r["employee_id"] for r in q(seeded, "SELECT employee_id FROM employees")}
    refs = {r["manager_id"] for r in q(
        seeded, "SELECT manager_id FROM employees WHERE manager_id IS NOT NULL")}
    assert refs <= ids


def test_seed_uses_integrity_layer(db, monkeypatch):
    """الإسناد يمرّ بـ org لا بكتابة مباشرة — فتُفحص قواعده."""
    calls = []
    original = org.assign_manager

    def spy(employee_id, manager_id, path=None):
        calls.append((employee_id, manager_id))
        return original(employee_id, manager_id, path)

    monkeypatch.setattr(org, "assign_manager", spy)
    seed.seed(db)
    assert len(calls) > 100


# ---------- المسؤول ----------

def test_admin_role_is_accepted_by_auth():
    assert "admin" in auth.ROLES


def test_admin_account_is_created(db):
    auth.init_db(db)
    seed.seed(db)
    account = seed.seed_admin_account(db, password="AdminDemo123!")

    assert account["role"] == "admin"
    user = auth.authenticate("rakan", "AdminDemo123!", db)
    assert user["role"] == "admin"
    assert user["employee_id"] == seed.ADMIN_ID


def test_admin_account_is_not_recreated(db):
    auth.init_db(db)
    seed.seed(db)
    assert seed.seed_admin_account(db, password="AdminDemo123!") is not None
    assert seed.seed_admin_account(db, password="Other123!") is None


def test_admin_has_an_employee_record(db):
    auth.init_db(db)
    seed.seed(db)
    assert org.employee_by_id(seed.ADMIN_ID, path=db) is not None


def test_no_name_is_hard_coded_into_authorization():
    """الصلاحية من الدور لا من الاسم."""
    import inspect
    for module in (auth, org):
        src = inspect.getsource(module)
        assert "ركان" not in src
        assert "rakan" not in src.lower()


# ---------- الزرع لا يمنح صلاحيات ----------

def test_seed_does_not_grant_org_wide_manager_access(seeded):
    """الزرع بيانات لا صلاحيات — المدير لا يرى الجميع بسببه."""
    import tools
    tools.DB_PATH = seeded

    mid = manager_ids(seeded)[0]
    reports = org.team_member_ids(mid, path=seeded)
    everyone = [r["employee_id"] for r in q(seeded, "SELECT employee_id FROM employees")]
    assert len(reports) < len(everyone)


def test_seed_does_not_change_role_semantics():
    """is_manager لم يتغيّر — الخطوة 3 مسؤولة عن ذلك."""
    assert auth.is_manager({"role": "manager"}) is True
    assert auth.is_manager({"role": "admin"}) is False
    assert auth.is_manager({"role": "employee"}) is False


def test_seed_creates_no_user_accounts_for_demo_employees(seeded):
    """180 موظفاً بلا حسابات دخول — الزرع لا يفتح أبواباً."""
    auth.init_db(seeded)
    accounts = {u["employee_id"] for u in auth.list_users(seeded)}
    demo = set(demo_ids(seeded)) - {seed.ADMIN_ID}
    assert not (accounts & demo)


# ---------- الحذف ----------

def test_purge_removes_only_demo_rows(seeded):
    seed.purge_demo_rows(seeded)

    remaining = {r["employee_id"] for r in q(seeded, "SELECT employee_id FROM employees")}
    assert remaining == {e[0] for e in build_db.EMPLOYEES}


def test_purge_keeps_leave_requests(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO leave_requests (employee_id, employee_name, start_date,"
                 " end_date, days, status, created_at) VALUES"
                 " ('EMP-003','خالد','2026-10-01','2026-10-03',3,'pending','2026-09-01')")
    conn.commit(); conn.close()

    seed.seed(db)
    seed.purge_demo_rows(db)
    assert len(q(db, "SELECT * FROM leave_requests")) == 1


def test_purge_leaves_no_dangling_references(seeded):
    seed.purge_demo_rows(seeded)
    assert migrations.validate_integrity(seeded)["ok"]


def test_seed_after_purge_restores_same_structure(seeded):
    before = q(seeded, "SELECT employee_id, name, manager_id FROM employees"
                       " ORDER BY employee_id")
    seed.purge_demo_rows(seeded)
    seed.seed(seeded)
    after = q(seeded, "SELECT employee_id, name, manager_id FROM employees"
                      " ORDER BY employee_id")
    assert before == after
