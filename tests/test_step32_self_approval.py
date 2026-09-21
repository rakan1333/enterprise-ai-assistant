"""الخطوة 3.2 — منع الاعتماد الذاتي (فصل المهام).

الثابت: لا ينجح اعتماد أو رفض إلا إذا اختلف مقدّم الطلب عن متّخذ
القرار. النطاق يحدّد أيّ الطلبات يراجعها الفاعل، لكنه لا يتجاوز
هذا الحظر أبداً — ولا نطاق المؤسسة للمسؤول.

الهيكل:
    مدير أعلى (EMP-201) ── مدير فرعي (EMP-202) ── موظف (EMP-211)
    مدير نظير (EMP-203) ── موظف آخر (EMP-221)
    مسؤول (EMP-100)
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

TOP = {"employee_id": "EMP-201", "name": "مدير أعلى", "role": "manager"}
SUB = {"employee_id": "EMP-202", "name": "مدير فرعي", "role": "manager"}
PEER = {"employee_id": "EMP-203", "name": "مدير نظير", "role": "manager"}
EMP = {"employee_id": "EMP-211", "name": "موظف فرعي", "role": "employee"}
EMP2 = {"employee_id": "EMP-221", "name": "موظف نظير", "role": "employee"}
ADMIN = {"employee_id": "EMP-100", "name": "مسؤول النظام", "role": "admin"}


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "s32.db"
    build_db.build(path)
    conn = sqlite3.connect(path)
    conn.executemany(
        """INSERT INTO employees
           (employee_id, name, department, job_title, annual_leave, leave_used)
           VALUES (?,?,?,?,?,?)""",
        [("EMP-201", "مدير أعلى", "تقنية المعلومات", "مدير", 30, 4),
         ("EMP-202", "مدير فرعي", "تقنية المعلومات", "رئيس فريق", 30, 4),
         ("EMP-203", "مدير نظير", "المالية", "مدير", 30, 4),
         ("EMP-211", "موظف فرعي", "تقنية المعلومات", "مهندس", 30, 4),
         ("EMP-221", "موظف نظير", "المالية", "محاسب", 30, 4),
         ("EMP-100", "مسؤول النظام", "تقنية المعلومات", "مسؤول", 30, 4)],
    )
    conn.commit(); conn.close()
    migrations.m005_organization_structure_backfill(path)
    monkeypatch.setattr(org, "DB_PATH", path)
    monkeypatch.setattr(tools, "DB_PATH", path)
    org.assign_manager("EMP-202", "EMP-201", path=path)
    org.assign_manager("EMP-211", "EMP-202", path=path)
    org.assign_manager("EMP-221", "EMP-203", path=path)
    return path


def submit(actor, offset=10, days=3):
    start = (date.today() + timedelta(days=offset)).isoformat()
    r = tools.submit_leave_request(actor["name"], start, days, "سبب", actor=actor)
    assert r.get("submitted"), r
    return r["request_id"]


def snapshot(path, rid):
    """حالة الطلب ورصيد صاحبه — للمقارنة قبل المحاولة وبعدها."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        req = dict(conn.execute(
            "SELECT status, decision_note, decided_at, decided_by, employee_id"
            " FROM leave_requests WHERE id = ?", (rid,)).fetchone())
        req["leave_used"] = conn.execute(
            "SELECT leave_used FROM employees WHERE employee_id = ?",
            (req["employee_id"],)).fetchone()[0]
        return req
    finally:
        conn.close()


def assert_untouched(path, rid, before):
    after = snapshot(path, rid)
    assert after == before
    assert after["status"] == "pending"
    assert after["decided_at"] is None
    assert after["decided_by"] is None
    assert not after["decision_note"]


# ---------------- الموظف ----------------

def test_01_employee_cannot_approve(db):
    rid = submit(EMP2)
    before = snapshot(db, rid)
    assert not tools.review_leave_request(rid, "approve", "", EMP)["reviewed"]
    assert_untouched(db, rid, before)


