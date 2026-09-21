"""علاقات البنية التنظيمية وقواعد سلامتها.

لا مفاتيح أجنبية في SQLite هنا — القواعد تُفرَض في org.py
ويحرسها هذا الملف. غياب القيد لا يعني غياب القاعدة.
"""

import sqlite3

import pytest

import build_db
import migrations
import org


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    path = tmp_path / "org.db"
    build_db.build(path)
    monkeypatch.setattr(org, "DB_PATH", path)
    return path


# ---------- قسم ← موظفون ----------

def test_departments_are_listed_with_ids():
    depts = org.departments()
    assert len(depts) == 5
    assert all(d["department_id"].startswith("DEPT-") for d in depts)


def test_department_lookup_by_id():
    first = org.departments()[0]
    assert org.department_by_id(first["department_id"])["name"] == first["name"]


def test_unknown_department_returns_none():
    assert org.department_by_id("DEPT-999") is None


def test_employees_resolve_to_their_department():
    it = next(d for d in org.departments() if d["name"] == "تقنية المعلومات")
    members = org.employees_in_department(it["department_id"])
    assert {m["name"] for m in members} == {"أحمد الشمري", "فهد المطيري"}


def test_every_seeded_employee_has_a_department():
    linked = sum(len(org.employees_in_department(d["department_id"]))
                 for d in org.departments())
    assert linked == 6


def test_computed_headcount_reflects_reality(db):
    """العمود المخزّن بيانات زرع ثابتة؛ الحساب هو الحقيقة."""
    it = next(d for d in org.departments() if d["name"] == "تقنية المعلومات")
    assert org.department_headcount(it["department_id"]) == 2
    assert it["headcount"] == 24          # المخزّن لم يُمسّ


# ---------- مدير ← تقارير مباشرة ----------

def test_no_manager_assigned_by_default():
    """الهجرة لا تخترع علاقات — الإسناد قرار لاحق."""
    assert org.direct_reports("EMP-002") == []
    assert org.manager_of("EMP-003") is None


def test_assign_manager_creates_relationship():
    org.assign_manager("EMP-003", "EMP-002")

    assert org.manager_of("EMP-003")["name"] == "نورة العتيبي"
    assert [r["employee_id"] for r in org.direct_reports("EMP-002")] == ["EMP-003"]


def test_team_member_ids_matches_direct_reports():
    org.assign_manager("EMP-003", "EMP-002")
    org.assign_manager("EMP-001", "EMP-002")
    assert sorted(org.team_member_ids("EMP-002")) == ["EMP-001", "EMP-003"]


def test_manager_can_be_cleared():
    org.assign_manager("EMP-003", "EMP-002")
    org.assign_manager("EMP-003", None)
    assert org.manager_of("EMP-003") is None


def test_management_chain_walks_upward():
    org.assign_manager("EMP-003", "EMP-002")
    org.assign_manager("EMP-002", "EMP-001")

    chain = [m["employee_id"] for m in org.management_chain("EMP-003")]
    assert chain == ["EMP-002", "EMP-001"]


def test_chain_is_empty_without_manager():
    assert org.management_chain("EMP-003") == []


# ---------- قواعد السلامة ----------

def test_self_manager_is_rejected():
    with pytest.raises(org.OrgIntegrityError, match="مديراً لنفسه"):
        org.assign_manager("EMP-003", "EMP-003")
    assert org.manager_of("EMP-003") is None


def test_unknown_manager_is_rejected():
    with pytest.raises(org.OrgIntegrityError, match="لا يوجد مدير"):
        org.assign_manager("EMP-003", "EMP-999")


def test_unknown_employee_is_rejected():
    with pytest.raises(org.OrgIntegrityError, match="لا يوجد موظف"):
        org.assign_manager("EMP-999", "EMP-002")


def test_direct_cycle_is_rejected():
    """أ يدير ب، فلا يجوز أن يدير ب أ."""
    org.assign_manager("EMP-003", "EMP-002")
    with pytest.raises(org.OrgIntegrityError, match="دورة"):
        org.assign_manager("EMP-002", "EMP-003")


