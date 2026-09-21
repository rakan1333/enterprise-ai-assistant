"""الخطوة 3.1 — تقوية فرض النطاق.

الثابت الحاكم: الصلاحية يحدّدها نطاق الفاعل المخزّن في الشجرة
التنظيمية — لا عضوية القسم، ولا ادّعاءات العميل، ولا معاملات
النموذج، ولا ظهور العناصر في الواجهة.

الهيكل (كل الموظفين في قسم تقنية المعلومات عدا فريق ج):

    مدير أ (رئيس القسم) ─┬─ موظف أ1
                         └─ موظف أ2 ── موظف أ3   (غير مباشر)
    مدير ب               ─── موظف ب1
    مدير ج (المالية)     ─── موظف ج1
    مدير د               (بلا تقارير)
"""

import base64
import json
import sqlite3
from datetime import date, timedelta

import pytest

import build_db
import migrations
import org
import tools

M_A = {"employee_id": "EMP-201", "name": "مدير أ", "role": "manager"}
M_B = {"employee_id": "EMP-202", "name": "مدير ب", "role": "manager"}
M_C = {"employee_id": "EMP-203", "name": "مدير ج", "role": "manager"}
M_D = {"employee_id": "EMP-204", "name": "مدير د", "role": "manager"}
E_A1 = {"employee_id": "EMP-211", "name": "موظف أ1", "role": "employee"}
E_A3 = {"employee_id": "EMP-213", "name": "موظف أ3", "role": "employee"}
E_B1 = {"employee_id": "EMP-221", "name": "موظف ب1", "role": "employee"}
E_C1 = {"employee_id": "EMP-231", "name": "موظف ج1", "role": "employee"}
ADMIN = {"employee_id": "EMP-100", "name": "مسؤول", "role": "admin"}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    db = tmp_path / "s31.db"
    build_db.build(db)
    conn = sqlite3.connect(db)
    conn.executemany(
        """INSERT INTO employees
           (employee_id, name, department, job_title, annual_leave, leave_used)
           VALUES (?,?,?,?,?,?)""",
        [("EMP-201", "مدير أ", "تقنية المعلومات", "مدير", 30, 0),
         ("EMP-202", "مدير ب", "تقنية المعلومات", "رئيس فريق", 30, 0),
         ("EMP-203", "مدير ج", "المالية", "مدير", 30, 0),
         ("EMP-204", "مدير د", "تقنية المعلومات", "رئيس فريق", 30, 0),
         ("EMP-211", "موظف أ1", "تقنية المعلومات", "مهندس", 30, 5),
         ("EMP-212", "موظف أ2", "تقنية المعلومات", "مهندس", 30, 5),
         ("EMP-213", "موظف أ3", "تقنية المعلومات", "مهندس", 30, 5),
         ("EMP-221", "موظف ب1", "تقنية المعلومات", "مهندس", 30, 5),
         ("EMP-231", "موظف ج1", "المالية", "محاسب", 30, 5),
         ("EMP-100", "مسؤول", "تقنية المعلومات", "مسؤول النظام", 30, 0)],
    )
    conn.commit(); conn.close()
    migrations.m005_organization_structure_backfill(db)

    monkeypatch.setattr(org, "DB_PATH", db)
    monkeypatch.setattr(tools, "DB_PATH", db)

    for emp, mgr in [("EMP-211", "EMP-201"), ("EMP-212", "EMP-201"),
                     ("EMP-213", "EMP-212"), ("EMP-221", "EMP-202"),
                     ("EMP-231", "EMP-203")]:
        org.assign_manager(emp, mgr, path=db)

    # مدير أ رئيس قسم تقنية المعلومات — ولا يمنحه ذلك شيئاً إضافياً
    it = next(d for d in org.departments(path=db) if d["name"] == "تقنية المعلومات")
    org.assign_department_manager(it["department_id"], "EMP-201", path=db)
    return db


def names(result):
    return {e["name"] for e in result.get("employees", [])}


def submit(name, actor, offset=10, days=2):
    start = (date.today() + timedelta(days=offset)).isoformat()
    return tools.submit_leave_request(name, start, days, "سبب", actor=actor)


