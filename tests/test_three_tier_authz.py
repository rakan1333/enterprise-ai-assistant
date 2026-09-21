"""نموذج الصلاحية الثلاثي: موظف · مدير · مسؤول.

⚠️ هذي الخطوة ضيّقت المدير عمداً من المؤسسة كلها إلى شجرة من
يديرهم. الاختبارات هنا تقيس الطرفين: وصولاً صحيحاً ومنعاً صحيحاً.

الهيكل المستخدَم — مديران في قسم واحد بفريقين منفصلين:

    قسم أ
    ├── مدير أ  → موظف أ1، موظف أ2 → (موظف أ3 تحت أ2)
    └── مدير ب  → موظف ب1
    قسم ب
    └── مدير ج  → موظف ج1
"""

import sqlite3

import pytest

import auth
import build_db
import org
import tools


MGR_A = {"employee_id": "EMP-201", "name": "مدير أ", "role": "manager"}
MGR_B = {"employee_id": "EMP-202", "name": "مدير ب", "role": "manager"}
MGR_C = {"employee_id": "EMP-203", "name": "مدير ج", "role": "manager"}
EMP_A1 = {"employee_id": "EMP-211", "name": "موظف أ1", "role": "employee"}
EMP_A3 = {"employee_id": "EMP-213", "name": "موظف أ3", "role": "employee"}
EMP_B1 = {"employee_id": "EMP-221", "name": "موظف ب1", "role": "employee"}
ADMIN = {"employee_id": "EMP-100", "name": "مسؤول", "role": "admin"}


@pytest.fixture(autouse=True)
def hierarchy(tmp_path, monkeypatch):
    db = tmp_path / "tier.db"
    build_db.build(db)

    conn = sqlite3.connect(db)
    conn.executemany(
        """INSERT INTO employees
           (employee_id, name, department, job_title, annual_leave, leave_used)
           VALUES (?,?,?,?,?,?)""",
        [("EMP-201", "مدير أ", "تقنية المعلومات", "مدير", 30, 0),
         ("EMP-202", "مدير ب", "تقنية المعلومات", "رئيس فريق", 30, 0),
         ("EMP-203", "مدير ج", "المالية", "مدير", 30, 0),
         ("EMP-211", "موظف أ1", "تقنية المعلومات", "مهندس", 30, 5),
         ("EMP-212", "موظف أ2", "تقنية المعلومات", "مهندس", 30, 5),
         ("EMP-213", "موظف أ3", "تقنية المعلومات", "مهندس", 30, 5),
         ("EMP-221", "موظف ب1", "تقنية المعلومات", "مهندس", 30, 5),
         ("EMP-231", "موظف ج1", "المالية", "محاسب", 30, 5),
         ("EMP-100", "مسؤول", "تقنية المعلومات", "مسؤول النظام", 30, 0)],
    )
    conn.commit(); conn.close()

    import migrations
    migrations.m005_organization_structure_backfill(db)

    monkeypatch.setattr(org, "DB_PATH", db)
    monkeypatch.setattr(tools, "DB_PATH", db)

    org.assign_manager("EMP-211", "EMP-201", path=db)
    org.assign_manager("EMP-212", "EMP-201", path=db)
    org.assign_manager("EMP-213", "EMP-212", path=db)     # غير مباشر
    org.assign_manager("EMP-221", "EMP-202", path=db)
    org.assign_manager("EMP-231", "EMP-203", path=db)
    return db


# ---------- النطاق ----------

@pytest.mark.parametrize("role,expected", [
    ("employee", auth.SCOPE_SELF),
    ("manager", auth.SCOPE_TEAM),
    ("admin", auth.SCOPE_ORG),
])
def test_scope_maps_to_role(role, expected):
    assert auth.scope_for({"role": role}) == expected


@pytest.mark.parametrize("bad", ["Admin", "ADMIN", "superadmin", "", None, "root"])
def test_unknown_role_gets_narrowest_scope(bad):
    """فشل مغلق: دور غير معروف يُعامَل كموظف."""
    assert auth.scope_for({"role": bad}) == auth.SCOPE_SELF


def test_missing_identity_is_internal_scope():
    """غياب الهوية = استدعاء داخلي (سكربت/زرع) لا طلب مصادَق."""
    assert auth.scope_for(None) == auth.SCOPE_ORG


def test_elevated_covers_manager_and_admin():
    assert auth.is_elevated({"role": "manager"})
    assert auth.is_elevated({"role": "admin"})
    assert not auth.is_elevated({"role": "employee"})


def test_is_manager_still_means_manager_only():
    """التمييز محفوظ: is_manager ليس بوابة "مدير فأعلى"."""
    assert auth.is_manager({"role": "manager"})
    assert not auth.is_manager({"role": "admin"})


