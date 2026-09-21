"""مصفوفة صلاحيات أدوات الوكيل.

خلفية: عشر أدوات من اثنتي عشرة كانت تستقبل `actor` وتفحص الصلاحية.
أداتان لا تستقبلانه، و`agent.py` كان يُسقط الهوية عند استدعائهما —
فأي موظف مصادَق يسأل المساعد "من يعمل في قسم المالية؟" فيحصل
على القائمة كاملة.

نفس فئة ثغرة رفع المستندات في المرحلة الأولى، ونفس السبب:
لا اختبار يغطي طبقة الأدوات كمصفوفة.
"""

from datetime import date, timedelta

import pytest

import build_db
import tools


@pytest.fixture(autouse=True)
def db(tmp_path):
    path = tmp_path / "authz.db"
    build_db.build(path)

    import org
    org.DB_PATH = path
    # نورة (الموارد البشرية) تدير أحمد وفهد في تقنية المعلومات —
    # فنطاقها يشمل قسمين، وقسم المالية يبقى خارجه.
    org.assign_manager("EMP-001", "EMP-002", path=path)
    org.assign_manager("EMP-005", "EMP-002", path=path)

    original = tools.DB_PATH
    tools.DB_PATH = path
    yield path
    tools.DB_PATH = original


EMPLOYEE = {"employee_id": "EMP-003", "name": "خالد الدوسري", "role": "employee"}
OTHER    = {"employee_id": "EMP-001", "name": "أحمد الشمري",  "role": "employee"}
MANAGER  = {"employee_id": "EMP-002", "name": "نورة العتيبي", "role": "manager"}
ADMIN    = {"employee_id": "EMP-000", "name": "مسؤول النظام", "role": "admin"}
OUT_OF_SCOPE = "المالية"      # لا أحد من فريق نورة فيه
INTERNAL = None          # سكربت أو مهمة داخلية


# ============ list_department_employees ============

def test_employee_denied_department_roster():
    """جوهر الثغرة: قائمة زملاء لا تخصّ الموظف."""
    r = tools.list_department_employees("تقنية المعلومات", EMPLOYEE)
    assert r["employees"] == []
    assert r["count"] == 0
    assert "error" in r


def test_employee_denied_even_for_own_department():
    """الموظف في "المالية" ولا يُمنح قسمه أيضاً.

    نطاق الموظف في النموذج الحالي هو سجله وحده. توسيعه ليشمل
    قسمه قرار سياسة يخصّ بناء الهيكل التنظيمي لا إغلاق ثغرة.
    """
    r = tools.list_department_employees("المالية", EMPLOYEE)
    assert r["employees"] == []


def test_manager_allowed_own_department_roster():
    """المدير يرى القسم الذي يضمّ فريقه."""
    r = tools.list_department_employees("تقنية المعلومات", MANAGER)
    assert "error" not in r
    assert {e["name"] for e in r["employees"]} == {"أحمد الشمري", "فهد المطيري"}


def test_manager_denied_department_outside_scope():
    """⚠️ تغيير سلوك (الخطوة 3): لم يعد المدير يرى كل الأقسام."""
    r = tools.list_department_employees(OUT_OF_SCOPE, MANAGER)
    assert r["employees"] == []
    assert "error" in r


def test_admin_allowed_department_roster():
    r = tools.list_department_employees("تقنية المعلومات", ADMIN)
    assert r["count"] == 2 and "error" not in r


def test_internal_call_unrestricted():
    """الاستدعاءات الداخلية بلا هوية تبقى تعمل — للسكربتات والزرع."""
    assert tools.list_department_employees("المالية")["count"] == 1


# ============ department_summary ============

def test_employee_denied_summary():
    r = tools.department_summary(EMPLOYEE)
    assert r["departments"] == []
    assert r["total_monthly_transport"] == 0
    assert "error" in r


def test_manager_summary_is_scoped():
    """⚠️ المدير يرى أقسامه؛ إجمالي المؤسسة للمسؤول وحده."""
    r = tools.department_summary(MANAGER)
    assert "error" not in r
    assert 0 < len(r["departments"]) < 5
    assert r["total_monthly_transport"] < 72000


def test_admin_allowed_summary():
    """المسؤول يرى الإجمالي الكامل — محسوباً من الموظفين الفعليين (الخطوة 5)."""
    expected, _ = _independent_org_total(tools.DB_PATH)
    assert tools.department_summary(ADMIN)["total_monthly_transport"] == expected


def test_internal_summary_unrestricted():
    _, expected = _independent_org_total(tools.DB_PATH)
    assert tools.department_summary()["total_headcount"] == expected


# ============ عبر الأقسام ============

@pytest.mark.parametrize("dept", [
    "المالية", "تقنية المعلومات", "الموارد البشرية", "التسويق", "العمليات",
])
def test_employee_denied_every_department(dept):
    """لا قسم واحد مستثنى."""
    assert tools.list_department_employees(dept, EMPLOYEE)["employees"] == []


@pytest.mark.parametrize("dept", [
    "المالية", "الموارد البشرية", "التسويق", "العمليات",
])
def test_manager_denied_departments_outside_scope(dept):
    """كل قسم لا يضمّ أحداً من فريقه محجوب عنه."""
    assert "error" in tools.list_department_employees(dept, MANAGER)


@pytest.mark.parametrize("dept", [
    "المالية", "تقنية المعلومات", "الموارد البشرية", "التسويق", "العمليات",
])
def test_admin_allowed_every_department(dept):
    assert "error" not in tools.list_department_employees(dept, ADMIN)