def decide(actor_mgr, emp_name, decision="approve"):
    rid = next(r["id"] for r in tools.list_pending_requests(actor_mgr)["requests"]
               if r["employee_name"] == emp_name)
    tools.review_leave_request(rid, decision, "ملاحظة", actor_mgr)
    return rid


# ================= A. list_department_employees =================

def test_A01_employee_denied(tree):
    r = tools.list_department_employees("تقنية المعلومات", E_A1)
    assert r["employees"] == [] and "error" in r


def test_A02_manager_sees_own_subtree(tree):
    assert names(tools.list_department_employees("تقنية المعلومات", M_A)) == \
        {"موظف أ1", "موظف أ2", "موظف أ3"}


def test_A03_manager_cannot_see_other_managers_team(tree):
    assert "موظف ب1" not in names(tools.list_department_employees("تقنية المعلومات", M_A))
    assert "موظف أ1" not in names(tools.list_department_employees("تقنية المعلومات", M_B))


def test_A04_same_department_different_line_denied(tree):
    """مدير أ ومدير ب ومدير د في قسم واحد — لا تسرّب بينهم."""
    got = names(tools.list_department_employees("تقنية المعلومات", M_A))
    assert not got & {"موظف ب1", "مدير ب", "مدير د", "أحمد الشمري", "فهد المطيري"}


def test_A05_other_department_denied(tree):
    r = tools.list_department_employees("المالية", M_A)
    assert r["employees"] == [] and "error" in r


def test_A06_zero_report_manager_gets_empty(tree):
    r = tools.list_department_employees("تقنية المعلومات", M_D)
    assert r["employees"] == [] and r["count"] == 0


def test_A07_admin_sees_complete_department(tree):
    got = names(tools.list_department_employees("تقنية المعلومات", ADMIN))
    assert {"موظف أ1", "موظف ب1", "مدير ب", "مدير د", "أحمد الشمري"} <= got


def test_A08_department_head_gains_nothing_extra(tree):
    """رئاسة القسم لا تعني رؤية القسم كاملاً."""
    got = names(tools.list_department_employees("تقنية المعلومات", M_A))
    assert "موظف ب1" not in got and "مدير د" not in got


def test_A09_wildcard_stays_inside_subtree(tree):
    assert names(tools.list_department_employees("%", M_A)) == {"موظف أ1", "موظف أ2", "موظف أ3"}


# ================= B. department_summary =================

def test_B08_employee_denied(tree):
    r = tools.department_summary(E_A1)
    assert r["departments"] == [] and "error" in r


def test_B09_manager_summary_counts_only_subtree(tree):
    r = tools.department_summary(M_A)
    it = next(d for d in r["departments"] if d["name"] == "تقنية المعلومات")
    assert it["headcount"] == 3
    assert it["monthly_total"] == 3 * it["transport_allowance"]


def test_B10_manager_cannot_see_peer_totals(tree):
    a = tools.department_summary(M_A)["total_headcount"]
    b = tools.department_summary(M_B)["total_headcount"]
    assert (a, b) == (3, 1)


def test_B11_same_department_other_line_excluded(tree):
    """مخزّن القسم 24 — لو تسرّب لظهر هنا."""
    it = next(d for d in tools.department_summary(M_A)["departments"]
              if d["name"] == "تقنية المعلومات")
    assert it["headcount"] < 24


def test_B12_zero_report_manager_gets_zero(tree):
    r = tools.department_summary(M_D)
    assert r["departments"] == []
    assert r["total_headcount"] == 0 and r["total_monthly_transport"] == 0


def test_B13_admin_sees_organization_summary(tree):
    r = tools.department_summary(ADMIN)
    expected, _ = _independent_org_total(tree)
    assert len(r["departments"]) == 5
    assert r["total_monthly_transport"] == expected


