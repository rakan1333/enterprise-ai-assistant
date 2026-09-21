"""ملكية البيانات: الفحص بالمعرّف لا بالاسم.

الثغرة السابقة: `_can_see` تقارن الأسماء بالاحتواء الجزئي.
موظف اسمه "خالد الدوسري" كان يستطيع قراءة بيانات "خالد المطيري"
لأن السلسلتين تتقاطعان. المعرّف الوظيفي مستقر وفريد — وهو موجود
في الرمز أصلاً وكان متجاهَلاً.
"""

from datetime import date, timedelta

import pytest

import auth
import build_db
import tools


def future(days):
    return (date.today() + timedelta(days=days)).isoformat()


@pytest.fixture(autouse=True)
def db_with_similar_names(tmp_path):
    """موظفان يتشاركان الاسم الأول — حالة التصادم."""
    db = tmp_path / "own.db"
    build_db.build(db)

    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO employees
           (employee_id, name, department, job_title, annual_leave, leave_used)
           VALUES (?,?,?,?,?,?)""",
        ("EMP-010", "خالد المطيري", "العمليات", "منسّق", 30, 4),
    )
    conn.commit()
    conn.close()

    import org
    org.DB_PATH = db
    # نورة تدير الثلاثة — فنطاق "المدير" له معنى قابل للقياس
    for emp in ("EMP-003", "EMP-001", "EMP-010"):
        org.assign_manager(emp, "EMP-002", path=db)

    original = tools.DB_PATH
    tools.DB_PATH = db
    yield db
    tools.DB_PATH = original


# هويات مبنية كما يبنيها الرمز: معرّف + اسم + دور
KHALID_D = {"employee_id": "EMP-003", "name": "خالد الدوسري", "role": "employee"}
KHALID_M = {"employee_id": "EMP-010", "name": "خالد المطيري", "role": "employee"}
SHORT_NAME = {"employee_id": "EMP-003", "name": "خالد", "role": "employee"}
MANAGER = {"employee_id": "EMP-002", "name": "نورة العتيبي", "role": "manager"}


# ---------- قراءة البيانات ----------

def test_employee_reads_own_record():
    assert tools.get_employee("خالد الدوسري", KHALID_D)["found"]


def test_employee_cannot_read_same_first_name_colleague():
    """جوهر الثغرة: اسمان يتقاطعان جزئياً، ومعرّفان مختلفان."""
    r = tools.get_employee("خالد المطيري", KHALID_D)
    assert not r.get("found"), "تسريب: قرأ بيانات زميل يشاركه الاسم الأول"


def test_the_other_direction_is_also_blocked():
    r = tools.get_employee("خالد الدوسري", KHALID_M)
    assert not r.get("found")


def test_partial_name_actor_cannot_widen_access():
    """هوية باسم قصير لا تفتح وصولاً أوسع — المعرّف هو الحاكم."""
    assert tools.get_employee("خالد الدوسري", SHORT_NAME)["found"]
    assert not tools.get_employee("خالد المطيري", SHORT_NAME).get("found")


def test_employee_cannot_read_unrelated_colleague():
    assert not tools.get_employee("أحمد الشمري", KHALID_D).get("found")


def test_manager_reads_their_team():
    """⚠️ تغيير سلوك مُعلَن (الخطوة 3): المدير يرى فريقه لا المؤسسة.

    كان هذا الاختبار اسمه manager_reads_everyone ويؤكّد الوصول
    الشامل. النموذج الثلاثي ضيّقه عمداً — والتأكيد الآن على
    الوصول الصحيح لا الواسع.
    """
    for name in ["خالد الدوسري", "خالد المطيري", "أحمد الشمري"]:
        assert tools.get_employee(name, MANAGER)["found"], name


def test_manager_cannot_read_outside_their_team():
    """سارة ليست في فريق نورة — فلا تراها."""
    assert not tools.get_employee("سارة القحطاني", MANAGER).get("found")


def test_admin_reads_everyone():
    """المسؤول وحده يملك نطاق المؤسسة."""
    admin = {"employee_id": "EMP-000", "name": "مسؤول", "role": "admin"}
    for name in ["خالد الدوسري", "خالد المطيري", "أحمد الشمري", "سارة القحطاني"]:
        assert tools.get_employee(name, admin)["found"], name


def test_internal_call_without_actor_is_unrestricted():
    """السكربتات والاستدعاءات الداخلية تمرّ — الفحص للطلبات المصادَقة."""
    assert tools.get_employee("أحمد الشمري")["found"]


# ---------- الرصيد ----------

def test_balance_is_scoped_to_self():
    assert tools.get_leave_balance("خالد الدوسري", KHALID_D)["found"]
    assert not tools.get_leave_balance("خالد المطيري", KHALID_D).get("found")


# ---------- تقديم الطلبات ----------

def test_cannot_submit_on_behalf_of_similar_name():
    r = tools.preview_leave_request("خالد المطيري", future(10), 2, KHALID_D)
    assert not r["valid"]


def test_can_submit_for_self():
    assert tools.preview_leave_request("خالد الدوسري", future(10), 2, KHALID_D)["valid"]


# ---------- عرض الطلبات ----------

def test_listing_is_scoped_to_own_requests():
    tools.submit_leave_request("خالد الدوسري", future(10), 2, actor=KHALID_D)
    tools.submit_leave_request("خالد المطيري", future(12), 2, actor=KHALID_M)

    mine = tools.list_leave_requests("", KHALID_D)
    names = {r["employee_name"] for r in mine["requests"]}
    assert names == {"خالد الدوسري"}, names


def test_listing_ignores_attempt_to_name_another():
    tools.submit_leave_request("خالد المطيري", future(12), 2, actor=KHALID_M)
    sneaky = tools.list_leave_requests("خالد المطيري", KHALID_D)
    names = {r["employee_name"] for r in sneaky["requests"]}
    assert "خالد المطيري" not in names


def test_manager_sees_all_requests():
    tools.submit_leave_request("خالد الدوسري", future(10), 2, actor=KHALID_D)
    tools.submit_leave_request("خالد المطيري", future(12), 2, actor=KHALID_M)
    assert tools.list_leave_requests("", MANAGER)["count"] == 2


# ---------- الإلغاء ----------

def test_cannot_cancel_similar_name_colleagues_request():
    tools.submit_leave_request("خالد المطيري", future(12), 2, actor=KHALID_M)
    r = tools.cancel_leave_request(1, actor=KHALID_D)
    assert not r["cancelled"]
    assert tools.list_leave_requests("", MANAGER)["requests"][0]["status"] == "pending"


def test_can_cancel_own_request():
    tools.submit_leave_request("خالد الدوسري", future(10), 2, actor=KHALID_D)
    assert tools.cancel_leave_request(1, actor=KHALID_D)["cancelled"]


def test_manager_can_cancel_any_request():
    tools.submit_leave_request("خالد المطيري", future(12), 2, actor=KHALID_M)
    assert tools.cancel_leave_request(1, actor=MANAGER)["cancelled"]