def test_02_employee_cannot_reject(db):
    rid = submit(EMP2)
    before = snapshot(db, rid)
    assert not tools.review_leave_request(rid, "reject", "لا", EMP)["reviewed"]
    assert_untouched(db, rid, before)


def test_02b_employee_cannot_approve_own(db):
    rid = submit(EMP)
    before = snapshot(db, rid)
    assert not tools.review_leave_request(rid, "approve", "", EMP)["reviewed"]
    assert_untouched(db, rid, before)


# ---------------- المدير ----------------

def test_03_manager_cannot_approve_own(db):
    """جوهر الثغرة: سجل المدير داخل نطاقه — فكان يعتمد نفسه."""
    rid = submit(SUB)
    before = snapshot(db, rid)
    r = tools.review_leave_request(rid, "approve", "موافق", SUB)
    assert not r["reviewed"]
    assert_untouched(db, rid, before)


def test_04_manager_cannot_reject_own(db):
    rid = submit(SUB)
    before = snapshot(db, rid)
    assert not tools.review_leave_request(rid, "reject", "لا", SUB)["reviewed"]
    assert_untouched(db, rid, before)


def test_05_manager_approves_direct_report(db):
    rid = submit(EMP)
    assert tools.review_leave_request(rid, "approve", "موافق", SUB)["reviewed"]
    after = snapshot(db, rid)
    assert after["status"] == "approved" and after["decided_by"] == "EMP-202"


def test_06_manager_rejects_direct_report(db):
    rid = submit(EMP)
    assert tools.review_leave_request(rid, "reject", "مزدحمة", SUB)["reviewed"]
    assert snapshot(db, rid)["status"] == "rejected"


def test_07_peer_manager_cannot_approve_other_manager(db):
    rid = submit(SUB)
    before = snapshot(db, rid)
    assert not tools.review_leave_request(rid, "approve", "", PEER)["reviewed"]
    assert_untouched(db, rid, before)


def test_08_peer_manager_cannot_reject_other_manager(db):
    rid = submit(SUB)
    before = snapshot(db, rid)
    assert not tools.review_leave_request(rid, "reject", "", PEER)["reviewed"]
    assert_untouched(db, rid, before)


def test_07b_superior_approves_sub_managers_request(db):
    """المسار الصحيح: طلب المدير الفرعي يعتمده مديره الأعلى."""
    rid = submit(SUB)
    assert tools.review_leave_request(rid, "approve", "موافق", TOP)["reviewed"]
    assert snapshot(db, rid)["decided_by"] == "EMP-201"


def test_07c_sub_managers_request_reaches_superiors_queue(db):
    rid = submit(SUB)
    assert rid in {r["id"] for r in tools.list_pending_requests(TOP)["requests"]}


def test_09_10_11_denied_self_approval_changes_nothing(db):
    rid = submit(SUB, days=5)
    before = snapshot(db, rid)
    for decision in ("approve", "reject", "approve"):
        tools.review_leave_request(rid, decision, "محاولة", SUB)
    assert_untouched(db, rid, before)
    assert tools.get_leave_balance("مدير فرعي", SUB)["remaining"] == 26


# ---------------- المسؤول ----------------

def test_12_admin_cannot_approve_own(db):
    """نطاق المؤسسة لا يتجاوز حظر الاعتماد الذاتي."""
    rid = submit(ADMIN)
    before = snapshot(db, rid)
    assert not tools.review_leave_request(rid, "approve", "", ADMIN)["reviewed"]
    assert_untouched(db, rid, before)


def test_13_admin_cannot_reject_own(db):
    rid = submit(ADMIN)
    before = snapshot(db, rid)
    assert not tools.review_leave_request(rid, "reject", "", ADMIN)["reviewed"]
    assert_untouched(db, rid, before)


def test_14_admin_approves_others(db):
    rid = submit(EMP2)
    assert tools.review_leave_request(rid, "approve", "", ADMIN)["reviewed"]
    assert snapshot(db, rid)["decided_by"] == "EMP-100"