def test_deep_cycle_is_rejected():
    org.assign_manager("EMP-003", "EMP-002")
    org.assign_manager("EMP-002", "EMP-001")
    with pytest.raises(org.OrgIntegrityError, match="دورة"):
        org.assign_manager("EMP-001", "EMP-003")


def test_rejected_assignment_leaves_state_unchanged():
    org.assign_manager("EMP-003", "EMP-002")
    with pytest.raises(org.OrgIntegrityError):
        org.assign_manager("EMP-003", "EMP-003")
    assert org.manager_of("EMP-003")["employee_id"] == "EMP-002"


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_blank_manager_clears_rather_than_errors(bad):
    org.assign_manager("EMP-003", "EMP-002")
    org.assign_manager("EMP-003", bad)
    assert org.manager_of("EMP-003") is None


# ---------- نقل الأقسام ----------

def test_assign_department_updates_id_and_name():
    target = next(d for d in org.departments() if d["name"] == "التسويق")
    org.assign_department("EMP-003", target["department_id"])

    emp = org.employee_by_id("EMP-003")
    assert emp["department_id"] == target["department_id"]
    assert emp["department"] == "التسويق"       # الاسم يبقى متزامناً


def test_assign_unknown_department_is_rejected():
    with pytest.raises(org.OrgIntegrityError, match="لا يوجد قسم"):
        org.assign_department("EMP-003", "DEPT-999")


def test_department_manager_assignment():
    d = org.departments()[0]
    org.assign_department_manager(d["department_id"], "EMP-002")
    assert org.department_by_id(d["department_id"])["manager_id"] == "EMP-002"


def test_department_manager_must_exist():
    d = org.departments()[0]
    with pytest.raises(org.OrgIntegrityError):
        org.assign_department_manager(d["department_id"], "EMP-999")


# ---------- كشف الأيتام ----------

def test_integrity_clean_after_seed(db):
    assert migrations.validate_integrity(db)["ok"]


def test_orphan_manager_reference_is_detected(db):
    """كتابة مباشرة تتجاوز الخدمة — الفحص يكشفها."""
    conn = sqlite3.connect(db)
    conn.execute("UPDATE employees SET manager_id='EMP-999' WHERE employee_id='EMP-003'")
    conn.commit(); conn.close()

    report = migrations.validate_integrity(db)
    assert not report["ok"]
    assert any(r["employee_id"] == "EMP-003" for r in report["orphan_manager_refs"])


def test_orphan_department_reference_is_detected(db):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE employees SET department_id='DEPT-999' WHERE employee_id='EMP-003'")
    conn.commit(); conn.close()

    report = migrations.validate_integrity(db)
    assert not report["ok"]
    assert any(r["employee_id"] == "EMP-003" for r in report["orphan_department_refs"])


def test_self_manager_written_directly_is_detected(db):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE employees SET manager_id='EMP-003' WHERE employee_id='EMP-003'")
    conn.commit(); conn.close()

    report = migrations.validate_integrity(db)
    assert not report["ok"]
    assert report["self_managers"][0]["employee_id"] == "EMP-003"


def test_orphan_department_manager_is_detected(db):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE departments SET manager_id='EMP-999' WHERE department_id='DEPT-001'")
    conn.commit(); conn.close()

    assert not migrations.validate_integrity(db)["ok"]


def test_chain_survives_corrupt_cycle(db):
    """دورة مكتوبة مباشرة لا تُدخل الاجتياز في حلقة لا نهائية."""
    conn = sqlite3.connect(db)
    conn.execute("UPDATE employees SET manager_id='EMP-002' WHERE employee_id='EMP-003'")
    conn.execute("UPDATE employees SET manager_id='EMP-003' WHERE employee_id='EMP-002'")
    conn.commit(); conn.close()

    chain = org.management_chain("EMP-003")
    assert len(chain) <= org.MAX_CHAIN_DEPTH


# ---------- مسار القاعدة ----------

def test_org_respects_reassigned_db_path(tmp_path, monkeypatch):
    other = tmp_path / "other.db"
    build_db.build(other)
    monkeypatch.setattr(org, "DB_PATH", other)
    assert len(org.departments()) == 5


def test_explicit_path_overrides_module_value(tmp_path):
    explicit = tmp_path / "explicit.db"
    build_db.build(explicit)
    assert len(org.departments(path=explicit)) == 5