def test_B13b_admin_and_manager_count_the_same_way(tree):
    """⚠️ الخطوة 5: مصدر واحد للعدد. قبلها رأى المسؤول العمود المخزّن
    (24 لتقنية المعلومات) والمدير العدد الفعلي — رقمان لقسم واحد."""
    adm = {d["name"]: d["headcount"] for d in tools.department_summary(ADMIN)["departments"]}
    it = org.department_headcount(
        next(d for d in org.departments(path=tree) if d["name"] == "تقنية المعلومات")["department_id"],
        path=tree)
    assert adm["تقنية المعلومات"] == it
    assert adm["تقنية المعلومات"] != 24


def test_B14_response_shape_unchanged(tree):
    for actor in (M_A, ADMIN):
        r = tools.department_summary(actor)
        assert set(r) >= {"departments", "total_headcount", "total_monthly_transport"}
        for d in r["departments"]:
            assert set(d) == {"name", "headcount", "transport_allowance", "monthly_total"}


# ================= D. recent_decisions =================

def _decisions_for_all(tree):
    submit("موظف أ1", E_A1, offset=10)
    submit("موظف أ3", E_A3, offset=12)
    submit("موظف ب1", E_B1, offset=14)
    submit("موظف ج1", E_C1, offset=16)
    decide(M_A, "موظف أ1")
    decide(M_A, "موظف أ3", "reject")
    decide(M_B, "موظف ب1")
    decide(M_C, "موظف ج1")


def dec_names(actor):
    return {d["employee_name"] for d in tools.recent_decisions(10, actor=actor)["decisions"]}


def test_D22_employee_denied(tree):
    _decisions_for_all(tree)
    r = tools.recent_decisions(10, actor=E_A1)
    assert r["decisions"] == [] and "error" in r


def test_D23_manager_sees_only_subtree(tree):
    _decisions_for_all(tree)
    assert dec_names(M_A) == {"موظف أ1", "موظف أ3"}


def test_D24_manager_cannot_see_peer_decisions(tree):
    _decisions_for_all(tree)
    assert "موظف ب1" not in dec_names(M_A)
    assert dec_names(M_B) == {"موظف ب1"}


def test_D25_manager_cannot_see_other_department(tree):
    _decisions_for_all(tree)
    assert "موظف ج1" not in dec_names(M_A)


def test_D26_admin_sees_all_decisions(tree):
    """كان المسؤول مرفوضاً: البوابة قارنت بـ "manager" حرفياً."""
    _decisions_for_all(tree)
    assert dec_names(ADMIN) == {"موظف أ1", "موظف أ3", "موظف ب1", "موظف ج1"}


def test_D27_limit_applies_after_scope(tree):
    """الحدّ على نطاق المدير — لا يُفرغه قرار خارجه."""
    _decisions_for_all(tree)
    r = tools.recent_decisions(1, actor=M_A)
    assert r["count"] == 1
    assert r["decisions"][0]["employee_name"] in {"موظف أ1", "موظف أ3"}


def test_D28_ordering_and_metadata_preserved(tree):
    _decisions_for_all(tree)
    rows = tools.recent_decisions(10, actor=ADMIN)["decisions"]
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids, reverse=True)
    for r in rows:
        assert {"decision_note", "decided_at", "decided_by_name", "reason"} <= set(r)
        assert "employee_id" not in r


def test_D29_zero_report_manager_sees_no_decisions(tree):
    _decisions_for_all(tree)
    assert tools.recent_decisions(10, actor=M_D)["decisions"] == []


# ================= E. IDOR / escalation (tools) =================

def test_E27_forged_employee_id_in_payload(tree):
    """معرّف مزيّف في الهوية يغيّر من هو الفاعل — لا يوسّع ما يراه."""
    forged = dict(E_A1, employee_id="EMP-221")
    assert not tools.get_employee("موظف أ1", forged).get("found")


def test_E28_forged_manager_id_claim(tree):
    forged = dict(M_A, manager_id="EMP-202")
    assert "موظف ب1" not in names(tools.list_department_employees("تقنية المعلومات", forged))


def test_E29_forged_scope_org(tree):
    forged = dict(M_A, scope="org")
    assert not tools.get_employee("موظف ب1", forged).get("found")
    assert tools.department_summary(forged)["total_headcount"] == 3


def test_E30_forged_team_claim(tree):
    forged = dict(M_A, team=["EMP-221", "EMP-231"])
    assert "موظف ب1" not in dec_names(forged) | names(
        tools.list_department_employees("تقنية المعلومات", forged))


