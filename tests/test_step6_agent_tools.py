"""الخطوة 6 — أدوات المساعد التنظيمية وتكامل الوكيل.

الهيكل (المواصفة §14):

    تقنية المعلومات
    ├── مدير أ (EMP-301) ─┬─ موظف أ1 (EMP-311)
    │                     ├─ موظف أ2 (EMP-312)
    │                     └─ قائد ب  (EMP-320) ─┬─ موظف ب1 (EMP-321)
    │                                            └─ موظف ب2 (EMP-322)
    └── مدير ج (EMP-330) ─── موظف ج1 (EMP-331)      ← نفس القسم، فرع آخر
    المالية
    └── مدير د (EMP-340) ─── موظف د1 (EMP-341)
    مسؤول (EMP-100)
"""

import json
import sqlite3
from datetime import date, timedelta

import pytest

import agent
import build_db
import migrations
import org
import tools

A = {"employee_id": "EMP-301", "name": "مدير أ", "role": "manager"}
B = {"employee_id": "EMP-320", "name": "قائد ب", "role": "manager"}
C = {"employee_id": "EMP-330", "name": "مدير ج", "role": "manager"}
D = {"employee_id": "EMP-340", "name": "مدير د", "role": "manager"}
A1 = {"employee_id": "EMP-311", "name": "موظف أ1", "role": "employee"}
B1 = {"employee_id": "EMP-321", "name": "موظف ب1", "role": "employee"}
C1 = {"employee_id": "EMP-331", "name": "موظف ج1", "role": "employee"}
ADMIN = {"employee_id": "EMP-100", "name": "مسؤول النظام", "role": "admin"}

LEAKS = ("password_hash", "$2b$", "AUTH_SECRET", "Traceback", "SELECT ",
         "sqlite", ".db", "/tmp", "token")


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    path = tmp_path / "s6.db"
    build_db.build(path)
    conn = sqlite3.connect(path)
    conn.executemany(
        """INSERT INTO employees (employee_id, name, department, job_title,
                                  annual_leave, leave_used, email, hire_date)
           VALUES (?,?,?,?,?,?,?,?)""",
        [("EMP-301", "مدير أ", "تقنية المعلومات", "مدير", 30, 2, "a@x.example", "2020-01-01"),
         ("EMP-311", "موظف أ1", "تقنية المعلومات", "مهندس", 30, 3, "a1@x.example", "2021-01-01"),
         ("EMP-312", "موظف أ2", "تقنية المعلومات", "مهندس", 30, 3, "a2@x.example", "2021-01-01"),
         ("EMP-320", "قائد ب", "تقنية المعلومات", "رئيس فريق", 30, 2, "b@x.example", "2020-06-01"),
         ("EMP-321", "موظف ب1", "تقنية المعلومات", "مطوّر", 30, 3, "b1@x.example", "2022-01-01"),
         ("EMP-322", "موظف ب2", "تقنية المعلومات", "مطوّر", 30, 3, "b2@x.example", "2022-01-01"),
         ("EMP-330", "مدير ج", "تقنية المعلومات", "رئيس فريق", 30, 2, "c@x.example", "2020-01-01"),
         ("EMP-331", "موظف ج1", "تقنية المعلومات", "مهندس", 30, 3, "c1@x.example", "2022-01-01"),
         ("EMP-340", "مدير د", "المالية", "مدير", 30, 2, "d@x.example", "2019-01-01"),
         ("EMP-341", "موظف د1", "المالية", "محاسب", 30, 3, "d1@x.example", "2023-01-01"),
         ("EMP-100", "مسؤول النظام", "تقنية المعلومات", "مسؤول", 30, 2, "adm@x.example", "2018-01-01")])
    conn.commit(); conn.close()
    migrations.m005_organization_structure_backfill(path)
    monkeypatch.setattr(tools, "DB_PATH", path)
    monkeypatch.setattr(org, "DB_PATH", path)
    for emp, mgr in [("EMP-311", "EMP-301"), ("EMP-312", "EMP-301"), ("EMP-320", "EMP-301"),
                     ("EMP-321", "EMP-320"), ("EMP-322", "EMP-320"),
                     ("EMP-331", "EMP-330"), ("EMP-341", "EMP-340")]:
        org.assign_manager(emp, mgr, path=path)
    it = next(d for d in org.departments(path=path) if d["name"] == "تقنية المعلومات")
    org.assign_department_manager(it["department_id"], "EMP-301", path=path)
    return path