def test_second_employee_also_denied():
    """ليست خاصية حساب واحد."""
    assert tools.list_department_employees("المالية", OTHER)["employees"] == []


# ============ مدخلات مشوّهة ============

@pytest.mark.parametrize("bad", ["", "   ", "%", "'; DROP TABLE employees; --",
                                 "قسم غير موجود", "%%%", "\\"])
def test_malformed_department_still_denied_for_employee(bad):
    """المنع يسبق الاستعلام — لا تسريب عبر محرف بدل."""
    r = tools.list_department_employees(bad, EMPLOYEE)
    assert r["employees"] == [] and "error" in r


def test_wildcard_does_not_leak_for_employee():
    """`%` في LIKE يطابق كل شيء — لولا الفحص لعاد كل الموظفين."""
    assert tools.list_department_employees("%", EMPLOYEE)["count"] == 0
    assert tools.list_department_employees("%", ADMIN)["count"] == 6


def test_wildcard_is_scoped_for_manager():
    """محرف البدل لا يتجاوز النطاق: المدير يرى فريقه فقط."""
    r = tools.list_department_employees("%", MANAGER)
    assert r["count"] < 6
    assert all(e["name"] in ("أحمد الشمري", "فهد المطيري") for e in r["employees"])


def test_sql_injection_attempt_leaves_table_intact():
    tools.list_department_employees("'; DROP TABLE employees; --", ADMIN)
    assert tools.list_department_employees("%", ADMIN)["count"] == 6


def test_unknown_department_returns_empty_for_admin():
    r = tools.list_department_employees("قسم وهمي", ADMIN)
    assert r["count"] == 0 and "error" not in r


# ============ تصعيد الدور ============

@pytest.mark.parametrize("role", [
    "Manager", "MANAGER", "manager ", " manager", "admin_", "superadmin",
    "employee", "", None, "root", "owner",
])
def test_only_exact_elevated_roles_pass(role):
    """المقارنة تامّة: لا حساسية حالة ولا مسافات ولا أدوار مخترَعة."""
    actor = {"employee_id": "EMP-003", "name": "خالد", "role": role}
    allowed = role in ("manager", "admin")
    r = tools.list_department_employees("المالية", actor)
    assert ("error" not in r) is allowed, role


def test_extra_payload_fields_do_not_elevate():
    """حقول إضافية في الهوية لا ترفع الصلاحية."""
    forged = dict(EMPLOYEE, is_admin=True, scope="org", permissions=["*"],
                  manager=True, role_override="admin")
    assert tools.list_department_employees("المالية", forged)["employees"] == []
    assert tools.department_summary(forged)["departments"] == []


def test_missing_role_key_is_denied():
    """هوية بلا دور تفشل مغلقة."""
    assert tools.list_department_employees("المالية",
                                           {"employee_id": "EMP-003"})["employees"] == []


def test_non_dict_actor_is_denied_safely():
    """هوية بشكل غير متوقّع لا تُسقط الأداة ولا تمنح وصولاً."""
    for bad in [{}, {"role": []}, {"role": {}}, {"role": 0}]:
        r = tools.list_department_employees("المالية", bad)
        assert r["employees"] == [] or bad == {}


# ============ اتساق المساعد ============

def test_agent_forwards_identity_to_both_tools():
    """الثغرة كانت في المرسِل لا في الأداة — نحرس الطرفين."""
    import inspect

    import agent
    src = inspect.getsource(agent._execute_tool)

    assert "tools.list_department_employees(**args, actor=actor)" in src
    assert "tools.department_summary(actor=actor)" in src


def test_every_data_tool_receives_actor():
    """أي أداة بيانات جديدة بلا هوية يكشفها هذا الاختبار."""
    import inspect
    import re

    import agent
    src = inspect.getsource(agent._execute_tool)

    calls = re.findall(r"return tools\.(\w+)\(([^)]*)\)", src)
    exempt = {"calculate"}          # حساب رياضي خالص — بلا بيانات

    missing = [name for name, args in calls
               if name not in exempt and "actor" not in args]
    assert not missing, f"أدوات لا تستقبل الهوية: {missing}"


def test_elevated_roles_constant_is_explicit():
    assert tools.ELEVATED_ROLES == ("manager", "admin")
    assert "employee" not in tools.ELEVATED_ROLES


# ============ الأدوات الأخرى لم تتأثر ============

def test_employee_scoping_unchanged():
    """نطاق الموظف لم يتغيّر بالخطوة 3 — سجله وحده."""
    assert tools.get_employee("خالد الدوسري", EMPLOYEE)["found"]
    assert not tools.get_employee("أحمد الشمري", EMPLOYEE).get("found")
    assert tools.get_leave_balance("خالد الدوسري", EMPLOYEE)["remaining"] == 8


def test_manager_scoping_follows_team():
    assert tools.get_employee("أحمد الشمري", MANAGER)["found"]      # في فريقها
    assert not tools.get_employee("خالد الدوسري", MANAGER).get("found")  # خارجه
    assert tools.get_employee("خالد الدوسري", ADMIN)["found"]


def test_pending_requests_still_manager_only():
    assert "error" in tools.list_pending_requests(EMPLOYEE)
    assert "error" not in tools.list_pending_requests(MANAGER)


def test_calculate_needs_no_identity():
    assert tools.calculate("800 * 24")["result"] == 19200


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