@pytest.mark.parametrize("bad", ["", " ", "EMP-", "EMP-99999", "'; --", None])
def test_E32_malformed_employee_id(tree, bad):
    forged = {"employee_id": bad, "name": "x", "role": "manager"}
    assert tools.list_department_employees("تقنية المعلومات", forged)["employees"] == []
    assert tools.department_summary(forged)["total_headcount"] == 0


@pytest.mark.parametrize("bad", ["", "%%", "DEPT-999", "'; DROP TABLE employees; --", "\\"])
def test_E33_malformed_department(tree, bad):
    r = tools.list_department_employees(bad, M_A)
    assert not ({e["name"] for e in r["employees"]} - {"موظف أ1", "موظف أ2", "موظف أ3"})


def test_E34_zero_report_manager_never_falls_back(tree):
    """لا تراجع إلى المؤسسة ولا القسم — في أي أداة."""
    submit("موظف أ1", E_A1)
    assert tools.list_pending_requests(M_D)["count"] == 0
    assert tools.department_summary(M_D)["total_headcount"] == 0
    assert tools.list_department_employees("تقنية المعلومات", M_D)["count"] == 0


def test_E35_indirect_report_included_everywhere(tree):
    submit("موظف أ3", E_A3)
    assert "موظف أ3" in names(tools.list_department_employees("تقنية المعلومات", M_A))
    assert any(r["employee_name"] == "موظف أ3"
               for r in tools.list_pending_requests(M_A)["requests"])


def test_E36_same_department_different_manager_everywhere(tree):
    submit("موظف ب1", E_B1)
    assert "موظف ب1" not in names(tools.list_department_employees("تقنية المعلومات", M_A))
    assert not any(r["employee_name"] == "موظف ب1"
                   for r in tools.list_pending_requests(M_A)["requests"])
    assert not tools.get_employee("موظف ب1", M_A).get("found")


def test_E37_state_unchanged_after_denied_review(tree):
    submit("موظف ب1", E_B1)
    rid = tools.list_pending_requests(M_B)["requests"][0]["id"]
    assert not tools.review_leave_request(rid, "approve", "", M_A)["reviewed"]
    assert tools.list_pending_requests(M_B)["requests"][0]["id"] == rid
    assert dec_names(ADMIN) == set()


# ================= C. /auth/users (HTTP) =================

def test_C14_employee_denied_users(api_client, employee):
    assert api_client.get("/auth/users", headers=employee).status_code == 403


def test_C15_manager_denied_users(api_client, manager):
    assert api_client.get("/auth/users", headers=manager).status_code == 403


def test_C16_admin_allowed_users(api_client, admin):
    assert api_client.get("/auth/users", headers=admin).status_code == 200


def test_C16b_unauthenticated_users(api_client):
    assert api_client.get("/auth/users").status_code == 401


def test_C17_manager_cannot_create_user(api_client, manager, admin):
    r = api_client.post("/auth/users", headers=manager,
                        json={"username": "mgrmade", "employee_name": "حساب من مدير"})
    assert r.status_code == 403
    listed = {u["username"] for u in api_client.get("/auth/users", headers=admin).json()}
    assert "mgrmade" not in listed


def test_C18_manager_cannot_delete_user(api_client, manager, admin):
    r = api_client.delete("/auth/users/ahmed", headers=manager)
    assert r.status_code == 403
    listed = {u["username"] for u in api_client.get("/auth/users", headers=admin).json()}
    assert "ahmed" in listed


def test_C19_manager_cannot_enumerate_users(api_client, manager):
    r = api_client.get("/auth/users", headers=manager)
    assert r.status_code == 403
    assert "ahmed" not in r.text and "khalid" not in r.text