# ---------- شجرة الإدارة ----------

def test_subordinates_include_indirect_reports(hierarchy):
    """مدير القسم يرى تحت رئيس الفريق أيضاً."""
    subs = set(org.subordinate_ids("EMP-201", path=hierarchy))
    assert subs == {"EMP-211", "EMP-212", "EMP-213"}


def test_direct_reports_exclude_indirect(hierarchy):
    assert set(org.team_member_ids("EMP-201", path=hierarchy)) == {"EMP-211", "EMP-212"}


def test_manages_answers_tree_membership(hierarchy):
    assert org.manages("EMP-201", "EMP-213", path=hierarchy)
    assert not org.manages("EMP-201", "EMP-221", path=hierarchy)


def test_manager_without_reports_has_empty_tree(hierarchy):
    assert org.subordinate_ids("EMP-231", path=hierarchy) == []


# ---------- قراءة بيانات الموظفين ----------

def test_employee_reads_only_self(hierarchy):
    assert tools.get_employee("موظف أ1", EMP_A1)["found"]
    assert not tools.get_employee("موظف أ3", EMP_A1).get("found")
    assert not tools.get_employee("مدير أ", EMP_A1).get("found")


def test_manager_reads_own_tree(hierarchy):
    for name in ("موظف أ1", "موظف أ2", "موظف أ3"):
        assert tools.get_employee(name, MGR_A)["found"], name


def test_manager_cannot_read_peer_team_same_department(hierarchy):
    """⚠️ جوهر الخطوة: نفس القسم، خطّ رفع آخر → ممنوع."""
    assert not tools.get_employee("موظف ب1", MGR_A).get("found")
    assert not tools.get_employee("موظف أ1", MGR_B).get("found")


def test_manager_cannot_read_other_department(hierarchy):
    assert not tools.get_employee("موظف ج1", MGR_A).get("found")


def test_manager_cannot_read_another_manager(hierarchy):
    assert not tools.get_employee("مدير ب", MGR_A).get("found")


def test_admin_reads_everyone(hierarchy):
    for name in ("موظف أ1", "موظف ب1", "موظف ج1", "مدير أ", "مدير ج"):
        assert tools.get_employee(name, ADMIN)["found"], name


# ---------- الأرصدة ----------

def test_balance_follows_the_same_scope(hierarchy):
    assert tools.get_leave_balance("موظف أ3", MGR_A)["found"]
    assert not tools.get_leave_balance("موظف ب1", MGR_A).get("found")
    assert tools.get_leave_balance("موظف ب1", ADMIN)["found"]


# ---------- الطلبات المعلّقة ----------

def _submit(name, actor, days=2, offset=10, path=None):
    from datetime import date, timedelta
    start = (date.today() + timedelta(days=offset)).isoformat()
    return tools.submit_leave_request(name, start, days, "سبب", actor=actor)


def test_manager_sees_only_their_teams_pending(hierarchy):
    _submit("موظف أ1", EMP_A1)
    _submit("موظف ب1", EMP_B1, offset=20)

    names = {r["employee_name"] for r in tools.list_pending_requests(MGR_A)["requests"]}
    assert names == {"موظف أ1"}


def test_manager_sees_indirect_reports_pending(hierarchy):
    _submit("موظف أ3", EMP_A3)
    names = {r["employee_name"] for r in tools.list_pending_requests(MGR_A)["requests"]}
    assert "موظف أ3" in names


def test_admin_sees_all_pending(hierarchy):
    _submit("موظف أ1", EMP_A1)
    _submit("موظف ب1", EMP_B1, offset=20)
    assert tools.list_pending_requests(ADMIN)["count"] == 2


def test_employee_still_denied_pending(hierarchy):
    assert "error" in tools.list_pending_requests(EMP_A1)


def test_manager_with_no_reports_sees_nothing(hierarchy):
    """مدير بلا فريق يرى صفراً — لا المؤسسة كلها."""
    _submit("موظف أ1", EMP_A1)
    lonely = {"employee_id": "EMP-231", "name": "موظف ج1", "role": "manager"}
    assert tools.list_pending_requests(lonely)["count"] == 0


# ---------- المراجعة ----------

def test_manager_reviews_own_team(hierarchy):
    _submit("موظف أ1", EMP_A1)
    rid = tools.list_pending_requests(MGR_A)["requests"][0]["id"]
    assert tools.review_leave_request(rid, "approve", "موافق", MGR_A)["reviewed"]


def test_manager_cannot_review_peer_team(hierarchy):
    """⚠️ الأهم أمنياً: قرار على طلب خارج النطاق."""
    _submit("موظف ب1", EMP_B1)
    rid = tools.list_pending_requests(MGR_B)["requests"][0]["id"]

    r = tools.review_leave_request(rid, "approve", "تجاوز", MGR_A)
    assert not r["reviewed"]
    assert "خارج نطاق" in r["reason"]

    still = tools.list_pending_requests(MGR_B)["requests"][0]
    assert still["id"] == rid