def ids(members):
    return {m["employee_id"] for m in members}


def no_leak(obj):
    text = json.dumps(obj, ensure_ascii=False)
    for bad in LEAKS:
        assert bad not in text, bad


def submit(actor, days=2, offset=10):
    start = (date.today() + timedelta(days=offset)).isoformat()
    r = tools.submit_leave_request(actor["name"], start, days, "سبب", actor=actor)
    assert r.get("submitted"), r
    return r["request_id"]


def row(db, rid):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT status, decided_by, decided_at FROM leave_requests"
                            " WHERE id = ?", (rid,)).fetchone()
    finally:
        conn.close()


def used(eid):
    return tools.leave_summary_for(eid)["leave_used"]


# ================= get_my_profile =================

@pytest.mark.parametrize("actor", [A1, A, ADMIN])
def test_profile_is_own_record(actor):
    p = tools.get_my_profile(actor)["profile"]
    assert p["employee_id"] == actor["employee_id"]
    assert p["role"] == actor["role"]


def test_profile_fields():
    p = tools.get_my_profile(B1)["profile"]
    assert (p["department"], p["job_title"], p["manager_name"]) == ("تقنية المعلومات", "مطوّر", "قائد ب")


def test_profile_without_identity_refused():
    r = tools.get_my_profile({"name": "x", "role": "admin"})
    assert r == {"authorized": False, "reason": "تعذّر التحقق من هويتك"}


def test_profile_for_account_without_record():
    r = tools.get_my_profile({"employee_id": "EMP-999", "name": "x", "role": "employee"})
    assert not r["found"]


# ================= get_my_manager =================

@pytest.mark.parametrize("actor,expected", [(A1, "مدير أ"), (B1, "قائد ب"),
                                            (B, "مدير أ"), (C1, "مدير ج")])
def test_manager_resolved_from_tree(actor, expected):
    assert tools.get_my_manager(actor)["manager"]["name"] == expected


def test_top_level_has_no_manager():
    r = tools.get_my_manager(A)
    assert r["has_manager"] is False and "manager" not in r


def test_manager_result_hides_personal_fields():
    m = tools.get_my_manager(A1)["manager"]
    assert set(m) == {"employee_id", "name", "job_title", "department"}


# ================= get_my_team =================

def test_team_employee_empty():
    assert tools.get_my_team(A1) == {"relationship": "direct_reports", "count": 0, "members": []}


def test_team_is_direct_only():
    """مدير أ يرى قائد ب — لا ب1 وب2 تحته."""
    assert ids(tools.get_my_team(A)["members"]) == {"EMP-311", "EMP-312", "EMP-320"}


def test_team_lead_sees_own_reports():
    assert ids(tools.get_my_team(B)["members"]) == {"EMP-321", "EMP-322"}


def test_team_marks_leads():
    m = {x["employee_id"]: x for x in tools.get_my_team(A)["members"]}
    assert m["EMP-320"]["direct_reports"] == 2 and m["EMP-311"]["direct_reports"] == 0


def test_team_same_department_peer_isolated():
    assert ids(tools.get_my_team(C)["members"]) == {"EMP-331"}
    assert not ids(tools.get_my_team(A)["members"]) & {"EMP-331"}


def test_team_admin_follows_api_contract():
    assert tools.get_my_team(ADMIN)["count"] == 0


def test_team_hides_email():
    no_leak(tools.get_my_team(A))
    assert "email" not in json.dumps(tools.get_my_team(A), ensure_ascii=False)


# ================= get_organization =================

@pytest.mark.parametrize("actor", [A1, A, B, C])
def test_organization_denied_below_admin(actor):
    assert tools.get_organization(actor) == {"authorized": False,
                                             "reason": "هيكل المؤسسة يتطلب صلاحية مسؤول"}