def test_C20_manager_cannot_create_admin(api_client, manager, admin):
    """الثغرة المُكتشَفة: مدير كان ينشئ حساب مسؤول ويدخل به."""
    r = api_client.post("/auth/users", headers=manager,
                        json={"username": "fakeadmin", "employee_name": "مسؤول مزيّف",
                              "role": "admin", "password": "Escalate123!"})
    assert r.status_code == 403
    login = api_client.post("/auth/login",
                            json={"username": "fakeadmin", "password": "Escalate123!"})
    assert login.status_code == 401
    listed = {u["username"] for u in api_client.get("/auth/users", headers=admin).json()}
    assert "fakeadmin" not in listed


def test_C20b_admin_can_create_and_delete(api_client, admin):
    r = api_client.post("/auth/users", headers=admin,
                        json={"username": "tempuser", "employee_name": "حساب مؤقت"})
    assert r.status_code == 201
    assert api_client.delete("/auth/users/tempuser", headers=admin).status_code == 204


def test_C20c_manager_cannot_escalate_self(api_client, manager, admin):
    """لا إعادة إنشاء لحسابه بدور أعلى."""
    r = api_client.post("/auth/users", headers=manager,
                        json={"username": "noura", "employee_name": "نورة العتيبي",
                              "role": "admin"})
    assert r.status_code == 403
    me = api_client.get("/auth/me", headers=manager).json()
    assert me["role"] == "manager"


def _b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def test_C21_forged_role_claim_rejected(api_client, manager):
    """تعديل الدور داخل الرمز يكسر التوقيع."""
    token = manager["Authorization"].split(" ", 1)[1]
    head, body, sig = token.split(".")
    pad = body + "=" * (-len(body) % 4)
    claims = json.loads(base64.urlsafe_b64decode(pad))
    claims["role"] = "admin"
    forged = f"{head}.{_b64(claims)}.{sig}"
    r = api_client.get("/auth/users", headers={"Authorization": "Bearer " + forged})
    assert r.status_code == 401


def test_E31_token_signed_with_other_key_rejected(api_client):
    from jose import jwt
    evil = jwt.encode({"sub": "x", "role": "admin", "employee_id": "EMP-100",
                       "name": "x", "exp": 9999999999}, "not-the-secret" * 4,
                      algorithm="HS256")
    for path in ("/auth/users", "/stats", "/leave/decisions"):
        assert api_client.get(path, headers={"Authorization": "Bearer " + evil}
                              ).status_code == 401, path


def test_E31b_unsigned_token_rejected(api_client):
    """alg=none — الهجوم الكلاسيكي على JWT."""
    token = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'sub': 'x', 'role': 'admin'})}."
    assert api_client.get("/auth/users",
                          headers={"Authorization": "Bearer " + token}).status_code == 401


# ================= /stats (HTTP) =================

def test_stats_employee_denied(api_client, employee):
    assert api_client.get("/stats", headers=employee).status_code == 403


def test_stats_manager_denied(api_client, manager):
    """الإحصاءات تعرض أسئلة موظفي المؤسسة كلها — خارج نطاق الفريق."""
    assert api_client.get("/stats", headers=manager).status_code == 403


def test_stats_admin_allowed(api_client, admin):
    assert api_client.get("/stats", headers=admin).status_code == 200


def test_stats_unauthenticated(api_client):
    assert api_client.get("/stats").status_code == 401


# ================= /leave/decisions (HTTP) =================

def test_decisions_http_admin_allowed(api_client, admin):
    """المسؤول كان يحصل على خطأ من الأداة رغم اجتياز البوابة."""
    r = api_client.get("/leave/decisions", headers=admin)
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_decisions_http_employee_denied(api_client, employee):
    assert api_client.get("/leave/decisions", headers=employee).status_code == 403


def _independent_org_total(db_path):
    """الإجمالي محسوباً مستقلاً عن الأداة: الموظفون النشطون × بدل قسمهم.

    لا رقم ثابت: الأداة تُقارَن بحساب SQL منفصل، فأي خطأ في
    تجميعها — كاقتطاع أقسام كما فعل RAG — يُكشف.
    """
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            """SELECT COALESCE(SUM(d.transport_allowance), 0), COUNT(e.employee_id)
                 FROM employees e JOIN departments d ON d.department_id = e.department_id
                WHERE COALESCE(e.employment_status, 'active') = 'active'"""
        ).fetchone()
    finally:
        conn.close()
