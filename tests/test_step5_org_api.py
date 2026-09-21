"""الخطوة 5 — نقاط التنظيم والموظف عبر HTTP.

الهيكل (فوق قاعدة التجهيزات المشتركة):

    تقنية المعلومات
    ├── مدير أ (EMP-201) ─┬─ موظف أ1 (EMP-211)
    │                     └─ موظف أ2 (EMP-212) ── موظف أ3 (EMP-213)   ← غير مباشر
    ├── مدير ب (EMP-202) ─── موظف ب1 (EMP-221)                        ← نفس القسم
    └── مدير د (EMP-204)                                              ← بلا تقارير
    المالية
    └── مدير ج (EMP-203) ─── موظف ج1 (EMP-231)
"""

import base64
import json
import sqlite3
from datetime import date, timedelta

import pytest

import auth
import org
import tools

PW = "Step5Pass!23"
FORBIDDEN = ("password_hash", "$2b$", "Traceback", "sqlite3", "SELECT ", "AUTH_SECRET")


@pytest.fixture(scope="module")
def tree(api_client):
    db = api_client._db
    conn = sqlite3.connect(db)
    conn.executemany(
        """INSERT OR IGNORE INTO employees
           (employee_id, name, department, job_title, annual_leave, leave_used)
           VALUES (?,?,?,?,?,?)""",
        [("EMP-201", "مدير أ", "تقنية المعلومات", "مدير", 30, 2),
         ("EMP-202", "مدير ب", "تقنية المعلومات", "رئيس فريق", 30, 2),
         ("EMP-203", "مدير ج", "المالية", "مدير", 30, 2),
         ("EMP-204", "مدير د", "تقنية المعلومات", "رئيس فريق", 30, 2),
         ("EMP-211", "موظف أ1", "تقنية المعلومات", "مهندس", 30, 2),
         ("EMP-212", "موظف أ2", "تقنية المعلومات", "مهندس أول", 30, 2),
         ("EMP-213", "موظف أ3", "تقنية المعلومات", "مهندس", 30, 2),
         ("EMP-221", "موظف ب1", "تقنية المعلومات", "مهندس", 30, 2),
         ("EMP-231", "موظف ج1", "المالية", "محاسب", 30, 2),
         ("EMP-100", "مسؤول النظام", "تقنية المعلومات", "مسؤول", 30, 2)])
    conn.commit(); conn.close()

    import migrations
    migrations.m005_organization_structure_backfill(db)
    for emp, mgr in [("EMP-211", "EMP-201"), ("EMP-212", "EMP-201"),
                     ("EMP-213", "EMP-212"), ("EMP-221", "EMP-202"),
                     ("EMP-231", "EMP-203")]:
        org.assign_manager(emp, mgr, path=db)
    it = next(d for d in org.departments(path=db) if d["name"] == "تقنية المعلومات")
    org.assign_department_manager(it["department_id"], "EMP-201", path=db)

    accounts = {"mgr_a": ("EMP-201", "مدير أ", "manager"),
                "mgr_b": ("EMP-202", "مدير ب", "manager"),
                "mgr_c": ("EMP-203", "مدير ج", "manager"),
                "mgr_d": ("EMP-204", "مدير د", "manager"),
                "emp_a1": ("EMP-211", "موظف أ1", "employee"),
                "orphan": ("EMP-999", "حساب بلا سجل", "employee")}
    headers = {}
    for user, (eid, name, role) in accounts.items():
        if not auth.user_exists(user, db):
            auth.create_user(user, eid, name, PW, role, db)
        r = api_client.post("/auth/login", json={"username": user, "password": PW})
        assert r.status_code == 200, f"{user}: {r.status_code} {r.text[:80]}"
        tok = r.json()["token"]
        headers[user] = {"Authorization": "Bearer " + tok}
    return headers


@pytest.fixture(autouse=True)
def clean(api_client, tree, monkeypatch):
    monkeypatch.setattr(tools, "DB_PATH", api_client._db)
    monkeypatch.setattr(org, "DB_PATH", api_client._db)
    conn = sqlite3.connect(api_client._db)
    conn.execute("DELETE FROM leave_requests")
    conn.execute("UPDATE employees SET leave_used = 2 WHERE CAST(SUBSTR(employee_id,5) AS INTEGER) >= 100")
    conn.commit(); conn.close()


