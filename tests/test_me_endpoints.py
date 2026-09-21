"""مسارات مساحة الموظف: /me/leave · /me/requests · /leave/{id}/cancel.

المبدأ الحاكم: الموظف يُحدَّد من الرمز حصراً. لا يوجد معامل
في أي من هذي النقاط يسمح للعميل باختيار صاحب البيانات.
"""

from datetime import date, timedelta

import pytest


def future(days):
    return (date.today() + timedelta(days=days)).isoformat()


def _submit(api_client, headers, days=2, offset=10, reason="ظروف عائلية"):
    """يقدّم طلباً عبر طبقة الأدوات — لا نغيّر مسار preview→submit."""
    import tools
    me = api_client.get("/auth/me", headers=headers).json()
    actor = {"employee_id": me["id"], "name": me["name"], "role": me["role"]}
    return tools.submit_leave_request(me["name"], future(offset), days, reason, actor=actor)


@pytest.fixture(autouse=True)
def clean_requests(api_client):
    """قاعدة الطلبات نظيفة قبل كل اختبار."""
    import sqlite3
    conn = sqlite3.connect(api_client._db)
    conn.execute("DELETE FROM leave_requests")
    conn.execute("UPDATE employees SET leave_used = 22 WHERE employee_id = 'EMP-003'")
    conn.execute("UPDATE employees SET leave_used = 12 WHERE employee_id = 'EMP-001'")
    conn.commit()
    conn.close()


# ---------- المصادقة ----------

@pytest.mark.parametrize("method,path", [
    ("get",  "/me/leave"),
    ("get",  "/me/requests"),
    ("post", "/leave/1/cancel"),
])
def test_unauthenticated_is_rejected(api_client, method, path):
    assert getattr(api_client, method)(path).status_code == 401


@pytest.mark.parametrize("method,path", [
    ("get",  "/me/leave"),
    ("get",  "/me/requests"),
    ("post", "/leave/1/cancel"),
])
def test_tampered_token_is_rejected(api_client, employee, method, path):
    bad = {"Authorization": employee["Authorization"][:-6] + "AAAAAA"}
    assert getattr(api_client, method)(path, headers=bad).status_code == 401


# ---------- /me/leave ----------

def test_leave_returns_own_record_only(api_client, employee):
    d = api_client.get("/me/leave", headers=employee).json()
    assert d["employee_id"] == "EMP-003"
    assert d["employee_name"] == "خالد الدوسري"


def test_each_identity_gets_its_own_record(api_client, employee, other_employee):
    """لا معامل يختار الموظف — الرمز وحده يحدّده."""
    a = api_client.get("/me/leave", headers=employee).json()
    b = api_client.get("/me/leave", headers=other_employee).json()
    assert a["employee_id"] != b["employee_id"]
    assert {a["employee_id"], b["employee_id"]} == {"EMP-003", "EMP-001"}


def test_query_parameters_cannot_redirect_the_lookup(api_client, employee):
    """محاولة حقن هوية عبر المعاملات لا تغيّر النتيجة."""
    for qs in ["?employee_id=EMP-001", "?name=أحمد الشمري", "?employee_id=EMP-001&name=أحمد"]:
        d = api_client.get("/me/leave" + qs, headers=employee).json()
        assert d["employee_id"] == "EMP-003", qs


def test_balance_formula_is_explicit(api_client, employee):
    d = api_client.get("/me/leave", headers=employee).json()
    assert d["available"] == d["annual_leave"] - d["leave_used"] - d["pending_days"]
    assert "المتاح" in d["calculation"]
    assert str(d["available"]) in d["formula"]


def test_pending_request_reduces_available_not_used(api_client, employee):
    """الطلب المعلّق حجز لا خصم — الرصيد المخزّن لا يتغيّر."""
    before = api_client.get("/me/leave", headers=employee).json()
    _submit(api_client, employee, days=3)
    after = api_client.get("/me/leave", headers=employee).json()

    assert after["leave_used"] == before["leave_used"]
    assert after["pending_days"] == before["pending_days"] + 3
    assert after["available"] == before["available"] - 3


def test_manager_me_leave_returns_manager_own_data(api_client, manager):
    """صلاحية الإدارة لا توسّع نطاق "بياناتي"."""
    d = api_client.get("/me/leave", headers=manager).json()
    assert d["employee_id"] == "EMP-002"


# ---------- /me/requests ----------

def test_requests_are_scoped_to_the_token(api_client, employee, other_employee):
    _submit(api_client, employee, days=2)
    _submit(api_client, other_employee, days=1, offset=15)

    mine = api_client.get("/me/requests", headers=employee).json()
    theirs = api_client.get("/me/requests", headers=other_employee).json()

    assert len(mine) == 1 and mine[0]["days"] == 2
    assert len(theirs) == 1 and theirs[0]["days"] == 1