def test_organization_admin_summary():
    r = tools.get_organization(ADMIN)
    it = next(d for d in r["departments"] if d["department"] == "تقنية المعلومات")
    leads = {l["name"]: l["direct_reports"] for l in it["team_leads"]}
    assert leads == {"مدير أ": 3, "قائد ب": 2, "مدير ج": 1}
    assert it["manager"] == "مدير أ"
    assert r["totals"]["employees"] == len(org._query("SELECT employee_id FROM employees"))


def test_organization_is_summary_not_tree():
    text = json.dumps(tools.get_organization(ADMIN), ensure_ascii=False)
    assert "موظف أ1" not in text and "email" not in text
    no_leak(tools.get_organization(ADMIN))


def test_organization_counts_actual_employees(db):
    """لا العمود المخزّن: المالية 11 مخزّناً."""
    r = tools.get_organization(ADMIN)
    fin = next(d for d in r["departments"] if d["department"] == "المالية")
    stored = org.department_by_id(next(d["department_id"] for d in org.departments()
                                       if d["name"] == "المالية"))["headcount"]
    assert fin["headcount"] == org.department_headcount(
        next(d["department_id"] for d in org.departments() if d["name"] == "المالية"))
    assert fin["headcount"] != stored


# ================= G1 + G3: لا مسرّب وجود ولا أسماء خارج النطاق =================

def test_G1_employee_cannot_distinguish_existence():
    real = tools.get_employee("موظف ج1", A1)
    fake = tools.get_employee("زيد عمرو بكر", A1)
    assert real == fake == {"found": False, "message": tools.NOT_IN_SCOPE}


def test_G1_manager_cannot_distinguish_out_of_scope():
    assert tools.get_employee("موظف د1", A) == tools.get_employee("اسم لا وجود له", A)


def test_G1_balance_same_rule():
    assert tools.get_leave_balance("موظف ج1", A1) == tools.get_leave_balance("لا أحد", A1)


def test_G1_admin_keeps_precise_not_found():
    assert "لا يوجد موظف بالاسم" in tools.get_employee("لا أحد", ADMIN)["message"]


def test_G3_manager_matches_confined_to_subtree():
    """كان المدير يستلم أسماء المؤسسة كلها عند التطابق المتعدد."""
    r = tools.get_employee("موظف", A)
    assert set(r["matches"]) == {"موظف أ1", "موظف أ2", "موظف ب1", "موظف ب2"}


def test_G3_team_lead_matches_confined():
    assert set(tools.get_employee("موظف", B)["matches"]) == {"موظف ب1", "موظف ب2"}


def test_G3_admin_sees_all_matches():
    assert len(tools.get_employee("موظف", ADMIN)["matches"]) == 6


def test_single_in_scope_match_resolves():
    assert tools.get_employee("موظف ب1", A)["employee"]["employee_id"] == "EMP-321"


# ================= G2: التقديم للنفس فقط =================

def test_G2_manager_cannot_submit_for_report():
    before = used("EMP-311")
    start = (date.today() + timedelta(days=10)).isoformat()
    r = tools.submit_leave_request("موظف أ1", start, 5, "", actor=A)
    assert not r.get("submitted")
    assert used("EMP-311") == before
    assert tools.list_pending_requests(A)["count"] == 0


def test_G2_admin_cannot_submit_for_others():
    start = (date.today() + timedelta(days=10)).isoformat()
    assert not tools.preview_leave_request("موظف د1", start, 2, ADMIN)["valid"]


def test_G2_same_message_whether_name_exists():
    start = (date.today() + timedelta(days=10)).isoformat()
    a = tools.preview_leave_request("موظف ج1", start, 2, A1)["reason"]
    b = tools.preview_leave_request("اسم لا وجود له", start, 2, A1)["reason"]
    assert a == b and "موظف آخر" in a


@pytest.mark.parametrize("actor", [A1, A, ADMIN])
def test_G2_everyone_can_submit_for_self(actor):
    assert submit(actor)