def get(c, path, h=None):
    r = c.get(path, headers=h or {})
    return r, (r.json() if r.headers.get("content-type", "").startswith("application/json") else None)


def no_leak(text):
    for bad in FORBIDDEN:
        assert bad not in text, bad


def forged(headers, **changes):
    tok = headers["Authorization"].split(" ", 1)[1]
    head, body, sig = tok.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims.update(changes)
    b = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return {"Authorization": f"Bearer {head}.{b}.{sig}"}


def submit(emp_id, offset=10, days=2):
    e = org.employee_by_id(emp_id)
    actor = {"employee_id": emp_id, "name": e["name"], "role": "employee"}
    start = (date.today() + timedelta(days=offset)).isoformat()
    r = tools.submit_leave_request(e["name"], start, days, "سبب", actor=actor)
    assert r.get("submitted"), r
    return r["request_id"]


NEW = ["/me/team", "/org/departments", "/org/structure"]


# ================= المصادقة =================

@pytest.mark.parametrize("path", ["/auth/me"] + NEW + ["/leave/pending", "/leave/decisions"])
def test_unauthenticated_rejected(api_client, path):
    assert api_client.get(path).status_code == 401


@pytest.mark.parametrize("path", ["/auth/me"] + NEW)
def test_tampered_jwt_rejected(api_client, tree, path):
    """رفع الدور داخل الرمز يكسر التوقيع."""
    h = forged(tree["emp_a1"], role="admin", employee_id="EMP-100")
    assert api_client.get(path, headers=h).status_code == 401


# ================= /auth/me =================

def test_me_keeps_original_fields(api_client, tree):
    """الواجهة تقرأ name و role و id — يجب ألا تنكسر."""
    _, d = get(api_client, "/auth/me", tree["emp_a1"])
    assert (d["name"], d["role"], d["id"]) == ("موظف أ1", "employee", "EMP-211")


def test_me_adds_organizational_fields(api_client, tree):
    _, d = get(api_client, "/auth/me", tree["emp_a1"])
    assert d["department"] == "تقنية المعلومات"
    assert d["department_id"].startswith("DEPT-")
    assert d["job_title"] == "مهندس"
    assert d["manager_id"] == "EMP-201" and d["manager_name"] == "مدير أ"
    assert d["employment_status"] == "active"


def test_me_for_account_without_employee_record(api_client, tree):
    r, d = get(api_client, "/auth/me", tree["orphan"])
    assert r.status_code == 200
    assert d["id"] == "EMP-999" and d["department"] is None and d["manager_name"] is None


@pytest.mark.parametrize("qs", ["?employee_id=EMP-221", "?id=EMP-100",
                                "?role=admin", "?manager_id=EMP-201"])
def test_me_ignores_query_identity(api_client, tree, qs):
    _, base = get(api_client, "/auth/me", tree["emp_a1"])
    _, forged_body = get(api_client, "/auth/me" + qs, tree["emp_a1"])
    assert forged_body == base


def test_me_leaks_nothing(api_client, tree, admin):
    for h in (tree["emp_a1"], tree["mgr_a"], admin):
        no_leak(api_client.get("/auth/me", headers=h).text)


# ================= /me/team =================

def ids(members):
    return {m["employee_id"] for m in members}


def test_team_employee_is_empty(api_client, tree):
    r, d = get(api_client, "/me/team", tree["emp_a1"])
    assert r.status_code == 200 and d["count"] == 0 and d["members"] == []


def test_team_is_direct_reports_only(api_client, tree):
    """المباشرون فقط: أ3 تحت أ2 لا يظهر في فريق مدير أ."""
    _, d = get(api_client, "/me/team", tree["mgr_a"])
    assert d["relationship"] == "direct_reports"
    assert ids(d["members"]) == {"EMP-211", "EMP-212"}
    assert "EMP-213" not in ids(d["members"])


def test_team_marks_team_leads(api_client, tree):
    _, d = get(api_client, "/me/team", tree["mgr_a"])
    by = {m["employee_id"]: m for m in d["members"]}
    assert by["EMP-212"]["direct_reports"] == 1
    assert by["EMP-211"]["direct_reports"] == 0


