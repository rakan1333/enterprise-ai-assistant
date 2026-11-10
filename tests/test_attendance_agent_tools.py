"""أدوات الحضور في طبقة الوكيل — النطاق والخصوصية وحدّ ما لا يوجد.

ثلاث أدوات قراءة، ولا أداة كتابة. أكثر ما يختبره هذا الملف هو
**غياب** شيء: أن `check_in` و`check_out` ليستا أداتَي وكيل، وأن
الإحداثيات لا تصل إلى سياق النموذج من أي مسار.

القرار مشروح في `tools.py` فوق قسم أدوات الحضور. الاختبار هنا
يحرسه من النقض بالسهو — إضافة الأداة لاحقاً تُسقط اختباراً
باسمها الصريح بدل أن تمرّ بصمت.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import agent
import attendance as att
import build_db
import migrations
import tools

EMPLOYEE = {"employee_id": "EMP-003", "name": "خالد الدوسري", "role": "employee"}
OTHER    = {"employee_id": "EMP-001", "name": "أحمد الشمري",  "role": "employee"}
MANAGER  = {"employee_id": "EMP-002", "name": "نورة العتيبي", "role": "manager"}
ADMIN    = {"employee_id": "EMP-100", "name": "مسؤول النظام", "role": "admin"}
NO_ID    = {"employee_id": "", "name": "بلا سجل", "role": "employee"}

# ما لا يجوز أن يبلغ سياق النموذج من أي أداة
PRIVATE = ("in_reported_lat", "in_reported_lon", "in_reported_accuracy_m",
           "out_reported_lat", "out_reported_lon", "out_reported_accuracy_m",
           "in_computed_distance_m", "out_computed_distance_m",
           "in_request_ip", "out_request_ip", "latitude", "longitude")


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    path = tmp_path / "agent_att.db"
    build_db.build(path)
    migrations.migrate(path)

    import org
    monkeypatch.setattr(org, "DB_PATH", path)
    org.assign_manager("EMP-003", "EMP-002", path=path)
    org.assign_manager("EMP-001", "EMP-002", path=path)

    monkeypatch.setattr(tools, "DB_PATH", path)
    monkeypatch.setattr(att, "DB_PATH", path)
    att.create_site("المقرّ الرئيسي", 24.7136, 46.6753, radius_m=300, path=path)
    return path


def seed(db, employee_id, hours_ago=9, closed=True, verdict="inside"):
    """جلسة مؤرّخة في الماضي مع كل الحقول الخاصّة مملوءة.

    تُملأ عمداً: أداة تحجبها لأن الصفّ خالٍ منها لم تُختبر.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = (now - timedelta(hours=hours_ago)).isoformat()
    end = (now - timedelta(hours=1)).isoformat()
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """INSERT INTO attendance_records
                 (employee_id, check_in_at, check_out_at, in_verdict, out_verdict,
                  in_reported_lat, in_reported_lon, in_reported_accuracy_m,
                  in_computed_distance_m, in_request_ip, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (employee_id, start, end if closed else None, verdict,
             "inside" if closed else None,
             24.7136, 46.6753, 11.0, 42.0, "203.0.113.9", start))
        conn.commit()
    finally:
        conn.close()
    return start, end


def no_private(result):
    """تفتيش الردّ المُسلسَل — مفاتيح وقيماً معاً."""
    body = json.dumps(result, ensure_ascii=False)
    for field in PRIVATE:
        assert field not in body, f"حقل خاص {field!r} بلغ سياق النموذج"
    for value in ("24.7136", "46.6753", "203.0.113.9", "42.0"):
        assert value not in body, f"قيمة خاصّة {value} بلغت سياق النموذج"


# ================= الحدّ: ما لا يوجد =================

def test_check_in_is_not_an_agent_tool():
    """قرار معماري لا سهو: الإحداثيات عبر النموذج ادّعاء لا قياس."""
    names = {t["function"]["name"] for t in agent.TOOL_SPECS}
    assert "check_in" not in names
    assert "attendance_check_in" not in names


def test_check_out_is_not_an_agent_tool():
    names = {t["function"]["name"] for t in agent.TOOL_SPECS}
    assert "check_out" not in names
    assert "attendance_check_out" not in names


def test_no_attendance_write_tool_of_any_name():
    """حارس أوسع: أي أداة تكتب حضوراً تُسقط هذا الاختبار."""
    names = {t["function"]["name"] for t in agent.TOOL_SPECS}
    writing = {n for n in names
               if any(v in n for v in ("check_in", "check_out", "record_attendance",
                                       "resolve_duplicate", "review_correction",
                                       "create_correction", "create_site"))}
    assert not writing, f"أداة كتابة حضور مسجّلة: {writing}"


def test_the_write_tool_set_is_unchanged():
    """الطبقة 3 قراءة فقط — WRITE_TOOLS كما كانت حرفاً بحرف."""
    assert agent.WRITE_TOOLS == {"submit_leave_request", "cancel_leave_request",
                                 "review_leave_request"}
    assert set(agent.WRITE_SUCCESS_KEY) == agent.WRITE_TOOLS


def test_the_three_tools_are_registered():
    names = [t["function"]["name"] for t in agent.TOOL_SPECS]
    for n in ("get_my_attendance", "get_team_attendance", "list_sites"):
        assert n in names
    assert len(names) == len(set(names)), "لا اسم مكرّر"


def test_resolve_duplicate_session_stays_off_every_surface():
    """المسار الوحيد الذي يكتب انصرافاً لم يقسه السياج — سطر أوامر فقط."""
    assert not hasattr(tools, "resolve_duplicate_session")
    src = open("agent.py", encoding="utf-8").read()
    assert "resolve_duplicate_session" not in src


# ================= get_my_attendance =================

def test_employee_sees_their_own_sessions(db):
    seed(db, "EMP-003")
    r = tools.get_my_attendance(actor=EMPLOYEE)
    assert r["authorized"] is True and r["count"] == 1


def test_it_does_not_see_anyone_elses(db):
    seed(db, "EMP-001")
    assert tools.get_my_attendance(actor=EMPLOYEE)["count"] == 0


def test_a_forged_employee_id_in_args_is_ignored(db):
    """التوقيع لا يحوي employee_id أصلاً — فالتزوير يرتدّ خطأً."""
    seed(db, "EMP-001")
    with pytest.raises(TypeError):
        tools.get_my_attendance(employee_id="EMP-001", actor=EMPLOYEE)


def test_the_dispatcher_drops_a_forged_identity(db):
    """المسار الحقيقي: _execute_tool يمرّر days وحدها."""
    seed(db, "EMP-001")
    r = agent._execute_tool(None, "get_my_attendance",
                            {"days": 7, "employee_id": "EMP-001",
                             "role": "admin", "actor": ADMIN},
                            EMPLOYEE)
    assert r["count"] == 0, "معرّف النموذج لم يغيّر صاحب السجل"


def test_no_identity_is_refused(db):
    assert tools.get_my_attendance(actor=NO_ID)["authorized"] is False


def test_my_attendance_carries_no_coordinates(db):
    seed(db, "EMP-003")
    no_private(tools.get_my_attendance(actor=EMPLOYEE))


def test_minutes_stay_separated_by_source(db):
    seed(db, "EMP-003")
    t = tools.get_my_attendance(actor=EMPLOYEE)["totals"]
    assert t["verified_minutes"] > 0
    assert t["administrative_minutes"] == 0
    assert t["total_minutes"] == t["verified_minutes"]


def test_an_administratively_closed_session_is_counted_as_unresolved(db):
    seed(db, "EMP-003")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE attendance_records SET out_verdict = ?",
                 (att.VERDICT_CONFLICT_CLOSED,))
    conn.commit(); conn.close()

    r = tools.get_my_attendance(actor=EMPLOYEE)
    assert r["totals"]["unresolved_sessions"] == 1
    assert r["totals"]["total_minutes"] == 0
    assert r["sessions"][0]["duration_minutes"] is None


def test_the_open_session_is_reported(db):
    seed(db, "EMP-003", closed=False)
    assert tools.get_my_attendance(actor=EMPLOYEE)["open_session"] is not None


def test_no_open_session_reads_as_none(db):
    seed(db, "EMP-003")
    assert tools.get_my_attendance(actor=EMPLOYEE)["open_session"] is None


@pytest.mark.parametrize("bad", [0, -1, 32, 100000, "كثير", None])
def test_days_outside_the_range_is_refused(db, bad):
    """النموذج يملك هذا المعامل وحده، فيُقصّ قبل أن يلمس القاعدة."""
    r = tools.get_my_attendance(days=bad, actor=EMPLOYEE)
    assert r["authorized"] is False
    assert "sessions" not in r


@pytest.mark.parametrize("value,window", [(7.0, 7), (3.7, 3), ("5", 5)])
def test_a_numeric_day_count_is_truncated_not_refused(db, value, window):
    """النموذج يرسل 7.0 بحكم JSON، ورفضها قسوة بلا فائدة.

    القصّ إلى صحيح مقبول لنافذة قراءة: 3.7 يوم ليس له معنى مقصود،
    والحدّ الذي يهمّ هو المدى لا الكسر.
    """
    r = tools.get_my_attendance(days=value, actor=EMPLOYEE)
    assert r["authorized"] is True
    assert r["days"] == window


def test_the_window_actually_filters(db):
    seed(db, "EMP-003", hours_ago=9)             # اليوم
    seed(db, "EMP-003", hours_ago=24 * 20)       # قبل عشرين يوماً
    assert tools.get_my_attendance(days=1, actor=EMPLOYEE)["count"] == 1
    assert tools.get_my_attendance(days=31, actor=EMPLOYEE)["count"] == 2


# ================= get_team_attendance =================

def test_employee_is_refused_the_team_view(db):
    seed(db, "EMP-003")
    r = tools.get_team_attendance(actor=EMPLOYEE)
    assert r["authorized"] is False
    assert r["employees"] == []


def test_manager_sees_their_subtree(db):
    seed(db, "EMP-003")
    seed(db, "EMP-001")
    r = tools.get_team_attendance(actor=MANAGER)
    assert {e["employee_id"] for e in r["employees"]} == {"EMP-003", "EMP-001"}
    assert r["scope"] == "team"


def test_manager_does_not_see_outside_their_subtree(db):
    seed(db, "EMP-777")
    r = tools.get_team_attendance(actor=MANAGER)
    assert "EMP-777" not in {e["employee_id"] for e in r["employees"]}


def test_admin_sees_the_organization(db):
    seed(db, "EMP-777")
    r = tools.get_team_attendance(actor=ADMIN)
    assert r["scope"] == "org"
    assert "EMP-777" in {e["employee_id"] for e in r["employees"]}


def test_team_view_carries_no_coordinates(db):
    seed(db, "EMP-003")
    no_private(tools.get_team_attendance(actor=MANAGER))
    no_private(tools.get_team_attendance(actor=ADMIN))


def test_team_view_gives_a_summary_not_individual_sessions(db):
    """المدير يعرف من داوم وكم، لا دخوله وخروجه ساعةً بساعة."""
    seed(db, "EMP-003", hours_ago=30)
    seed(db, "EMP-003", hours_ago=9)
    row = tools.get_team_attendance(actor=MANAGER)["employees"][0]
    assert row["sessions"] == 2
    assert "check_in_at" not in row
    assert "sessions_detail" not in row


def test_names_are_resolved(db):
    seed(db, "EMP-003")
    row = tools.get_team_attendance(actor=MANAGER)["employees"][0]
    assert row["name"] == "خالد الدوسري"


def test_open_now_is_reported_per_employee(db):
    seed(db, "EMP-003", closed=False)
    row = tools.get_team_attendance(actor=MANAGER)["employees"][0]
    assert row["open_now"] is True


def test_team_days_is_bounded_too(db):
    assert tools.get_team_attendance(days=9999, actor=MANAGER)["authorized"] is False


def test_a_manager_with_no_reports_sees_no_one(db):
    """مدير بلا تقارير يحصل على فارغ — لا تراجع إلى المؤسسة."""
    seed(db, "EMP-001")
    lonely = {"employee_id": "EMP-005", "name": "فهد المطيري", "role": "manager"}
    r = tools.get_team_attendance(actor=lonely)
    assert r["authorized"] is True
    assert r["employees"] == []


# ================= list_sites =================

def test_everyone_sees_active_sites(db):
    for who in (EMPLOYEE, MANAGER, ADMIN):
        assert tools.list_sites(actor=who)["count"] == 1


def test_sites_carry_no_coordinates(db):
    """النموذج لا يرسم خريطة، وإحداثيات المقرّ في سياق المحادثة
    تضع للمستخدم ما ينسخه في ادّعاء موقع."""
    r = tools.list_sites(actor=EMPLOYEE)
    body = json.dumps(r, ensure_ascii=False)
    assert "latitude" not in body and "longitude" not in body
    assert "24.7136" not in body and "46.6753" not in body
    assert r["sites"][0]["radius_m"] == 300


def test_inactive_sites_are_admin_only(db):
    s = att.create_site("موقع مغلق", 25.0, 47.0, path=db)
    att.set_site_active(s["site_id"], False, path=db)

    assert s["site_id"] not in {x["site_id"] for x in
                                tools.list_sites(actor=EMPLOYEE)["sites"]}
    assert s["site_id"] in {x["site_id"] for x in
                            tools.list_sites(actor=ADMIN)["sites"]}


def test_manager_does_not_see_inactive_sites(db):
    """المواقع إدارة لا إشراف — المدير ليس مسؤولاً."""
    s = att.create_site("موقع مغلق", 25.0, 47.0, path=db)
    att.set_site_active(s["site_id"], False, path=db)
    assert s["site_id"] not in {x["site_id"] for x in
                                tools.list_sites(actor=MANAGER)["sites"]}


# ================= عبر المُوزِّع =================

@pytest.mark.parametrize("name", ["get_my_attendance", "get_team_attendance",
                                  "list_sites"])
def test_the_dispatcher_reaches_each_tool(db, name):
    seed(db, "EMP-003")
    r = agent._execute_tool(None, name, {}, ADMIN)
    assert "error" not in r and r["authorized"] is True


def test_the_dispatcher_passes_days_through(db):
    seed(db, "EMP-003", hours_ago=24 * 20)
    assert agent._execute_tool(None, "get_my_attendance", {"days": 1},
                               EMPLOYEE)["count"] == 0
    assert agent._execute_tool(None, "get_my_attendance", {"days": 31},
                               EMPLOYEE)["count"] == 1


def test_the_dispatcher_enforces_the_team_gate(db):
    seed(db, "EMP-003")
    r = agent._execute_tool(None, "get_team_attendance", {}, EMPLOYEE)
    assert r["authorized"] is False


def test_an_unknown_attendance_tool_name_is_rejected(db):
    r = agent._execute_tool(None, "check_in", {"latitude": 24.7}, EMPLOYEE)
    assert "error" in r and "غير معروفة" in r["error"]


def test_every_dispatcher_response_is_free_of_coordinates(db):
    seed(db, "EMP-003")
    for name in ("get_my_attendance", "get_team_attendance", "list_sites"):
        no_private(agent._execute_tool(None, name, {}, ADMIN))