def test_G2_identity_from_token_not_supplied_name():
    """اسم مكتوب بشكل جزئي يُحلّ إلى سجل الفاعل نفسه."""
    start = (date.today() + timedelta(days=10)).isoformat()
    r = tools.submit_leave_request("أ1", start, 2, "", actor=A1)
    assert r.get("submitted")
    assert tools.list_leave_requests("", A1)["requests"][0]["employee_name"] == "موظف أ1"


# ================= الأقسام =================

def dept_names(actor, name="تقنية المعلومات"):
    return {e["name"] for e in tools.list_department_employees(name, actor)["employees"]}


def test_roster_employee_denied():
    assert dept_names(A1) == set()


def test_roster_manager_subtree_only():
    assert dept_names(A) == {"موظف أ1", "موظف أ2", "قائد ب", "موظف ب1", "موظف ب2"}


def test_roster_team_lead_cannot_see_parent_branch():
    assert dept_names(B) == {"موظف ب1", "موظف ب2"}


def test_roster_same_department_peer_excluded():
    assert "موظف ج1" not in dept_names(A) and dept_names(C) == {"موظف ج1"}


def test_roster_admin_full():
    assert {"موظف أ1", "موظف ج1", "مدير ج"} <= dept_names(ADMIN)


def test_summary_uses_actual_counts():
    it = next(d for d in tools.department_summary(A)["departments"]
              if d["name"] == "تقنية المعلومات")
    assert it["headcount"] == 5


# ================= الإجازات =================

def test_self_review_denied_manager(db):
    rid = submit(A)
    assert not tools.review_leave_request(rid, "approve", "", A)["reviewed"]
    assert row(db, rid) == ("pending", None, None)


def test_self_review_denied_admin(db):
    rid = submit(ADMIN)
    assert not tools.review_leave_request(rid, "approve", "", ADMIN)["reviewed"]
    assert row(db, rid) == ("pending", None, None)


def test_valid_manager_review(db):
    rid = submit(B1)
    assert tools.review_leave_request(rid, "approve", "", B)["reviewed"]
    assert row(db, rid)[0] == "approved"


def test_parent_manager_reviews_indirect(db):
    rid = submit(B1)
    assert tools.review_leave_request(rid, "reject", "لا", A)["reviewed"]


def test_valid_admin_review_of_manager(db):
    rid = submit(A)
    assert tools.review_leave_request(rid, "approve", "", ADMIN)["reviewed"]


def test_forged_request_id_denied():
    assert not tools.review_leave_request(99999, "approve", "", A)["reviewed"]


def test_employee_cannot_review(db):
    rid = submit(B1)
    assert not tools.review_leave_request(rid, "approve", "", A1)["reviewed"]
    assert row(db, rid)[0] == "pending"


def test_manager_cannot_review_outside_scope(db):
    rid = submit(C1)
    assert not tools.review_leave_request(rid, "approve", "", A)["reviewed"]
    assert row(db, rid)[0] == "pending"


# ================= الموزّع: معاملات النموذج تُهمَل =================

FORGED = {"employee_id": "EMP-341", "manager_id": "EMP-340", "department_id": "DEPT-001",
          "role": "admin", "scope": "org", "actor_id": "EMP-100", "username": "rakan"}


@pytest.mark.parametrize("tool", ["get_my_profile", "get_my_manager", "get_my_team",
                                  "get_organization"])
def test_dispatcher_ignores_forged_args(tool):
    """نفس النتيجة بمعاملات مزوّرة أو بدونها — لا تصل إلى الأداة."""
    clean = agent._execute_tool(None, tool, {}, actor=A1)
    forged = agent._execute_tool(None, tool, dict(FORGED), actor=A1)
    assert clean == forged


def test_dispatcher_profile_stays_own():
    r = agent._execute_tool(None, "get_my_profile", dict(FORGED), actor=A1)
    assert r["profile"]["employee_id"] == "EMP-311" and r["profile"]["role"] == "employee"