def test_team_same_department_peer_excluded(api_client, tree):
    """مدير ب في نفس القسم — فريقاهما لا يتداخلان."""
    _, a = get(api_client, "/me/team", tree["mgr_a"])
    _, b = get(api_client, "/me/team", tree["mgr_b"])
    assert ids(b["members"]) == {"EMP-221"}
    assert not ids(a["members"]) & ids(b["members"])


def test_team_zero_report_manager_empty(api_client, tree):
    _, d = get(api_client, "/me/team", tree["mgr_d"])
    assert d["count"] == 0


@pytest.mark.parametrize("qs", ["?manager_id=EMP-201", "?employee_id=EMP-201",
                                "?scope=org", "?team=EMP-211,EMP-212"])
def test_team_ignores_forged_params(api_client, tree, qs):
    _, d = get(api_client, "/me/team" + qs, tree["mgr_b"])
    assert ids(d["members"]) == {"EMP-221"}


def test_team_admin_returns_own_direct_reports(api_client, admin):
    """المسؤول بلا تقارير مباشرة — لا تتحوّل النقطة إلى المؤسسة."""
    _, d = get(api_client, "/me/team", admin)
    assert d["count"] == 0


def test_team_leaks_nothing(api_client, tree):
    r = api_client.get("/me/team", headers=tree["mgr_a"])
    no_leak(r.text)
    assert "email" not in r.text and "hire_date" not in r.text


# ================= /org/departments =================

def dept(d, name):
    return next((x for x in d["departments"] if x["name"] == name), None)


def actual(name):
    return org.department_headcount(
        next(d for d in org.departments() if d["name"] == name)["department_id"])


def test_departments_employee_sees_own_without_count(api_client, tree):
    _, d = get(api_client, "/org/departments", tree["emp_a1"])
    assert d["scope"] == "self"
    assert [x["name"] for x in d["departments"]] == ["تقنية المعلومات"]
    assert d["departments"][0]["headcount"] is None


@pytest.mark.parametrize("qs", ["?department_id=DEPT-001", "?name=المالية", "?scope=org"])
def test_departments_employee_cannot_widen(api_client, tree, qs):
    _, d = get(api_client, "/org/departments" + qs, tree["emp_a1"])
    assert [x["name"] for x in d["departments"]] == ["تقنية المعلومات"]
    assert d["departments"][0]["headcount"] is None


def test_departments_manager_counts_only_subtree(api_client, tree):
    """أ1 وأ2 وأ3 = 3 — لا 24 المخزّن ولا عدد القسم الفعلي."""
    _, d = get(api_client, "/org/departments", tree["mgr_a"])
    assert d["scope"] == "team"
    it = dept(d, "تقنية المعلومات")
    assert it["headcount"] == 3
    assert it["headcount"] < actual("تقنية المعلومات")


def test_departments_manager_excludes_departments_without_team(api_client, tree):
    _, d = get(api_client, "/org/departments", tree["mgr_a"])
    assert dept(d, "المالية") is None


def test_departments_peer_manager_counts_differ(api_client, tree):
    _, b = get(api_client, "/org/departments", tree["mgr_b"])
    assert dept(b, "تقنية المعلومات")["headcount"] == 1


def test_departments_other_department_manager(api_client, tree):
    _, c = get(api_client, "/org/departments", tree["mgr_c"])
    assert [x["name"] for x in c["departments"]] == ["المالية"]
    assert c["departments"][0]["headcount"] == 1


def test_departments_zero_report_manager_empty(api_client, tree):
    _, d = get(api_client, "/org/departments", tree["mgr_d"])
    assert d["departments"] == []


def test_departments_admin_sees_all_with_actual_counts(api_client, admin):
    _, d = get(api_client, "/org/departments", admin)
    assert d["scope"] == "org"
    assert len(d["departments"]) == len(org.departments())
    for x in d["departments"]:
        assert x["headcount"] == actual(x["name"]), x["name"]


def test_departments_shows_department_manager(api_client, admin):
    _, d = get(api_client, "/org/departments", admin)
    assert dept(d, "تقنية المعلومات")["manager_name"] == "مدير أ"


