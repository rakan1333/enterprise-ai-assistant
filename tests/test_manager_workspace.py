"""مساحة المدير: القرارات الأخيرة والصلاحيات.

`/leave/decisions` تسدّ فجوة حقيقية: لا نقطة قائمة تعيد الطلبات
المقرَّرة لغير صاحبها — `/leave/pending` تقتصر على المعلّقة،
و`/me/requests` على صاحب الرمز.
"""

from datetime import date, timedelta

import pytest


def future(days):
    return (date.today() + timedelta(days=days)).isoformat()


def _submit(api_client, headers, days=2, offset=10, reason="ظروف عائلية"):
    import tools
    me = api_client.get("/auth/me", headers=headers).json()
    actor = {"employee_id": me["id"], "name": me["name"], "role": me["role"]}
    return tools.submit_leave_request(me["name"], future(offset), days, reason, actor=actor)


@pytest.fixture(autouse=True)
def clean(api_client):
    import sqlite3
    conn = sqlite3.connect(api_client._db)
    conn.execute("DELETE FROM leave_requests")
    conn.execute("UPDATE employees SET leave_used = 22 WHERE employee_id = 'EMP-003'")
    conn.execute("UPDATE employees SET leave_used = 12 WHERE employee_id = 'EMP-001'")
    conn.commit()
    conn.close()


# ---------- الصلاحية ----------

def test_unauthenticated_cannot_read_decisions(api_client):
    assert api_client.get("/leave/decisions").status_code == 401


def test_employee_cannot_read_decisions(api_client, employee):
    """قرارات الفريق بيانات إدارية — لا يراها الموظف."""
    assert api_client.get("/leave/decisions", headers=employee).status_code == 403


def test_tampered_token_cannot_read_decisions(api_client, employee):
    bad = {"Authorization": employee["Authorization"][:-6] + "AAAAAA"}
    assert api_client.get("/leave/decisions", headers=bad).status_code == 401


def test_manager_can_read_decisions(api_client, manager):
    r = api_client.get("/leave/decisions", headers=manager)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ---------- المحتوى ----------

def test_decisions_exclude_pending(api_client, employee, manager):
    """المعلّق ليس قراراً — مكانه /leave/pending."""
    _submit(api_client, employee)
    assert api_client.get("/leave/decisions", headers=manager).json() == []


def test_decision_appears_after_review(api_client, employee, manager):
    _submit(api_client, employee, days=3, reason="ظروف عائلية")
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    api_client.post(f"/leave/{rid}/review",
                    json={"decision": "approve", "note": "موافق"}, headers=manager)

    decisions = api_client.get("/leave/decisions", headers=manager).json()
    assert len(decisions) == 1
    d = decisions[0]
    assert d["status"] == "approved"
    assert d["employee_name"] == "خالد الدوسري"
    assert d["days"] == 3


def test_decision_keeps_reason_and_note_separate(api_client, employee, manager):
    """الحقلان لا يُدمجان — سبب الموظف ليس ملاحظة المدير."""
    _submit(api_client, employee, reason="سبب الموظف")
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    api_client.post(f"/leave/{rid}/review",
                    json={"decision": "reject", "note": "ملاحظة المدير"}, headers=manager)

    d = api_client.get("/leave/decisions", headers=manager).json()[0]
    assert d["reason"] == "سبب الموظف"
    assert d["decision_note"] == "ملاحظة المدير"


def test_decision_records_who_decided(api_client, employee, manager):
    _submit(api_client, employee)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    api_client.post(f"/leave/{rid}/review", json={"decision": "approve"}, headers=manager)

    d = api_client.get("/leave/decisions", headers=manager).json()[0]
    assert d["decided_by_name"] == "نورة العتيبي"
    assert d["decided_at"]


def test_cancelled_requests_are_not_decisions(api_client, employee, manager):
    """الإلغاء فعل الموظف لا قرار المدير."""
    _submit(api_client, employee)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    api_client.post(f"/leave/{rid}/cancel", headers=employee)

    assert api_client.get("/leave/decisions", headers=manager).json() == []


def test_decisions_are_newest_first(api_client, employee, other_employee, manager):
    for who, off in [(employee, 10), (other_employee, 20)]:
        _submit(api_client, who, offset=off)
    pending = api_client.get("/leave/pending", headers=manager).json()
    for p in pending:
        api_client.post(f"/leave/{p['id']}/review",
                        json={"decision": "approve"}, headers=manager)

    decisions = api_client.get("/leave/decisions", headers=manager).json()
    assert len(decisions) == 2
    ids = [d["id"] for d in decisions]
    assert ids == sorted(ids, reverse=True)


# ---------- حدّ العدد ----------

@pytest.mark.parametrize("limit,expected_max", [(1, 1), (3, 3), (500, 50), (0, 50), (-5, 50)])
def test_limit_is_clamped(api_client, employee, other_employee, manager, limit, expected_max):
    """قيمة من العميل تُقيَّد — لا استعلام غير محدود."""
    for off in (10, 20, 30):
        _submit(api_client, employee, offset=off, days=1)
    for p in api_client.get("/leave/pending", headers=manager).json():
        api_client.post(f"/leave/{p['id']}/review",
                        json={"decision": "approve"}, headers=manager)

    r = api_client.get(f"/leave/decisions?limit={limit}", headers=manager)
    assert r.status_code == 200
    assert len(r.json()) <= expected_max


def test_non_numeric_limit_is_rejected_safely(api_client, manager):
    r = api_client.get("/leave/decisions?limit=abc", headers=manager)
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        assert isinstance(r.json(), list)


# ---------- لا تسريب ----------

def test_decisions_response_hides_internals(api_client, employee, manager):
    _submit(api_client, employee)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    api_client.post(f"/leave/{rid}/review", json={"decision": "approve"}, headers=manager)

    body = api_client.get("/leave/decisions", headers=manager).text
    for leak in ["employee_id", "decided_by\"", "password", "Traceback", "/mnt/"]:
        assert leak not in body, leak


def test_employee_forbidden_message_reveals_nothing(api_client, employee):
    detail = api_client.get("/leave/decisions", headers=employee).json()["detail"]
    assert "Traceback" not in detail and "SELECT" not in detail


# ---------- الموافقة تبقى محميّة ----------

def test_review_still_requires_manager(api_client, employee):
    _submit(api_client, employee)
    rid = api_client.get("/me/requests", headers=employee).json()[0]["id"]
    assert api_client.post(f"/leave/{rid}/review",
                           json={"decision": "approve"}, headers=employee).status_code == 403


def test_pending_still_requires_manager(api_client, employee):
    assert api_client.get("/leave/pending", headers=employee).status_code == 403