def test_every_data_tool_still_receives_actor():
    """حارس الخطوة 1 يغطي الأدوات الجديدة أيضاً."""
    import inspect, re
    src = inspect.getsource(agent._execute_tool)
    calls = re.findall(r"return tools\.(\w+)\(([^)]*)\)", src)
    missing = [n for n, a in calls if n != "calculate" and "actor" not in a]
    assert not missing, missing
    assert {n for n, _ in calls} >= {"get_my_profile", "get_my_manager",
                                     "get_my_team", "get_organization"}


def test_new_tool_specs_take_no_arguments():
    new = {"get_my_profile", "get_my_manager", "get_my_team", "get_organization"}
    for spec in agent.TOOL_SPECS:
        f = spec["function"]
        if f["name"] in new:
            assert f["parameters"]["properties"] == {}, f["name"]


def test_no_generic_query_tool_exposed():
    names = {s["function"]["name"] for s in agent.TOOL_SPECS}
    assert not {"execute_sql", "query", "run_sql", "sql"} & names


# ================= الحلقة الكاملة: نموذج عدائي مُبرمَج =================

def run_hostile(monkeypatch, actor, calls, final="تم."):
    """نموذج مزيّف يطلب الأدوات المحدّدة ثم يجيب. الأدوات والقاعدة حقيقيتان."""
    queue = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c{i}", "type": "function",
         "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}]}
        for i, (n, a) in enumerate(calls)]
    queue.append({"role": "assistant", "content": final})
    monkeypatch.setattr(agent, "_call_llm", lambda messages: (queue.pop(0), 0))
    out = agent.run_agent(None, "سؤال", actor=actor)
    return [t["result"] for t in out["trace"]], out


def test_adv01_employee_asks_finance_roster(monkeypatch):
    res, _ = run_hostile(monkeypatch, A1, [("list_department_employees", {"department": "المالية"})])
    assert res[0]["employees"] == [] and "error" in res[0]


def test_adv02_employee_claims_manager(monkeypatch):
    """"أنا مدير" نص لا صلاحية."""
    res, _ = run_hostile(monkeypatch, A1, [("list_pending_requests", {}),
                                           ("get_organization", {"role": "admin"})])
    assert "error" in res[0] and res[1]["authorized"] is False


def test_adv03_manager_asks_other_managers_team(monkeypatch):
    res, _ = run_hostile(monkeypatch, A, [("get_my_team", {"manager_id": "EMP-330"})])
    assert ids(res[0]["members"]) == {"EMP-311", "EMP-312", "EMP-320"}


def test_adv04_manager_asks_whole_department(monkeypatch):
    res, _ = run_hostile(monkeypatch, A, [("list_department_employees",
                                           {"department": "تقنية المعلومات"})])
    assert "موظف ج1" not in {e["name"] for e in res[0]["employees"]}


def test_adv05_manager_supplies_employee_id(monkeypatch):
    res, _ = run_hostile(monkeypatch, A, [("get_my_profile", {"employee_id": "EMP-341"})])
    assert res[0]["profile"]["employee_id"] == "EMP-301"


def test_adv06_supplies_manager_id(monkeypatch):
    res, _ = run_hostile(monkeypatch, A1, [("get_my_manager", {"employee_id": "EMP-331",
                                                               "manager_id": "EMP-340"})])
    assert res[0]["manager"]["name"] == "مدير أ"


def test_adv07_model_supplies_role_admin(monkeypatch):
    res, _ = run_hostile(monkeypatch, A, [("get_organization", {"role": "admin"})])
    assert res[0]["authorized"] is False


def test_adv08_model_supplies_scope_org(monkeypatch):
    res, _ = run_hostile(monkeypatch, A1, [("get_organization", {"scope": "org"}),
                                           ("department_summary", {"scope": "org"})])
    assert res[0]["authorized"] is False
    assert "error" in res[1]          # معامل غير معروف ← رفض آمن، لا توسيع


def test_adv09_foreign_department_id(monkeypatch):
    res, _ = run_hostile(monkeypatch, A, [("list_department_employees",
                                           {"department": "المالية", "department_id": "DEPT-001"})])
    assert "error" in res[0] and "employees" not in res[0]