def test_departments_manager_param_forging(api_client, tree):
    _, d = get(api_client, "/org/departments?manager_id=EMP-202&scope=org", tree["mgr_a"])
    assert dept(d, "تقنية المعلومات")["headcount"] == 3
    assert dept(d, "المالية") is None


def test_departments_consistent_with_tool(api_client, tree):
    """§12: النقطة والأداة بنفس الأرقام لنفس الفاعل."""
    _, d = get(api_client, "/org/departments", tree["mgr_a"])
    actor = {"employee_id": "EMP-201", "name": "مدير أ", "role": "manager"}
    tool = {x["name"]: x["headcount"] for x in tools.department_summary(actor)["departments"]}
    assert {x["name"]: x["headcount"] for x in d["departments"]} == tool


def test_departments_admin_consistent_with_tool(api_client, admin):
    _, d = get(api_client, "/org/departments", admin)
    tool = {x["name"]: x["headcount"] for x in
            tools.department_summary({"employee_id": "EMP-100", "role": "admin"})["departments"]}
    assert {x["name"]: x["headcount"] for x in d["departments"]} == tool


def test_departments_leaks_nothing(api_client, tree, admin):
    for h in (tree["emp_a1"], tree["mgr_a"], admin):
        r = api_client.get("/org/departments", headers=h)
        no_leak(r.text)
        assert "transport_allowance" not in r.text


# ================= /org/structure =================

@pytest.mark.parametrize("who,code", [("emp_a1", 403), ("mgr_a", 403), ("mgr_d", 403)])
def test_structure_admin_only(api_client, tree, who, code):
    assert api_client.get("/org/structure", headers=tree[who]).status_code == code


def test_structure_admin_allowed(api_client, admin):
    assert api_client.get("/org/structure", headers=admin).status_code == 200


def test_structure_manager_cannot_force_via_params(api_client, tree):
    assert api_client.get("/org/structure?role=admin&scope=org",
                          headers=tree["mgr_a"]).status_code == 403


def walk(nodes):
    for n in nodes:
        yield n
        yield from walk(n["reports"])


def test_structure_every_employee_once(api_client, admin):
    _, d = get(api_client, "/org/structure", admin)
    seen = [n["employee_id"] for dep in d["departments"] for n in walk(dep["roots"])]
    seen += [n["employee_id"] for n in walk(d["unassigned"])]
    assert len(seen) == len(set(seen)) == d["totals"]["employees"]


def test_structure_nests_indirect_reports(api_client, admin):
    _, d = get(api_client, "/org/structure", admin)
    it = next(x for x in d["departments"] if x["name"] == "تقنية المعلومات")
    a = next(n for n in it["roots"] if n["employee_id"] == "EMP-201")
    a2 = next(n for n in a["reports"] if n["employee_id"] == "EMP-212")
    assert [n["employee_id"] for n in a2["reports"]] == ["EMP-213"]


def test_structure_totals_match_database(api_client, admin):
    _, d = get(api_client, "/org/structure", admin)
    conn = sqlite3.connect(api_client._db)
    emps = conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    links = conn.execute("SELECT COUNT(*) FROM employees WHERE manager_id IS NOT NULL").fetchone()[0]
    conn.close()
    assert d["totals"]["employees"] == emps
    assert d["totals"]["reporting_links"] == links
    assert d["unplaced"] == []


def test_structure_leaks_nothing(api_client, admin):
    r = api_client.get("/org/structure", headers=admin)
    no_leak(r.text)
    assert "email" not in r.text and "leave_used" not in r.text