def test_15_admin_rejects_others(db):
    rid = submit(EMP2)
    assert tools.review_leave_request(rid, "reject", "لا", ADMIN)["reviewed"]


def test_16_admin_own_request_absent_and_unapprovable(db):
    """⚠️ الخطوة 5: كان هذا الاختبار own_request_visible — طلبه يظهر في
    طابوره. الطابور صار لما يمكن مراجعته فقط، وطلبه ليس منه.
    الحظر الأصلي باقٍ: حتى لو استُدعيت المراجعة مباشرة تُرفض."""
    rid = submit(ADMIN)
    assert rid not in {r["id"] for r in tools.list_pending_requests(ADMIN)["requests"]}
    assert not tools.review_leave_request(rid, "approve", "", ADMIN)["reviewed"]


def test_16b_manager_own_request_absent_from_own_queue(db):
    rid = submit(SUB)
    assert rid not in {r["id"] for r in tools.list_pending_requests(SUB)["requests"]}


def test_16c_manager_request_still_reaches_superior(db):
    """الإزالة من طابوره لا تُضيّعه: يصل مديره الأعلى والمسؤول."""
    rid = submit(SUB)
    assert rid in {r["id"] for r in tools.list_pending_requests(TOP)["requests"]}
    assert rid in {r["id"] for r in tools.list_pending_requests(ADMIN)["requests"]}


# ---------------- التجاوز ----------------

def test_17_model_cannot_inject_employee_id(db):
    """الأداة لا تقبل معرّفاً من النموذج — فلا يُحقن رقم آخر."""
    rid = submit(SUB)
    before = snapshot(db, rid)
    with pytest.raises(TypeError):
        tools.review_leave_request(rid, "approve", "", SUB, employee_id="EMP-201")
    assert_untouched(db, rid, before)


def test_17b_actor_without_identity_fails_closed(db):
    """دور مرتفع بلا معرّف: لا يمكن إثبات أن المراجع غير صاحب الطلب."""
    rid = submit(EMP2)
    before = snapshot(db, rid)
    for role in ("admin", "manager"):
        r = tools.review_leave_request(rid, "approve", "", {"name": "?", "role": role})
        assert not r["reviewed"], role
    assert_untouched(db, rid, before)


def test_18_forged_manager_id_does_not_bypass(db):
    rid = submit(SUB)
    before = snapshot(db, rid)
    forged = dict(SUB, manager_id="EMP-201", reports_to="EMP-201")
    assert not tools.review_leave_request(rid, "approve", "", forged)["reviewed"]
    assert_untouched(db, rid, before)


def test_19_forged_scope_does_not_bypass(db):
    rid = submit(SUB)
    before = snapshot(db, rid)
    forged = dict(SUB, scope="org", is_admin=True)
    assert not tools.review_leave_request(rid, "approve", "", forged)["reviewed"]
    assert_untouched(db, rid, before)


def test_21_direct_tool_invocation_is_guarded(db):
    """المسار الأدنى نفسه يفرض الحظر — لا المساعد ولا الواجهة."""
    rid = submit(TOP)
    before = snapshot(db, rid)
    assert not tools.review_leave_request(rid, "approve", "", TOP)["reviewed"]
    assert_untouched(db, rid, before)


def test_21b_agent_dispatch_is_guarded(db):
    """عبر موزّع أدوات المساعد: نفس الحظر."""
    import agent
    rid = submit(SUB)
    before = snapshot(db, rid)
    out = agent._execute_tool(None, "review_leave_request",
                              {"request_id": rid, "decision": "approve"}, actor=SUB)
    assert not out.get("reviewed")
    assert_untouched(db, rid, before)


# ---------------- الانحدار ----------------

def test_23_valid_approval_workflow(db):
    rid = submit(EMP, days=3)
    assert tools.review_leave_request(rid, "approve", "موافق", SUB)["reviewed"]
    assert tools.get_leave_balance("موظف فرعي", EMP)["remaining"] == 23


def test_24_valid_rejection_workflow(db):
    rid = submit(EMP, days=3)
    assert tools.review_leave_request(rid, "reject", "لا", SUB)["reviewed"]
    assert tools.get_leave_balance("موظف فرعي", EMP)["remaining"] == 26