def test_manager_cannot_review_across_departments(hierarchy):
    _submit("موظف ج1", {"employee_id": "EMP-231", "name": "موظف ج1", "role": "employee"})
    rid = tools.list_pending_requests(MGR_C)["requests"][0]["id"]
    assert not tools.review_leave_request(rid, "approve", "", MGR_A)["reviewed"]


def test_admin_reviews_anything(hierarchy):
    _submit("موظف ب1", EMP_B1)
    rid = tools.list_pending_requests(ADMIN)["requests"][0]["id"]
    assert tools.review_leave_request(rid, "approve", "", ADMIN)["reviewed"]


def test_employee_cannot_review(hierarchy):
    _submit("موظف أ1", EMP_A1)
    rid = tools.list_pending_requests(MGR_A)["requests"][0]["id"]
    assert not tools.review_leave_request(rid, "approve", "", EMP_A1)["reviewed"]


# ---------- الإلغاء ----------

def test_manager_cancels_own_team_request(hierarchy):
    _submit("موظف أ1", EMP_A1)
    rid = tools.list_pending_requests(MGR_A)["requests"][0]["id"]
    assert tools.cancel_leave_request(rid, actor=MGR_A)["cancelled"]


def test_manager_cannot_cancel_peer_team_request(hierarchy):
    _submit("موظف ب1", EMP_B1)
    rid = tools.list_pending_requests(MGR_B)["requests"][0]["id"]

    assert not tools.cancel_leave_request(rid, actor=MGR_A)["cancelled"]
    assert tools.list_pending_requests(MGR_B)["count"] == 1


# ---------- عرض الطلبات ----------

def test_request_listing_is_scoped(hierarchy):
    _submit("موظف أ1", EMP_A1)
    _submit("موظف ب1", EMP_B1, offset=20)

    a = {r["employee_name"] for r in tools.list_leave_requests("", MGR_A)["requests"]}
    b = {r["employee_name"] for r in tools.list_leave_requests("", MGR_B)["requests"]}
    assert a == {"موظف أ1"} and b == {"موظف ب1"}
    assert tools.list_leave_requests("", ADMIN)["count"] == 2


def test_employee_listing_unchanged(hierarchy):
    _submit("موظف أ1", EMP_A1)
    _submit("موظف ب1", EMP_B1, offset=20)
    assert {r["employee_name"] for r in
            tools.list_leave_requests("", EMP_A1)["requests"]} == {"موظف أ1"}


def test_naming_another_employee_does_not_widen_scope(hierarchy):
    _submit("موظف ب1", EMP_B1)
    assert tools.list_leave_requests("موظف ب1", MGR_A)["count"] == 0


# ---------- الأقسام ----------

def test_manager_sees_departments_of_their_team(hierarchy):
    r = tools.list_department_employees("تقنية المعلومات", MGR_A)
    assert "error" not in r
    names = {e["name"] for e in r["employees"]}
    assert "موظف أ1" in names


def test_manager_denied_department_without_their_team(hierarchy):
    assert "error" in tools.list_department_employees("المالية", MGR_A)


def test_admin_sees_any_department(hierarchy):
    assert "error" not in tools.list_department_employees("المالية", ADMIN)


def test_summary_totals_differ_by_tier(hierarchy):
    """إجمالي المؤسسة للمسؤول؛ المدير يرى أقسامه."""
    mgr = tools.department_summary(MGR_A)
    adm = tools.department_summary(ADMIN)
    assert mgr["total_monthly_transport"] < adm["total_monthly_transport"]
    assert "error" in tools.department_summary(EMP_A1)


# ---------- تصعيد ----------

def test_payload_cannot_grant_org_scope(hierarchy):
    forged = dict(MGR_A, scope="org", is_admin=True, permissions=["*"])
    assert not tools.get_employee("موظف ب1", forged).get("found")


def test_employee_claiming_manager_id_gains_nothing(hierarchy):
    """حقل manager_id في الهوية لا يُقرأ — الشجرة من القاعدة."""
    forged = dict(EMP_A1, manager_id="EMP-201", team=["EMP-221"])
    assert not tools.get_employee("موظف ب1", forged).get("found")


def test_scope_derives_from_stored_tree_not_claims(hierarchy):
    """إزالة العلاقة من القاعدة تُضيّق النطاق فوراً."""
    assert tools.get_employee("موظف أ1", MGR_A)["found"]
    org.assign_manager("EMP-211", None, path=hierarchy)
    assert not tools.get_employee("موظف أ1", MGR_A).get("found")