def test_structure_surfaces_corrupt_cycle(tmp_path):
    """دورة في بيانات تالفة لا تُسقط موظفين بصمت."""
    import build_db
    db = tmp_path / "cyc.db"
    build_db.build(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE employees SET manager_id='EMP-002' WHERE employee_id='EMP-003'")
    conn.execute("UPDATE employees SET manager_id='EMP-003' WHERE employee_id='EMP-002'")
    conn.commit(); conn.close()
    t = org.organization_tree(path=db)
    assert {p["employee_id"] for p in t["unplaced"]} == {"EMP-002", "EMP-003"}


# ================= /leave/pending =================

def pend(api_client, h):
    r = api_client.get("/leave/pending", headers=h)
    return r, {x["id"] for x in r.json()} if r.status_code == 200 else None


def test_pending_employee_forbidden(api_client, tree):
    assert api_client.get("/leave/pending", headers=tree["emp_a1"]).status_code == 403


def test_pending_manager_sees_subtree_including_indirect(api_client, tree):
    r1, r3 = submit("EMP-211"), submit("EMP-213", 20)
    rb = submit("EMP-221", 30)
    _, got = pend(api_client, tree["mgr_a"])
    assert {r1, r3} <= got and rb not in got


def test_pending_excludes_own_request(api_client, tree):
    own = submit("EMP-201")
    _, got = pend(api_client, tree["mgr_a"])
    assert own not in got


def test_pending_own_request_reaches_admin(api_client, tree, admin):
    own = submit("EMP-201")
    _, got = pend(api_client, admin)
    assert own in got


def test_pending_admin_excludes_own(api_client, admin):
    own = submit("EMP-100")
    _, got = pend(api_client, admin)
    assert own not in got


def test_pending_consistent_with_tool(api_client, tree):
    submit("EMP-211"); submit("EMP-221", 20); submit("EMP-201", 30)
    _, got = pend(api_client, tree["mgr_a"])
    actor = {"employee_id": "EMP-201", "name": "مدير أ", "role": "manager"}
    assert got == {x["id"] for x in tools.list_pending_requests(actor)["requests"]}


# ================= المراجعة لم تتغيّر =================

def state(api_client, rid):
    conn = sqlite3.connect(api_client._db)
    try:
        return conn.execute("SELECT status, decided_by, decided_at FROM leave_requests"
                            " WHERE id=?", (rid,)).fetchone()
    finally:
        conn.close()


def test_self_approval_still_impossible_over_http(api_client, tree):
    own = submit("EMP-201")
    before = state(api_client, own)
    r = api_client.post(f"/leave/{own}/review", headers=tree["mgr_a"], json={"decision": "approve"})
    assert r.status_code == 400
    assert state(api_client, own) == before == ("pending", None, None)


def test_valid_approval_still_works(api_client, tree):
    rid = submit("EMP-211")
    r = api_client.post(f"/leave/{rid}/review", headers=tree["mgr_a"], json={"decision": "approve"})
    assert r.status_code == 200 and state(api_client, rid)[0] == "approved"


def test_valid_rejection_still_works(api_client, tree):
    rid = submit("EMP-213")
    r = api_client.post(f"/leave/{rid}/review", headers=tree["mgr_a"],
                        json={"decision": "reject", "note": "مزدحمة"})
    assert r.status_code == 200 and state(api_client, rid)[0] == "rejected"


def test_peer_cannot_review_same_department(api_client, tree):
    rid = submit("EMP-211")
    r = api_client.post(f"/leave/{rid}/review", headers=tree["mgr_b"], json={"decision": "approve"})
    assert r.status_code == 400 and state(api_client, rid)[0] == "pending"


# ================= /leave/decisions =================

def test_decisions_scoped(api_client, tree, admin):
    for emp, mgr in [("EMP-211", "mgr_a"), ("EMP-221", "mgr_b"), ("EMP-231", "mgr_c")]:
        rid = submit(emp)
        api_client.post(f"/leave/{rid}/review", headers=tree[mgr], json={"decision": "approve"})

    names = lambda h: {x["employee_name"] for x in api_client.get("/leave/decisions", headers=h).json()}
    assert names(tree["mgr_a"]) == {"موظف أ1"}
    assert names(tree["mgr_b"]) == {"موظف ب1"}
    assert names(admin) >= {"موظف أ1", "موظف ب1", "موظف ج1"}
    assert api_client.get("/leave/decisions", headers=tree["emp_a1"]).status_code == 403


def test_decisions_shape_preserved(api_client, tree):
    rid = submit("EMP-211")
    api_client.post(f"/leave/{rid}/review", headers=tree["mgr_a"],
                    json={"decision": "approve", "note": "موافق"})
    row = api_client.get("/leave/decisions", headers=tree["mgr_a"]).json()[0]
    assert {"id", "employee_name", "status", "decision_note", "decided_at",
            "decided_by_name", "reason"} <= set(row)
    assert "employee_id" not in row