def test_requests_expose_expected_fields(api_client, employee):
    _submit(api_client, employee, reason="ظروف عائلية")
    r = api_client.get("/me/requests", headers=employee).json()[0]

    for field in ("id", "leave_type", "start_date", "end_date", "days",
                  "status", "reason", "created_at", "can_cancel"):
        assert field in r, field
    assert r["leave_type"] == "annual"
    assert r["reason"] == "ظروف عائلية"
    assert r["status"] == "pending"
    assert r["can_cancel"] is True


def test_requests_are_newest_first(api_client, employee):
    for i in range(3):
        _submit(api_client, employee, days=1, offset=10 + i * 5)
    ids = [r["id"] for r in api_client.get("/me/requests", headers=employee).json()]
    assert ids == sorted(ids, reverse=True)


def test_rejection_reason_appears_only_on_rejection(api_client, employee, manager):
    """ملاحظة المدير كانت تُكتب فوق سبب الموظف — الآن منفصلة."""
    _submit(api_client, employee, reason="سبب الموظف")
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]

    api_client.post(f"/leave/{rid}/review",
                    json={"decision": "reject", "note": "الفترة مزدحمة"},
                    headers=manager)

    r = api_client.get("/me/requests", headers=employee).json()[0]
    assert r["status"] == "rejected"
    assert r["reason"] == "سبب الموظف"
    assert r["rejection_reason"] == "الفترة مزدحمة"
    assert r["can_cancel"] is False


def test_approval_note_is_not_shown_as_rejection_reason(api_client, employee, manager):
    _submit(api_client, employee)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    api_client.post(f"/leave/{rid}/review",
                    json={"decision": "approve", "note": "موافق"}, headers=manager)

    r = api_client.get("/me/requests", headers=employee).json()[0]
    assert r["status"] == "approved"
    assert r["rejection_reason"] == ""


def test_empty_history_returns_empty_list(api_client, employee):
    assert api_client.get("/me/requests", headers=employee).json() == []


# ---------- الإلغاء ----------

def test_employee_cancels_own_pending_request(api_client, employee):
    _submit(api_client, employee, days=3)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]

    r = api_client.post(f"/leave/{rid}/cancel", headers=employee)
    assert r.status_code == 200
    body = r.json()
    assert body["cancelled"] is True and body["days_returned"] == 3


def test_cancellation_releases_the_hold(api_client, employee):
    """المتاح محسوب — الإلغاء يحرّره بلا عملية عكسية."""
    before = api_client.get("/me/leave", headers=employee).json()["available"]
    _submit(api_client, employee, days=4)
    assert api_client.get("/me/leave", headers=employee).json()["available"] == before - 4

    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    api_client.post(f"/leave/{rid}/cancel", headers=employee)
    assert api_client.get("/me/leave", headers=employee).json()["available"] == before


def test_cancelled_status_is_visible(api_client, employee):
    _submit(api_client, employee)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    api_client.post(f"/leave/{rid}/cancel", headers=employee)

    r = api_client.get("/me/requests", headers=employee).json()[0]
    assert r["status"] == "cancelled"
    assert r["can_cancel"] is False


def test_employee_cannot_cancel_another_employees_request(api_client, employee, other_employee):
    _submit(api_client, other_employee, days=2)
    rid = api_client.get("/me/requests", headers=other_employee).json()[0]["id"]

    r = api_client.post(f"/leave/{rid}/cancel", headers=employee)
    assert r.status_code == 403
    assert api_client.get("/me/requests", headers=other_employee).json()[0]["status"] == "pending"


def test_cancelling_unknown_request_returns_404(api_client, employee):
    assert api_client.post("/leave/999999/cancel", headers=employee).status_code == 404


def test_cannot_cancel_twice(api_client, employee):
    _submit(api_client, employee)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    api_client.post(f"/leave/{rid}/cancel", headers=employee)

    again = api_client.post(f"/leave/{rid}/cancel", headers=employee)
    assert again.status_code == 409


def test_cannot_cancel_approved_request(api_client, employee, manager):
    """الحالة النهائية: الأيام خُصمت — الإلغاء قرار إداري لا عملية تلقائية."""
    _submit(api_client, employee, days=2)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    api_client.post(f"/leave/{rid}/review", json={"decision": "approve"}, headers=manager)

    r = api_client.post(f"/leave/{rid}/cancel", headers=employee)
    assert r.status_code == 409
    assert "معتمد" in r.json()["detail"]


def test_cannot_cancel_rejected_request(api_client, employee, manager):
    _submit(api_client, employee)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    api_client.post(f"/leave/{rid}/review",
                    json={"decision": "reject", "note": "غير مناسب"}, headers=manager)

    assert api_client.post(f"/leave/{rid}/cancel", headers=employee).status_code == 409


def test_manager_can_cancel_any_pending_request(api_client, employee, manager):
    """صلاحية الإدارة القائمة تبقى كما هي."""
    _submit(api_client, employee, days=2)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    assert api_client.post(f"/leave/{rid}/cancel", headers=manager).status_code == 200


