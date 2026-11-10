"""نقاط الحضور عبر HTTP — الصلاحية والخصوصية وحدّ الثقة.

الهيكل (فوق قاعدة التجهيزات المشتركة):

    نورة (EMP-002, manager) ─┬─ خالد (EMP-003, employee)
                             └─ أحمد (EMP-001, employee)
    مسؤول النظام (EMP-100, admin)

الاختبارات تضرب HTTP مباشرةً بلا متصفّح: الحجب في نماذج الردّ
لا في الواجهة، وما لا يُعرَّف لا يخرج مهما حوى الصفّ.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import attendance as att
import auth
import geofence
import migrations
import org
import tools

RIYADH = {"latitude": 24.7136, "longitude": 46.6753, "accuracy_m": 10.0}
JEDDAH = {"latitude": 21.4858, "longitude": 39.1925, "accuracy_m": 10.0}

# ما لا يجوز أن يظهر في أي ردّ على الإطلاق
SECRETS = ("password_hash", "$2b$", "AUTH_SECRET", "Traceback",
           "SELECT ", "sqlite3", "/home/", "C:\\")

# ما لا يجوز أن يظهر في ردّ المدير أو المسؤول عن غيرهما
PRIVATE_FIELDS = ("in_reported_lat", "in_reported_lon", "in_reported_accuracy_m",
                  "out_reported_lat", "out_reported_lon", "out_reported_accuracy_m",
                  "in_request_ip", "out_request_ip",
                  "in_computed_distance_m", "out_computed_distance_m")


@pytest.fixture(scope="module", autouse=True)
def attendance_db(api_client):
    """يرقّي قاعدة الاختبار ويوجّه وحدة الحضور إليها."""
    db = api_client._db
    migrations.migrate(db)
    att.DB_PATH = db
    tools.DB_PATH = db
    org.DB_PATH = db
    return db


@pytest.fixture(scope="module")
def site(attendance_db):
    return att.create_site("المقرّ الرئيسي", 24.7136, 46.6753,
                           radius_m=300, path=attendance_db)


@pytest.fixture(autouse=True)
def clean(attendance_db, site):
    conn = sqlite3.connect(attendance_db)
    conn.execute("DELETE FROM attendance_records")
    conn.execute("DELETE FROM attendance_corrections")
    conn.commit(); conn.close()
    import ratelimit
    ratelimit.attendance_limiter.reset()


@pytest.fixture(scope="module")
def admin_h(admin):
    return admin


@pytest.fixture(scope="module")
def manager_h(manager):
    return manager


@pytest.fixture(scope="module")
def employee_h(employee):
    return employee


@pytest.fixture(scope="module")
def other_h(other_employee):
    return other_employee


def no_leak(response):
    """تفتيش النصّ الخام — مفاتيح وقيماً معاً."""
    body = response.text
    for token in SECRETS:
        assert token not in body, f"تسريب {token!r} في {response.url}"


def no_private(response):
    body = response.text
    for field in PRIVATE_FIELDS:
        assert field not in body, f"حقل خاص {field!r} ظهر في ردّ جماعي"


def seed_session(db, employee_id, hours_ago=9, closed=True, verdict="inside"):
    """جلسة مؤرّخة في الماضي — check_in يكتب الوقت الحالي فلا يصلح.

    end يعود دائماً: الجلسة المفتوحة تحتاج وقتاً مقترحاً لإغلاقها،
    وclosed يقرّر هل كُتب في الصفّ لا هل يُعرف.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = (now - timedelta(hours=hours_ago)).isoformat()
    end = (now - timedelta(hours=1)).isoformat()
    stored_out = end if closed else None
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            """INSERT INTO attendance_records
                 (employee_id, site_id, check_in_at, check_out_at, in_verdict,
                  out_verdict, in_reported_lat, in_reported_lon,
                  in_reported_accuracy_m, in_computed_distance_m, in_request_ip,
                  created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (employee_id, None, start, stored_out, verdict,
             "inside" if closed else None,
             24.7136, 46.6753, 11.0, 42.0, "203.0.113.9", start))
        conn.commit()
        return cur.lastrowid, start, end
    finally:
        conn.close()


# ========================= حدّ الثقة =========================

def test_request_model_has_only_three_fields():
    """لا site_id ولا inside ولا distance — المخطّط يُسقطها."""
    import api
    fields = set(api.Reading.model_fields)
    assert fields == {"latitude", "longitude", "accuracy_m"}


def test_claimed_verdict_in_the_body_is_ignored(api_client, employee_h, site):
    """ادّعاء العميل لا يُقرأ: الخادم يحسب ويرفض."""
    r = api_client.post("/attendance/check-in", headers=employee_h, json={
        **JEDDAH, "inside": True, "distance_m": 0, "site_id": site["site_id"],
        "verdict": "inside", "employee_id": "EMP-999"})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is False
    assert body["verdict"] == geofence.VERDICT_OUTSIDE
    assert body["site_id"] is None


def test_forged_employee_id_does_not_change_the_owner(api_client, employee_h, site,
                                                      attendance_db):
    api_client.post("/attendance/check-in", headers=employee_h,
                    json={**RIYADH, "employee_id": "EMP-001"})
    rows = att.records_for(None, path=attendance_db)
    assert [r["employee_id"] for r in rows] == ["EMP-003"]


def test_server_computes_the_distance(api_client, employee_h, site):
    r = api_client.post("/attendance/check-in", headers=employee_h, json=RIYADH)
    assert r.json()["distance_m"] is not None


# ========================= الحضور والانصراف =========================

def test_check_in_inside_the_fence(api_client, employee_h, site):
    r = api_client.post("/attendance/check-in", headers=employee_h, json=RIYADH)
    assert r.status_code == 200 and r.json()["accepted"] is True
    no_leak(r)


def test_check_in_outside_is_recorded_not_accepted(api_client, employee_h, site):
    r = api_client.post("/attendance/check-in", headers=employee_h, json=JEDDAH)
    assert r.json() == {**r.json(), "recorded": True, "accepted": False}


def test_coarse_reading_is_rejected_not_outside(api_client, employee_h, site):
    r = api_client.post("/attendance/check-in", headers=employee_h,
                        json={**RIYADH, "accuracy_m": 5000})
    assert r.json()["verdict"] == geofence.VERDICT_REJECTED


def test_double_check_in_is_refused(api_client, employee_h, site):
    api_client.post("/attendance/check-in", headers=employee_h, json=RIYADH)
    r = api_client.post("/attendance/check-in", headers=employee_h, json=RIYADH)
    assert r.json()["recorded"] is False


def test_check_out_without_check_in(api_client, employee_h, site):
    r = api_client.post("/attendance/check-out", headers=employee_h, json=RIYADH)
    assert r.json()["recorded"] is False
    assert r.json()["reason"] == att.NO_OPEN_SESSION


def test_check_out_outside_still_closes(api_client, employee_h, site):
    api_client.post("/attendance/check-in", headers=employee_h, json=RIYADH)
    r = api_client.post("/attendance/check-out", headers=employee_h, json=JEDDAH)
    assert r.json()["recorded"] is True


@pytest.mark.parametrize("bad", [
    {"latitude": 999, "longitude": 46.0, "accuracy_m": 10},
    {"latitude": 24.7, "longitude": 999, "accuracy_m": 10},
    {"latitude": 24.7, "longitude": 46.0, "accuracy_m": -5},
])
def test_malformed_coordinates_have_a_defined_outcome(api_client, employee_h, site, bad):
    r = api_client.post("/attendance/check-in", headers=employee_h, json=bad)
    assert r.status_code in (200, 422)
    no_leak(r)


def test_missing_field_is_a_validation_error(api_client, employee_h):
    r = api_client.post("/attendance/check-in", headers=employee_h,
                        json={"latitude": 24.7})
    assert r.status_code == 422
    no_leak(r)


# ========================= /me/attendance =========================

def test_employee_sees_only_their_own(api_client, employee_h, attendance_db, site):
    seed_session(attendance_db, "EMP-003")
    seed_session(attendance_db, "EMP-001")
    r = api_client.get("/me/attendance", headers=employee_h)
    assert {x["record_id"] for x in r.json()["records"]} == {
        x["id"] for x in att.records_for(["EMP-003"], path=attendance_db)}


def test_employee_sees_their_own_coordinates(api_client, employee_h, attendance_db, site):
    """الطرف الآخر: الحجب عن الجميع ليس نجاحاً."""
    seed_session(attendance_db, "EMP-003")
    rec = api_client.get("/me/attendance", headers=employee_h).json()["records"][0]
    assert rec["in_reported_lat"] == 24.7136
    assert rec["in_reported_accuracy_m"] == 11.0
    assert rec["in_computed_distance_m"] == 42.0


def test_open_session_is_in_the_same_response(api_client, employee_h, site):
    api_client.post("/attendance/check-in", headers=employee_h, json=RIYADH)
    body = api_client.get("/me/attendance", headers=employee_h).json()
    assert body["open_session"] is not None
    assert body["open_session"]["check_out_at"] is None


def test_no_open_session_after_check_out(api_client, employee_h, site):
    api_client.post("/attendance/check-in", headers=employee_h, json=RIYADH)
    api_client.post("/attendance/check-out", headers=employee_h, json=RIYADH)
    assert api_client.get("/me/attendance", headers=employee_h).json()["open_session"] is None


def test_totals_separate_verified_from_administrative(api_client, employee_h,
                                                      attendance_db, site):
    seed_session(attendance_db, "EMP-003")
    t = api_client.get("/me/attendance", headers=employee_h).json()["totals"]
    assert t["verified_minutes"] > 0
    assert t["administrative_minutes"] == 0
    assert t["total_minutes"] == t["verified_minutes"]


def test_conflict_closed_contributes_no_minutes(api_client, employee_h,
                                                attendance_db, site):
    rid, _, _ = seed_session(attendance_db, "EMP-003")
    conn = sqlite3.connect(attendance_db)
    conn.execute("UPDATE attendance_records SET out_verdict=? WHERE id=?",
                 (att.VERDICT_CONFLICT_CLOSED, rid))
    conn.commit(); conn.close()

    body = api_client.get("/me/attendance", headers=employee_h).json()
    assert body["totals"]["verified_minutes"] == 0
    assert body["totals"]["administrative_minutes"] == 0
    assert body["totals"]["unresolved_sessions"] == 1
    assert body["records"][0]["duration_minutes"] is None
    assert body["records"][0]["counts_toward_work"] is False


# ========================= /team/attendance =========================

def test_employee_cannot_read_the_team(api_client, employee_h):
    r = api_client.get("/team/attendance", headers=employee_h)
    assert r.status_code == 403
    no_leak(r)


def test_manager_sees_the_subtree(api_client, manager_h, attendance_db, site):
    seed_session(attendance_db, "EMP-003")
    seed_session(attendance_db, "EMP-001")
    body = api_client.get("/team/attendance", headers=manager_h).json()
    assert {x["employee_id"] for x in body["records"]} == {"EMP-003", "EMP-001"}
    assert body["scope"] == "team"


def test_manager_does_not_see_outside_the_subtree(api_client, manager_h,
                                                  attendance_db, site):
    seed_session(attendance_db, "EMP-777")
    body = api_client.get("/team/attendance", headers=manager_h).json()
    assert "EMP-777" not in {x["employee_id"] for x in body["records"]}


def test_admin_sees_the_organization(api_client, admin_h, attendance_db, site):
    seed_session(attendance_db, "EMP-777")
    body = api_client.get("/team/attendance", headers=admin_h).json()
    assert body["scope"] == "org"
    assert "EMP-777" in {x["employee_id"] for x in body["records"]}


def test_manager_never_receives_coordinates(api_client, manager_h,
                                            attendance_db, site):
    seed_session(attendance_db, "EMP-003")
    r = api_client.get("/team/attendance", headers=manager_h)
    no_private(r)
    no_leak(r)


def test_admin_never_receives_coordinates(api_client, admin_h, attendance_db, site):
    seed_session(attendance_db, "EMP-003")
    r = api_client.get("/team/attendance", headers=admin_h)
    no_private(r)


def test_the_coordinate_values_themselves_do_not_appear(api_client, manager_h,
                                                        attendance_db, site):
    """قيمة لا مفتاحاً — التسريب قد يأتي في نصّ لا في حقل."""
    seed_session(attendance_db, "EMP-003")
    body = api_client.get("/team/attendance", headers=manager_h).text
    for value in ("24.7136", "46.6753", "203.0.113.9", "42.0"):
        assert value not in body, f"قيمة خاصّة {value} ظهرت"


def test_team_names_are_resolved(api_client, manager_h, attendance_db, site):
    seed_session(attendance_db, "EMP-003")
    rec = api_client.get("/team/attendance", headers=manager_h).json()["records"][0]
    assert rec["employee_name"] == "خالد الدوسري"


# ========================= التصفية الزمنية =========================

def test_bare_date_is_rejected(api_client, employee_h):
    r = api_client.get("/me/attendance?start=2026-10-12", headers=employee_h)
    assert r.status_code == 400
    no_leak(r)


def test_timestamp_without_offset_is_rejected(api_client, employee_h):
    r = api_client.get("/me/attendance?start=2026-10-12T00:00:00", headers=employee_h)
    assert r.status_code == 400


def test_window_is_half_open(api_client, employee_h, attendance_db, site):
    _, start, _ = seed_session(attendance_db, "EMP-003")
    # params لا f-string: علامة + في الإزاحة تُفكّ مسافةً في مسار الاستعلام
    inclusive = api_client.get("/me/attendance", headers=employee_h,
                               params={"start": start})
    assert inclusive.json()["count"] == 1, "الحدّ الأدنى شامل"
    exclusive = api_client.get("/me/attendance", headers=employee_h,
                               params={"end": start})
    assert exclusive.json()["count"] == 0, "الحدّ الأعلى غير شامل"


def test_offsets_are_compared_in_utc(api_client, employee_h, attendance_db, site):
    seed_session(attendance_db, "EMP-003", hours_ago=9)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    riyadh_bound = (now - timedelta(hours=10)).astimezone(
        timezone(timedelta(hours=3))).isoformat()
    r = api_client.get("/me/attendance", headers=employee_h,
                       params={"start": riyadh_bound})
    assert r.json()["count"] == 1


# ========================= المواقع =========================

def test_everyone_sees_active_sites(api_client, employee_h, site):
    body = api_client.get("/sites", headers=employee_h).json()
    assert site["site_id"] in {s["site_id"] for s in body["sites"]}


def test_employee_cannot_create_a_site(api_client, employee_h):
    r = api_client.post("/sites", headers=employee_h,
                        json={"name": "موقع", "latitude": 24.7, "longitude": 46.6})
    assert r.status_code == 403


def test_manager_cannot_create_a_site(api_client, manager_h):
    r = api_client.post("/sites", headers=manager_h,
                        json={"name": "موقع", "latitude": 24.7, "longitude": 46.6})
    assert r.status_code == 403


def test_admin_creates_edits_and_deactivates(api_client, admin_h):
    created = api_client.post("/sites", headers=admin_h, json={
        "name": "موقع المشروع", "latitude": 24.8, "longitude": 46.7, "radius_m": 200})
    assert created.status_code == 201
    sid = created.json()["site_id"]

    patched = api_client.patch(f"/sites/{sid}", headers=admin_h, json={"radius_m": 400})
    assert patched.json()["radius_m"] == 400
    assert patched.json()["name"] == "موقع المشروع", "ما لم يُرسل لا يتغيّر"

    off = api_client.post(f"/sites/{sid}/deactivate", headers=admin_h)
    assert off.json()["is_active"] is False


def test_admin_sees_inactive_sites_and_others_do_not(api_client, admin_h, employee_h):
    created = api_client.post("/sites", headers=admin_h, json={
        "name": "موقع مغلق", "latitude": 25.0, "longitude": 47.0})
    sid = created.json()["site_id"]
    api_client.post(f"/sites/{sid}/deactivate", headers=admin_h)

    assert sid in {s["site_id"] for s in
                   api_client.get("/sites", headers=admin_h).json()["sites"]}
    assert sid not in {s["site_id"] for s in
                       api_client.get("/sites", headers=employee_h).json()["sites"]}


def test_bad_site_coordinates_are_rejected(api_client, admin_h):
    r = api_client.post("/sites", headers=admin_h,
                        json={"name": "سيئ", "latitude": 999, "longitude": 46.6})
    assert r.status_code == 400
    no_leak(r)


def test_unknown_site_patch_is_404(api_client, admin_h):
    r = api_client.patch("/sites/SITE-NOPE", headers=admin_h, json={"radius_m": 200})
    assert r.status_code == 404


# ========================= طلبات التصحيح =========================

def test_correction_on_someone_elses_record_is_indistinguishable_from_missing(
        api_client, employee_h, attendance_db, site):
    """غير موجود وخارج الملكية: الرمز والنصّ نفسهما حرفياً."""
    rid, _, _ = seed_session(attendance_db, "EMP-001")
    theirs = api_client.post("/attendance/corrections", headers=employee_h,
                             json={"kind": "wrong_time", "record_id": rid})
    missing = api_client.post("/attendance/corrections", headers=employee_h,
                              json={"kind": "wrong_time", "record_id": 999999})
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json()["detail"] == missing.json()["detail"]


def test_employee_can_file_a_correction(api_client, employee_h, attendance_db, site):
    rid, start, end = seed_session(attendance_db, "EMP-003")
    r = api_client.post("/attendance/corrections", headers=employee_h, json={
        "kind": "wrong_time", "record_id": rid, "reason": "الوقت خاطئ",
        "requested_check_out_at": end})
    assert r.status_code == 200 and r.json()["created"] is True


def test_employee_cannot_see_the_pending_queue(api_client, employee_h):
    r = api_client.get("/attendance/corrections/pending", headers=employee_h)
    assert r.status_code == 403


def test_manager_sees_only_their_subtree_queue(api_client, manager_h,
                                               attendance_db, site):
    att.create_correction("EMP-003", "other", reason="ضمن الفريق", path=attendance_db)
    att.create_correction("EMP-777", "other", reason="خارج الفريق", path=attendance_db)
    body = api_client.get("/attendance/corrections/pending", headers=manager_h).json()
    assert {c["employee_id"] for c in body["corrections"]} == {"EMP-003"}


def test_manager_approves_a_correction(api_client, manager_h, attendance_db, site):
    rid, start, end = seed_session(attendance_db, "EMP-003", closed=False)
    cid = att.create_correction("EMP-003", "missing_check_out", record_id=rid,
                                requested_check_out_at=end,
                                path=attendance_db)["correction_id"]
    r = api_client.post(f"/attendance/corrections/{cid}/review", headers=manager_h,
                        json={"decision": "approve", "note": "مقبول"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert att.record_by_id(rid, path=attendance_db)["check_out_at"] == end


def test_self_review_is_forbidden(api_client, manager_h, attendance_db, site):
    cid = att.create_correction("EMP-002", "other", reason="طلب المديرة",
                                path=attendance_db)["correction_id"]
    r = api_client.post(f"/attendance/corrections/{cid}/review", headers=manager_h,
                        json={"decision": "approve"})
    assert r.status_code == 403
    assert att.correction_by_id(cid, path=attendance_db)["status"] == "pending"


def test_out_of_scope_review_is_indistinguishable_from_missing(
        api_client, manager_h, attendance_db, site):
    cid = att.create_correction("EMP-777", "other", path=attendance_db)["correction_id"]
    out = api_client.post(f"/attendance/corrections/{cid}/review", headers=manager_h,
                          json={"decision": "approve"})
    missing = api_client.post("/attendance/corrections/999999/review",
                              headers=manager_h, json={"decision": "approve"})
    assert out.status_code == missing.status_code == 404
    assert out.json()["detail"] == missing.json()["detail"]


def test_reviewing_twice_is_409(api_client, manager_h, attendance_db, site):
    cid = att.create_correction("EMP-003", "other", path=attendance_db)["correction_id"]
    api_client.post(f"/attendance/corrections/{cid}/review", headers=manager_h,
                    json={"decision": "approve"})
    again = api_client.post(f"/attendance/corrections/{cid}/review", headers=manager_h,
                            json={"decision": "reject"})
    assert again.status_code == 409


def test_employee_cannot_review(api_client, employee_h, attendance_db, site):
    cid = att.create_correction("EMP-003", "other", path=attendance_db)["correction_id"]
    r = api_client.post(f"/attendance/corrections/{cid}/review", headers=employee_h,
                        json={"decision": "approve"})
    assert r.status_code == 403


def test_rejected_location_keeps_the_geofence_verdict(api_client, manager_h,
                                                      attendance_db, site):
    """القبول الإداري لا يقلب حكم السياج."""
    rid, start, end = seed_session(attendance_db, "EMP-003", closed=False,
                                   verdict="outside")
    cid = att.create_correction("EMP-003", "rejected_location", record_id=rid,
                                requested_check_out_at=end,
                                path=attendance_db)["correction_id"]
    api_client.post(f"/attendance/corrections/{cid}/review", headers=manager_h,
                    json={"decision": "approve"})

    rec = att.record_by_id(rid, path=attendance_db)
    assert rec["in_verdict"] == "outside", "الحكم المقيس لم يتغيّر"
    assert rec["out_verdict"] == att.VERDICT_CORRECTION_CLOSED
    assert rec["corrected_by_correction_id"] == cid


def test_the_original_time_survives_a_second_correction(api_client, manager_h,
                                                        attendance_db, site):
    """تصحيحان متتاليان: الأصل يُحفظ مرّةً ولا يُدهس بالقيمة المصحَّحة.

    بلا شرط IS NULL تصير original_check_in_at نسخةً من آخر تصحيح،
    فيضيع ما كان السجل يقوله أصلاً — وهو بيان التدقيق كلّه.
    """
    rid, start, end = seed_session(attendance_db, "EMP-003", hours_ago=9)
    first = (datetime.fromisoformat(start) + timedelta(minutes=10)).isoformat()
    second = (datetime.fromisoformat(start) + timedelta(minutes=20)).isoformat()

    for wanted in (first, second):
        cid = att.create_correction("EMP-003", "wrong_time", record_id=rid,
                                    requested_check_in_at=wanted,
                                    requested_check_out_at=end,
                                    path=attendance_db)["correction_id"]
        r = api_client.post(f"/attendance/corrections/{cid}/review",
                            headers=manager_h, json={"decision": "approve"})
        assert r.status_code == 200, r.text

    rec = att.record_by_id(rid, path=attendance_db)
    assert rec["check_in_at"] == second, "التصحيح الثاني طُبّق"
    assert rec["original_check_in_at"] == start, \
        "الأصل هو ما كان قبل أول تصحيح، لا قبل آخر واحد"


def test_admin_correction_has_no_reviewer(api_client, admin_h, attendance_db, site):
    """قيد موثَّق: لا باب خلفي ولا مراجعة ذاتية."""
    cid = att.create_correction("EMP-100", "other", reason="طلب المسؤول",
                                path=attendance_db)["correction_id"]
    r = api_client.post(f"/attendance/corrections/{cid}/review", headers=admin_h,
                        json={"decision": "approve"})
    assert r.status_code == 403
    assert att.correction_by_id(cid, path=attendance_db)["status"] == "pending"


# ========================= حدّ الطلبات =========================

def test_twenty_pass_then_the_twenty_first_is_throttled(api_client, employee_h, site):
    import ratelimit
    ratelimit.attendance_limiter.reset()
    for i in range(20):
        r = api_client.post("/attendance/check-in", headers=employee_h, json=JEDDAH)
        assert r.status_code != 429, f"الطلب {i + 1} حُدّ قبل أوانه"
    r = api_client.post("/attendance/check-in", headers=employee_h, json=JEDDAH)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    no_leak(r)


def test_the_counter_is_keyed_to_the_employee_not_the_network(
        api_client, employee_h, other_h, site):
    """موقع بناء خلف IP واحد: كلٌّ عدّاده."""
    import ratelimit
    ratelimit.attendance_limiter.reset()
    for _ in range(20):
        api_client.post("/attendance/check-in", headers=employee_h, json=JEDDAH)
    assert api_client.post("/attendance/check-in", headers=employee_h,
                           json=JEDDAH).status_code == 429
    assert api_client.post("/attendance/check-in", headers=other_h,
                           json=JEDDAH).status_code != 429


def test_reading_endpoints_are_not_throttled(api_client, employee_h, site):
    import ratelimit
    ratelimit.attendance_limiter.reset()
    for _ in range(25):
        assert api_client.get("/me/attendance", headers=employee_h).status_code == 200


# ========================= المصادقة والتسريب =========================

@pytest.mark.parametrize("method,path", [
    ("post", "/attendance/check-in"), ("post", "/attendance/check-out"),
    ("get", "/me/attendance"), ("get", "/team/attendance"),
    ("get", "/sites"), ("post", "/sites"),
    ("post", "/attendance/corrections"),
    ("get", "/attendance/corrections/pending"),
])
def test_every_endpoint_requires_authentication(api_client, method, path):
    r = (api_client.post(path, json={}) if method == "post"
         else api_client.get(path))
    assert r.status_code in (401, 403)
    no_leak(r)


def test_no_attendance_write_tool_reaches_the_agent():
    """الكتابة تبقى في HTTP وحدها.

    كان هذا الاختبار في الطبقة 2 يمنع **أي** أداة حضور، وقد أدّى
    غرضه: ثبّت حدّ تلك الطبقة. والطبقة 3 أضافت ثلاث أدوات قراءة
    بقرار موثَّق، فنقلت الحدّ شرعياً — وهذي صيغته الدائمة.

    ما لا يتغيّر: لا أداة وكيل تكتب حضوراً. تسجيل الحضور عبر
    النموذج يعني أن الإحداثيات ادّعاء نصّي لا قراءة جهاز.
    تفصيله في tests/test_attendance_agent_tools.py.
    """
    import agent
    names = {t["function"]["name"] for t in agent.TOOL_SPECS}
    assert "check_in" not in names and "check_out" not in names
    assert agent.WRITE_TOOLS == {"submit_leave_request", "cancel_leave_request",
                                 "review_leave_request"}


def test_responses_carry_no_secrets_on_any_path(api_client, employee_h, manager_h,
                                                admin_h, attendance_db, site):
    seed_session(attendance_db, "EMP-003")
    for headers in (employee_h, manager_h, admin_h):
        for path in ("/me/attendance", "/team/attendance", "/sites",
                     "/attendance/corrections/pending"):
            no_leak(api_client.get(path, headers=headers))


def test_error_bodies_are_json_without_internals(api_client, employee_h):
    for r in (api_client.get("/team/attendance", headers=employee_h),
              api_client.get("/me/attendance?start=bad", headers=employee_h),
              api_client.post("/attendance/corrections", headers=employee_h,
                              json={"kind": "nope"})):
        assert r.headers["content-type"].startswith("application/json")
        assert set(json.loads(r.text)) <= {"detail"}
        no_leak(r)