def test_adv10_admin_approves_own_leave(monkeypatch, db):
    rid = submit(ADMIN)
    res, _ = run_hostile(monkeypatch, ADMIN, [("review_leave_request",
                                               {"request_id": rid, "decision": "approve"})])
    assert not res[0]["reviewed"] and row(db, rid) == ("pending", None, None)


def test_adv11_manager_approves_own_leave(monkeypatch, db):
    """"وافق على طلبي" — النموذج يطلب، والخادم يرفض."""
    rid = submit(A)
    res, out = run_hostile(monkeypatch, A, [("review_leave_request",
                                             {"request_id": rid, "decision": "approve"})],
                           final="تمت الموافقة على طلبك.")
    assert not res[0]["reviewed"] and row(db, rid) == ("pending", None, None)


def test_adv12_other_employee_profile(monkeypatch):
    res, _ = run_hostile(monkeypatch, A1, [("get_employee", {"name": "موظف د1"})])
    assert res[0] == {"found": False, "message": tools.NOT_IN_SCOPE}


def test_adv13_proxy_submission_via_agent(monkeypatch):
    start = (date.today() + timedelta(days=10)).isoformat()
    before = used("EMP-311")
    res, _ = run_hostile(monkeypatch, A, [("submit_leave_request",
                                           {"name": "موظف أ1", "start_date": start, "days": 5})])
    assert not res[0].get("submitted") and used("EMP-311") == before


def test_adv14_repeated_calls_do_not_wear_down_authorization(monkeypatch, db):
    """تكرار الطلب حتى حدّ الخطوات لا يغيّر النتيجة."""
    rid = submit(A)
    calls = [("review_leave_request", {"request_id": rid, "decision": "approve"})] * agent.MAX_STEPS
    queue = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": f"r{i}", "type": "function",
         "function": {"name": n, "arguments": json.dumps(a)}}]} for i, (n, a) in enumerate(calls)]
    monkeypatch.setattr(agent, "_call_llm", lambda m: (queue.pop(0), 0))
    out = agent.run_agent(None, "وافق على طلبي", actor=A)
    assert len(out["trace"]) == agent.MAX_STEPS
    assert all(not t["result"]["reviewed"] for t in out["trace"])
    assert row(db, rid) == ("pending", None, None)


def test_action_guard_still_catches_false_claim(monkeypatch, db):
    """النموذج ادّعى "تم اعتماد طلبك بنجاح" والأداة رفضت."""
    rid = submit(A)
    _, out = run_hostile(monkeypatch, A, [("review_leave_request",
                                           {"request_id": rid, "decision": "approve"})],
                         final="تم اعتماد طلبك بنجاح.")
    assert out["answer"] != "تم اعتماد طلبك بنجاح."


def test_adversarial_results_leak_nothing(monkeypatch):
    res, _ = run_hostile(monkeypatch, A1, [
        ("get_my_profile", FORGED), ("get_my_team", FORGED), ("get_organization", FORGED),
        ("list_department_employees", {"department": "%"}),
        ("get_employee", {"name": "موظف"})])
    for r in res:
        for bad in ("password_hash", "$2b$", "AUTH_SECRET", "Traceback", "SELECT "):
            assert bad not in json.dumps(r, ensure_ascii=False), bad


# ================= اتساق الأدوات مع الواجهات البرمجية =================

def test_profile_matches_auth_me(api_client, employee, monkeypatch):
    monkeypatch.setattr(tools, "DB_PATH", api_client._db)
    monkeypatch.setattr(org, "DB_PATH", api_client._db)
    me = api_client.get("/auth/me", headers=employee).json()
    p = tools.get_my_profile({"employee_id": me["id"], "name": me["name"], "role": me["role"]})["profile"]
    for k in ("department", "department_id", "job_title", "manager_id", "manager_name",
              "employment_status", "email", "hire_date"):
        assert p[k] == me[k], k


def test_team_matches_me_team(api_client, manager, monkeypatch):
    monkeypatch.setattr(tools, "DB_PATH", api_client._db)
    monkeypatch.setattr(org, "DB_PATH", api_client._db)
    api = api_client.get("/me/team", headers=manager).json()
    me = api_client.get("/auth/me", headers=manager).json()
    tool = tools.get_my_team({"employee_id": me["id"], "name": me["name"], "role": me["role"]})
    assert ids(tool["members"]) == ids(api["members"]) and tool["count"] == api["count"]
    assert tool["count"] > 0