def test_own_request_can_still_be_cancelled(db):
    """الإلغاء فعل صاحب الطلب — ليس قراراً، فلا يشمله الحظر."""
    rid = submit(SUB)
    assert tools.cancel_leave_request(rid, actor=SUB)["cancelled"]


def test_internal_call_without_actor_unchanged(db):
    """الاستدعاءات الداخلية (سكربتات) بلا هوية تبقى كما كانت."""
    rid = submit(EMP)
    assert tools.review_leave_request(rid, "approve", "")["reviewed"]


# ---------------- REST ----------------

def _row(api_client, rid):
    return snapshot(api_client._db, rid)


@pytest.fixture
def http_admin_employee(api_client):
    """سجل موظف للمسؤول حتى يستطيع تقديم إجازة."""
    conn = sqlite3.connect(api_client._db)
    conn.execute("""INSERT OR IGNORE INTO employees
        (employee_id, name, department, job_title, annual_leave, leave_used)
        VALUES ('EMP-100','مسؤول النظام','تقنية المعلومات','مسؤول',30,4)""")
    conn.execute("DELETE FROM leave_requests")
    conn.commit(); conn.close()


def _submit_http(api_client, headers, offset):
    me = api_client.get("/auth/me", headers=headers).json()
    actor = {"employee_id": me["id"], "name": me["name"], "role": me["role"]}
    start = (date.today() + timedelta(days=offset)).isoformat()
    tools.DB_PATH = api_client._db
    return tools.submit_leave_request(me["name"], start, 2, "سبب", actor=actor)["request_id"]


def test_22_rest_manager_self_approval_denied(api_client, manager, http_admin_employee):
    rid = _submit_http(api_client, manager, 40)
    before = _row(api_client, rid)
    for decision in ("approve", "reject"):
        r = api_client.post(f"/leave/{rid}/review", headers=manager,
                            json={"decision": decision, "note": "ذاتي"})
        assert r.status_code == 400
    assert _row(api_client, rid) == before


def test_22b_rest_admin_self_approval_denied(api_client, admin, http_admin_employee):
    rid = _submit_http(api_client, admin, 50)
    before = _row(api_client, rid)
    r = api_client.post(f"/leave/{rid}/review", headers=admin,
                        json={"decision": "approve"})
    assert r.status_code == 400
    assert _row(api_client, rid) == before


def test_22c_rest_admin_approves_managers_request(api_client, manager, admin,
                                                  http_admin_employee):
    rid = _submit_http(api_client, manager, 60)
    r = api_client.post(f"/leave/{rid}/review", headers=admin, json={"decision": "approve"})
    assert r.status_code == 200
    assert _row(api_client, rid)["status"] == "approved"


def test_22d_rest_body_fields_cannot_change_identity(api_client, manager, http_admin_employee):
    rid = _submit_http(api_client, manager, 70)
    before = _row(api_client, rid)
    r = api_client.post(f"/leave/{rid}/review", headers=manager,
                        json={"decision": "approve", "employee_id": "EMP-100",
                              "role": "admin", "actor": {"employee_id": "EMP-100"}})
    assert r.status_code in (400, 422)
    assert _row(api_client, rid) == before


def _b64(o):
    return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()


def test_20_tampered_jwt_cannot_bypass(api_client, manager, http_admin_employee):
    """تغيير employee_id داخل الرمز يكسر التوقيع."""
    rid = _submit_http(api_client, manager, 80)
    before = _row(api_client, rid)
    head, body, sig = manager["Authorization"].split(" ", 1)[1].split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["employee_id"] = "EMP-100"
    claims["role"] = "admin"
    forged = f"{head}.{_b64(claims)}.{sig}"
    r = api_client.post(f"/leave/{rid}/review",
                        headers={"Authorization": "Bearer " + forged},
                        json={"decision": "approve"})
    assert r.status_code == 401
    assert _row(api_client, rid) == before