# ---------- الاتساق ----------

def test_endpoints_carry_request_id(api_client, employee):
    for path in ("/me/leave", "/me/requests"):
        assert api_client.get(path, headers=employee).headers.get("X-Request-ID")


def test_cancellation_is_audited(api_client, employee):
    import metrics
    _submit(api_client, employee)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    api_client.post(f"/leave/{rid}/cancel", headers=employee)

    trail = metrics.audit_trail(actor_id="EMP-003", path=api_client._db)
    assert any(e["kind"] == "cancel" for e in trail)


# ---------- تفاصيل الطلب: فصل الحقول ----------

def test_employee_reason_and_decision_note_stay_separate(api_client, employee, manager):
    """ثلاثة حقول متمايزة — دمجها يفقد سبب الموظف."""
    _submit(api_client, employee, reason="سبب الموظف")
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]

    api_client.post(f"/leave/{rid}/review",
                    json={"decision": "reject", "note": "ملاحظة المدير"}, headers=manager)

    r = api_client.get("/me/requests", headers=employee).json()[0]
    assert r["reason"] == "سبب الموظف"
    assert r["decision_note"] == "ملاحظة المدير"
    assert r["rejection_reason"] == "ملاحظة المدير"


def test_approval_note_is_visible_but_not_a_rejection_reason(api_client, employee, manager):
    """ملاحظة الاعتماد كانت محجوبة عن الموظف تماماً."""
    _submit(api_client, employee, reason="ظروف عائلية")
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]

    api_client.post(f"/leave/{rid}/review",
                    json={"decision": "approve", "note": "موافق"}, headers=manager)

    r = api_client.get("/me/requests", headers=employee).json()[0]
    assert r["decision_note"] == "موافق"
    assert r["rejection_reason"] == ""
    assert r["reason"] == "ظروف عائلية"


def test_decision_maker_name_is_resolved(api_client, employee, manager):
    _submit(api_client, employee)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    api_client.post(f"/leave/{rid}/review", json={"decision": "approve"}, headers=manager)

    r = api_client.get("/me/requests", headers=employee).json()[0]
    assert r["decided_by_name"] == "نورة العتيبي"
    assert r["decided_at"]


def test_pending_request_has_no_decision_metadata(api_client, employee):
    _submit(api_client, employee)
    r = api_client.get("/me/requests", headers=employee).json()[0]
    assert r["decision_note"] == ""
    assert r["decided_by_name"] == ""
    assert r["decided_at"] == ""


# ---------- الملكية: لا وصول عبر تبديل المعرّفات ----------

def test_request_ids_are_not_guessable_across_employees(api_client, employee, other_employee):
    """IDOR: الطلبات تُقيَّد بالرمز، لا برقم في الرابط."""
    _submit(api_client, other_employee, days=2)
    theirs = api_client.get("/me/requests", headers=other_employee).json()
    their_ids = {r["id"] for r in theirs}

    mine = api_client.get("/me/requests", headers=employee).json()
    assert not their_ids & {r["id"] for r in mine}


def test_cancel_rejects_foreign_request_id(api_client, employee, other_employee):
    _submit(api_client, other_employee, days=2)
    foreign_id = api_client.get("/me/requests", headers=other_employee).json()[0]["id"]

    r = api_client.post(f"/leave/{foreign_id}/cancel", headers=employee)
    assert r.status_code == 403
    assert api_client.get("/me/requests",
                          headers=other_employee).json()[0]["status"] == "pending"


def test_employee_cannot_reach_manager_review(api_client, employee):
    _submit(api_client, employee)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    r = api_client.post(f"/leave/{rid}/review",
                        json={"decision": "approve"}, headers=employee)
    assert r.status_code == 403
    assert api_client.get("/me/requests", headers=employee).json()[0]["status"] == "pending"


# ---------- انتقالات الحالة ----------

@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_can_cancel_flag_follows_server_rules(api_client, employee, manager, decision):
    """can_cancel يعكس ما يسمح به الخادم — لا اجتهاد في الواجهة."""
    _submit(api_client, employee)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    assert api_client.get("/me/requests", headers=employee).json()[0]["can_cancel"] is True

    api_client.post(f"/leave/{rid}/review",
                    json={"decision": decision, "note": "ملاحظة"}, headers=manager)

    after = api_client.get("/me/requests", headers=employee).json()[0]
    assert after["can_cancel"] is False
    assert api_client.post(f"/leave/{rid}/cancel", headers=employee).status_code == 409


def test_cancelled_request_cannot_be_cancelled_again(api_client, employee):
    _submit(api_client, employee)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    assert api_client.post(f"/leave/{rid}/cancel", headers=employee).status_code == 200

    after = api_client.get("/me/requests", headers=employee).json()[0]
    assert after["status"] == "cancelled" and after["can_cancel"] is False
    assert api_client.post(f"/leave/{rid}/cancel", headers=employee).status_code == 409