def test_organization_matches_org_structure(api_client, admin, monkeypatch):
    monkeypatch.setattr(tools, "DB_PATH", api_client._db)
    monkeypatch.setattr(org, "DB_PATH", api_client._db)
    api = api_client.get("/org/structure", headers=admin).json()
    tool = tools.get_organization({"employee_id": "EMP-100", "name": "م", "role": "admin"})
    assert tool["totals"] == api["totals"]
    assert {d["department"]: d["headcount"] for d in tool["departments"]} == \
           {d["name"]: d["headcount"] for d in api["departments"]}


# ================= سند ادّعاء التنفيذ =================
#
# الفجوة التي كانت موثّقة هنا بـ xfail: action_guard.verify كان يتلقّى
# كل أداة *استُدعيت*، فادّعاء «بنجاح» يمرّ ولو ردّت الأداة بالرفض.
# agent.py صار يمرّر أدوات الكتابة *الناجحة* وحدها.
#
# الطرفان يُقاسان معاً: حماية تصحّح كل شيء عديمة الفائدة تماماً
# كحماية لا تصحّح شيئاً. فلكل حالة رفض هنا حالة نجاح تقابلها.

CORRECTION_OPENING = "لم أنفّذ أي عملية بعد."


def test_write_success_map_covers_every_write_tool():
    """أداة كتابة بلا مفتاح تُعدّ فاشلة دائماً — نكشف النسيان هنا."""
    assert set(agent.WRITE_SUCCESS_KEY) == agent.WRITE_TOOLS


@pytest.mark.parametrize("name,key", [
    ("submit_leave_request", "submitted"),
    ("cancel_leave_request", "cancelled"),
    ("review_leave_request", "reviewed"),
])
def test_write_succeeded_reads_the_right_key(name, key):
    assert agent._write_succeeded(name, {key: True}) is True
    assert agent._write_succeeded(name, {key: False}) is False
    assert agent._write_succeeded(name, {}) is False


def test_write_succeeded_rejects_truthy_non_true():
    """'نعم' أو 1 ليسا إثباتاً للتنفيذ — نقارن بـ is True."""
    assert agent._write_succeeded("review_leave_request", {"reviewed": "نعم"}) is False
    assert agent._write_succeeded("review_leave_request", {"reviewed": 1}) is False


def test_write_succeeded_tolerates_non_dict():
    for junk in (None, "تم", 42, []):
        assert agent._write_succeeded("review_leave_request", junk) is False


def test_unknown_write_tool_counts_as_failure():
    """الاتجاه الآمن: المجهول لا يسند ادّعاءً."""
    assert agent._write_succeeded("delete_everything", {"done": True}) is False


# ---------- الرفض يُصحَّح ----------

def test_failed_self_review_does_not_support_claim(monkeypatch, db):
    """مدير يحاول اعتماد طلبه: الأداة ترفض، والادّعاء يُصحَّح."""
    rid = submit(A)
    res, out = run_hostile(monkeypatch, A,
                           [("review_leave_request", {"request_id": rid, "decision": "approve"})],
                           final="تم اعتماد طلبك بنجاح.")
    assert res[0]["reviewed"] is False
    assert out["corrected"] is True
    assert out["answer"].startswith(CORRECTION_OPENING)
    assert row(db, rid) == ("pending", None, None)


def test_failed_out_of_scope_review_does_not_support_claim(monkeypatch, db):
    """مدير ج يحاول اعتماد طلب موظف أ1 — خارج شجرته."""
    rid = submit(A1)
    res, out = run_hostile(monkeypatch, C,
                           [("review_leave_request", {"request_id": rid, "decision": "approve"})],
                           final="تم الاعتماد بنجاح.")
    assert res[0]["reviewed"] is False
    assert out["corrected"] is True
    assert row(db, rid) == ("pending", None, None)


def test_failed_cancel_does_not_support_claim(monkeypatch, db):
    """موظف أ1 يحاول إلغاء طلب موظف ب1."""
    rid = submit(B1)
    res, out = run_hostile(monkeypatch, A1,
                           [("cancel_leave_request", {"request_id": rid})],
                           final="تم إلغاء الطلب بنجاح.")
    assert res[0]["cancelled"] is False
    assert out["corrected"] is True
    assert row(db, rid)[0] == "pending"


def test_failed_submit_does_not_support_claim(monkeypatch, db):
    """موظف أ1 يحاول التقديم باسم موظف ب1."""
    start = (date.today() + timedelta(days=20)).isoformat()
    res, out = run_hostile(monkeypatch, A1,
                           [("submit_leave_request",
                             {"name": "موظف ب1", "start_date": start,
                              "days": 3, "reason": "سبب"})],
                           final="سجّلت الطلب بنجاح.")
    assert res[0]["submitted"] is False
    assert out["corrected"] is True


def test_read_tool_alone_does_not_support_claim(monkeypatch, db):
    """قراءة ناجحة ليست تنفيذاً."""
    _, out = run_hostile(monkeypatch, A, [("get_my_team", {})],
                         final="تم تسجيل طلبك بنجاح.")
    assert out["corrected"] is True


# ---------- النجاح يمرّ ----------

def test_successful_review_supports_claim(monkeypatch, db):
    """الطرف الآخر: اعتماد حقيقي يجب ألّا يُصحَّح."""
    rid = submit(A1)
    res, out = run_hostile(monkeypatch, A,
                           [("review_leave_request", {"request_id": rid, "decision": "approve"})],
                           final="تم اعتماد الطلب بنجاح.")
    assert res[0]["reviewed"] is True
    assert out["corrected"] is False
    assert out["answer"] == "تم اعتماد الطلب بنجاح."
    assert row(db, rid)[0] == "approved"


def test_successful_submit_supports_claim(monkeypatch, db):
    start = (date.today() + timedelta(days=25)).isoformat()
    res, out = run_hostile(monkeypatch, A1,
                           [("submit_leave_request",
                             {"name": "موظف أ1", "start_date": start,
                              "days": 2, "reason": "سبب"})],
                           final="تم تسجيل طلبك بنجاح.")
    assert res[0]["submitted"] is True
    assert out["corrected"] is False


def test_successful_cancel_supports_claim(monkeypatch, db):
    rid = submit(A1)
    res, out = run_hostile(monkeypatch, A1,
                           [("cancel_leave_request", {"request_id": rid})],
                           final="تم إلغاء الطلب بنجاح.")
    assert res[0]["cancelled"] is True
    assert out["corrected"] is False
    assert row(db, rid)[0] == "cancelled"


def test_one_success_among_failures_supports_claim(monkeypatch, db):
    """عملية واحدة نجحت فعلاً — الادّعاء مسنود ولو فشل ما قبله."""
    mine = submit(A)                 # طلب المدير نفسه: مراجعته مرفوضة
    theirs = submit(A1)              # طلب تابعه: مراجعته مسموحة
    res, out = run_hostile(monkeypatch, A, [
        ("review_leave_request", {"request_id": mine, "decision": "approve"}),
        ("review_leave_request", {"request_id": theirs, "decision": "approve"}),
    ], final="تمت المراجعة بنجاح.")
    assert (res[0]["reviewed"], res[1]["reviewed"]) == (False, True)
    assert out["corrected"] is False
    assert row(db, mine)[0] == "pending"
    assert row(db, theirs)[0] == "approved"


# ---------- بلا ادّعاء ----------

def test_no_claim_is_left_alone_after_failed_write(monkeypatch, db):
    """التصحيح يخصّ الادّعاء وحده — ردّ صادق بالفشل يمرّ كما هو."""
    rid = submit(A)
    _, out = run_hostile(monkeypatch, A,
                         [("review_leave_request", {"request_id": rid, "decision": "approve"})],
                         final="لا يمكنك مراجعة طلبك — يعتمده مديرك.")
    assert out["corrected"] is False
    assert out["answer"] == "لا يمكنك مراجعة طلبك — يعتمده مديرك."
