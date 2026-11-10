"""طبقة بيانات الحضور — المواقع والسجلات وطلبات التصحيح.

حدّ الثقة هو محور هذا الملف: نثبت أن الخادم يحسب الحكم ولا
يستقبله، وأن ما ادّعاه الجهاز وما استنتجه الخادم يُحفظان منفصلين.
"""

import inspect
import math
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import attendance as att
import build_db
import geofence
import migrations

RIYADH = (24.7136, 46.6753)
JEDDAH = (21.4858, 39.1925)


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    path = tmp_path / "att.db"
    build_db.build(path)
    migrations.migrate(path)
    monkeypatch.setattr(att, "DB_PATH", path)
    return path


@pytest.fixture
def hq(db):
    return att.create_site("المقرّ الرئيسي", *RIYADH, radius_m=150, path=db)


def valid_times():
    """وقتا حضور وانصراف صالحان في الماضي القريب، بإزاحة صريحة."""
    end = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    start = end - timedelta(hours=8)
    return start.isoformat(), end.isoformat()


def far_point(metres, origin=RIYADH):
    """نقطة تبعد المسافة المطلوبة شمالاً."""
    delta = metres / (math.pi / 180 * geofence.EARTH_RADIUS_M)
    return (origin[0] + delta, origin[1])


# ========================= المواقع =========================

def test_create_site_round_trips(db):
    s = att.create_site("موقع المشروع", *RIYADH, radius_m=300, path=db)
    assert s["name"] == "موقع المشروع"
    assert s["radius_m"] == 300
    assert s["is_active"] == 1
    assert s["site_id"].startswith("SITE-")


def test_site_ids_are_unique(db):
    a = att.create_site("موقع أ", *RIYADH, path=db)
    b = att.create_site("موقع ب", *JEDDAH, path=db)
    assert a["site_id"] != b["site_id"]


@pytest.mark.parametrize("lat,lon", [(999, 46.0), (24.0, 999), ("شمال", 46.0), (None, 46.0)])
def test_create_site_rejects_bad_coordinates(db, lat, lon):
    """موقع بإحداثيات فاسدة أخطر من غياب الموقع — السياج يُحسب عليه."""
    with pytest.raises(geofence.GeofenceError):
        att.create_site("سيئ", lat, lon, path=db)


@pytest.mark.parametrize("r", [0, -5, 1, 100_000])
def test_create_site_rejects_bad_radius(db, r):
    with pytest.raises(geofence.GeofenceError):
        att.create_site("سيئ", *RIYADH, radius_m=r, path=db)


def test_create_site_rejects_empty_name(db):
    with pytest.raises(geofence.GeofenceError):
        att.create_site("", *RIYADH, path=db)


def test_deactivated_site_hidden_from_active_list(db, hq):
    att.set_site_active(hq["site_id"], False, path=db)
    assert att.sites(active_only=True, path=db) == []
    assert len(att.sites(active_only=False, path=db)) == 1


def test_deactivate_does_not_delete(db, hq):
    """السجلات التاريخية تشير إليه — الحذف يُفقد معناها."""
    att.set_site_active(hq["site_id"], False, path=db)
    assert att.site_by_id(hq["site_id"], path=db) is not None


def test_update_site_keeps_untouched_fields(db, hq):
    s = att.update_site(hq["site_id"], radius_m=500, path=db)
    assert s["radius_m"] == 500
    assert s["name"] == hq["name"]
    assert s["latitude"] == hq["latitude"]


def test_update_site_rejects_bad_values(db, hq):
    with pytest.raises(geofence.GeofenceError):
        att.update_site(hq["site_id"], latitude=999, path=db)
    assert att.site_by_id(hq["site_id"], path=db)["latitude"] == hq["latitude"]


def test_update_unknown_site_returns_none(db):
    assert att.update_site("SITE-NOPE", name="x", path=db) is None
    assert att.set_site_active("SITE-NOPE", False, path=db) is None


# ===================== حدّ الثقة =====================

def test_check_in_takes_no_verdict_argument():
    """لا مسار يلتفّ على الحساب: لا معامل verdict ولا inside ولا site_id."""
    params = set(inspect.signature(att.check_in).parameters)
    assert not params & {"verdict", "inside", "site_id", "distance_m", "is_inside"}


def test_check_out_takes_no_verdict_argument():
    params = set(inspect.signature(att.check_out).parameters)
    assert not params & {"verdict", "inside", "site_id", "distance_m"}


def test_server_computes_the_distance_itself(db, hq):
    """الخادم يحسب، ولا يقرأ مسافة من العميل."""
    point = far_point(80)
    r = att.check_in("EMP-001", *point, 10, path=db)
    assert r["distance_m"] == pytest.approx(80, abs=2)


def test_reported_and_computed_are_stored_separately(db, hq):
    """عمود لما ادّعاه الجهاز، وعمود لما استنتجه الخادم."""
    point = far_point(50)
    att.check_in("EMP-001", *point, 12.5, path=db)
    row = att.records_for(["EMP-001"], path=db)[0]

    assert row["in_reported_lat"] == pytest.approx(point[0])
    assert row["in_reported_accuracy_m"] == 12.5
    assert row["in_computed_distance_m"] == pytest.approx(50, abs=2)
    assert row["in_verdict"] == geofence.VERDICT_INSIDE


def test_request_ip_is_stored(db, hq):
    att.check_in("EMP-001", *RIYADH, 10, request_ip="203.0.113.7", path=db)
    assert att.records_for(["EMP-001"], path=db)[0]["in_request_ip"] == "203.0.113.7"


# ===================== تسجيل الحضور =====================

def test_check_in_inside_the_fence_is_accepted(db, hq):
    r = att.check_in("EMP-001", *RIYADH, 10, path=db)
    assert r["recorded"] and r["accepted"]
    assert r["site_id"] == hq["site_id"]
    assert att.open_session("EMP-001", path=db) is not None


def test_check_in_outside_is_recorded_but_not_accepted(db, hq):
    """نحفظ المحاولة المرفوضة: الموظف يحتاج شيئاً يقدّم عليه تصحيحاً."""
    r = att.check_in("EMP-001", *JEDDAH, 10, path=db)
    assert r["recorded"] and not r["accepted"]
    assert r["verdict"] == geofence.VERDICT_OUTSIDE
    assert len(att.records_for(["EMP-001"], path=db)) == 1


def test_rejected_attempt_opens_no_session(db, hq):
    att.check_in("EMP-001", *JEDDAH, 10, path=db)
    assert att.open_session("EMP-001", path=db) is None


def test_rejected_attempt_stores_no_site(db, hq):
    """ربط سجل مرفوض بموقع يجعله يبدو حضوراً فيه."""
    att.check_in("EMP-001", *JEDDAH, 10, path=db)
    assert att.records_for(["EMP-001"], path=db)[0]["site_id"] is None


def test_coarse_reading_is_rejected_not_outside(db, hq):
    r = att.check_in("EMP-001", *RIYADH, 5000, path=db)
    assert r["verdict"] == geofence.VERDICT_REJECTED
    assert not r["accepted"]


def test_double_check_in_is_refused(db, hq):
    att.check_in("EMP-001", *RIYADH, 10, path=db)
    r = att.check_in("EMP-001", *RIYADH, 10, path=db)
    assert not r["recorded"]
    assert "مفتوح" in r["reason"]
    assert len(att.records_for(["EMP-001"], path=db)) == 1


def test_rejected_attempt_does_not_block_a_later_valid_one(db, hq):
    att.check_in("EMP-001", *JEDDAH, 10, path=db)
    r = att.check_in("EMP-001", *RIYADH, 10, path=db)
    assert r["accepted"]


def test_check_in_without_identity_is_refused(db, hq):
    for bad in ("", "   ", None):
        assert not att.check_in(bad, *RIYADH, 10, path=db)["recorded"]


def test_check_in_with_no_sites_configured(db):
    r = att.check_in("EMP-001", *RIYADH, 10, path=db)
    assert r["recorded"] and not r["accepted"]
    assert "لا توجد مواقع" in r["reason"]


def test_inactive_site_does_not_accept_check_in(db, hq):
    att.set_site_active(hq["site_id"], False, path=db)
    r = att.check_in("EMP-001", *RIYADH, 10, path=db)
    assert not r["accepted"]


# ===================== الانصراف =====================

def test_check_out_closes_the_session(db, hq):
    att.check_in("EMP-001", *RIYADH, 10, path=db)
    r = att.check_out("EMP-001", *RIYADH, 10, path=db)
    assert r["recorded"]
    assert att.open_session("EMP-001", path=db) is None


def test_check_out_without_check_in_is_refused(db, hq):
    r = att.check_out("EMP-001", *RIYADH, 10, path=db)
    assert not r["recorded"]
    assert "لا يوجد حضور مفتوح" in r["reason"]


def test_check_out_outside_the_fence_still_closes(db, hq):
    """منع الانصراف لضعف الإشارة يترك الموظف «حاضراً» إلى الأبد."""
    att.check_in("EMP-001", *RIYADH, 10, path=db)
    r = att.check_out("EMP-001", *JEDDAH, 10, path=db)
    assert r["recorded"]
    assert r["verdict"] == geofence.VERDICT_OUTSIDE
    assert att.open_session("EMP-001", path=db) is None


def test_check_out_records_its_own_verdict(db, hq):
    att.check_in("EMP-001", *RIYADH, 10, path=db)
    att.check_out("EMP-001", *JEDDAH, 20, path=db)
    row = att.records_for(["EMP-001"], path=db)[0]
    assert row["in_verdict"] == geofence.VERDICT_INSIDE
    assert row["out_verdict"] == geofence.VERDICT_OUTSIDE
    assert row["out_reported_accuracy_m"] == 20


def test_double_check_out_is_refused(db, hq):
    att.check_in("EMP-001", *RIYADH, 10, path=db)
    att.check_out("EMP-001", *RIYADH, 10, path=db)
    assert not att.check_out("EMP-001", *RIYADH, 10, path=db)["recorded"]


def test_check_in_at_one_site_and_out_at_another(db, hq):
    """واقعة حقيقية في العمل الميداني — تُسجَّل ولا تُسقط الجلسة."""
    other = att.create_site("موقع ثانٍ", *JEDDAH, radius_m=200, path=db)
    att.check_in("EMP-001", *RIYADH, 10, path=db)
    r = att.check_out("EMP-001", *JEDDAH, 10, path=db)
    row = att.records_for(["EMP-001"], path=db)[0]
    assert r["recorded"]
    assert row["site_id"] == hq["site_id"]              # موقع الحضور لا الانصراف
    assert row["out_verdict"] == geofence.VERDICT_INSIDE
    assert other["site_id"] != hq["site_id"]


def test_sessions_are_per_employee(db, hq):
    att.check_in("EMP-001", *RIYADH, 10, path=db)
    r = att.check_in("EMP-002", *RIYADH, 10, path=db)
    assert r["accepted"]
    assert att.open_session("EMP-002", path=db)["employee_id"] == "EMP-002"


# ===================== الاستعلام =====================

def test_records_scoped_to_the_given_ids(db, hq):
    att.check_in("EMP-001", *RIYADH, 10, path=db)
    att.check_in("EMP-002", *RIYADH, 10, path=db)
    rows = att.records_for(["EMP-001"], path=db)
    assert {r["employee_id"] for r in rows} == {"EMP-001"}


def test_empty_id_list_returns_nothing(db, hq):
    """القائمة الفارغة تعني «لا أحد» — لا «الجميع»."""
    att.check_in("EMP-001", *RIYADH, 10, path=db)
    assert att.records_for([], path=db) == []


def test_none_means_unrestricted(db, hq):
    att.check_in("EMP-001", *RIYADH, 10, path=db)
    att.check_in("EMP-002", *RIYADH, 10, path=db)
    assert len(att.records_for(None, path=db)) == 2


def test_records_are_newest_first(db, hq):
    att.check_in("EMP-001", *RIYADH, 10, path=db)
    att.check_out("EMP-001", *RIYADH, 10, path=db)
    att.check_in("EMP-001", *RIYADH, 10, path=db)
    rows = att.records_for(["EMP-001"], path=db)
    assert rows[0]["check_in_at"] >= rows[1]["check_in_at"]


def test_record_by_id_tolerates_junk(db):
    for junk in (None, "abc", [], 999999):
        assert att.record_by_id(junk, path=db) is None


# ===================== طلبات التصحيح =====================

def test_correction_is_created(db, hq):
    rid = att.check_in("EMP-001", *JEDDAH, 10, path=db)["record_id"]
    r = att.create_correction("EMP-001", "rejected_location",
                              "كنت في الموقع", "الإشارة ضعيفة", rid, path=db)
    assert r["created"] and r["status"] == att.STATUS_PENDING


def test_correction_on_someone_elses_record_is_refused(db, hq):
    """ملكية لا صلاحية: لا معنى لتصحيح سجل غيرك مهما كان دورك."""
    rid = att.check_in("EMP-002", *RIYADH, 10, path=db)["record_id"]
    r = att.create_correction("EMP-001", "wrong_time", record_id=rid, path=db)
    assert not r["created"]
    assert "لا يخصّك" in r["reason"]


def test_correction_with_unknown_record_is_refused(db):
    r = att.create_correction("EMP-001", "wrong_time", record_id=99999, path=db)
    assert not r["created"]


def test_correction_without_record_is_allowed(db):
    """حضور لم يُسجَّل أصلاً لا سجل له — وهو أكثر ما يُطلب تصحيحه."""
    assert att.create_correction("EMP-001", "missing_check_in",
                                 "حضرت 7:00", "نسيت التسجيل", path=db)["created"]


def test_unknown_correction_kind_is_refused(db):
    assert not att.create_correction("EMP-001", "hack", path=db)["created"]


def test_correction_without_identity_is_refused(db):
    assert not att.create_correction("", "wrong_time", path=db)["created"]


def test_manager_can_review_an_employees_correction(db):
    a, b = valid_times()
    cid = att.create_correction("EMP-001", "missing_check_in",
                                requested_check_in_at=a, requested_check_out_at=b,
                                path=db)["correction_id"]
    r = att.review_correction(cid, "approve", "EMP-900", "مقبول", path=db)
    assert r["reviewed"] and r["status"] == att.STATUS_APPROVED


def test_self_review_is_refused(db):
    """فصل المهام — نفس قاعدة الإجازات، ويُفحص قبل أي كتابة."""
    cid = att.create_correction("EMP-001", "missing_check_in", path=db)["correction_id"]
    r = att.review_correction(cid, "approve", "EMP-001", path=db)
    assert not r["reviewed"]
    assert att.correction_by_id(cid, path=db)["status"] == att.STATUS_PENDING


def test_self_review_leaves_no_trace_of_a_decision(db):
    cid = att.create_correction("EMP-001", "missing_check_in", path=db)["correction_id"]
    att.review_correction(cid, "approve", "EMP-001", path=db)
    row = att.correction_by_id(cid, path=db)
    assert row["reviewed_by"] is None and row["decided_at"] is None


def test_review_without_reviewer_identity_is_refused(db):
    cid = att.create_correction("EMP-001", "missing_check_in", path=db)["correction_id"]
    assert not att.review_correction(cid, "approve", "", path=db)["reviewed"]


def test_review_twice_is_refused(db):
    a, b = valid_times()
    cid = att.create_correction("EMP-001", "missing_check_in",
                                requested_check_in_at=a, requested_check_out_at=b,
                                path=db)["correction_id"]
    att.review_correction(cid, "approve", "EMP-900", path=db)
    r = att.review_correction(cid, "reject", "EMP-900", path=db)
    assert not r["reviewed"]
    assert att.correction_by_id(cid, path=db)["status"] == att.STATUS_APPROVED


def test_invalid_decision_is_refused(db):
    cid = att.create_correction("EMP-001", "missing_check_in", path=db)["correction_id"]
    for bad in ("maybe", "", None, "APPROVE!"):
        assert not att.review_correction(cid, bad, "EMP-900", path=db)["reviewed"]


def test_review_of_unknown_correction_is_refused(db):
    assert not att.review_correction(99999, "approve", "EMP-900", path=db)["reviewed"]


def test_pending_list_is_scoped(db):
    att.create_correction("EMP-001", "missing_check_in", path=db)
    att.create_correction("EMP-002", "missing_check_in", path=db)
    assert {c["employee_id"] for c in att.pending_corrections(["EMP-001"], path=db)} \
        == {"EMP-001"}


def test_pending_list_empty_scope_returns_nothing(db):
    att.create_correction("EMP-001", "missing_check_in", path=db)
    assert att.pending_corrections([], path=db) == []


def test_pending_list_excludes_decided(db):
    a, b = valid_times()
    cid = att.create_correction("EMP-001", "missing_check_in",
                                requested_check_in_at=a, requested_check_out_at=b,
                                path=db)["correction_id"]
    att.review_correction(cid, "approve", "EMP-900", path=db)
    assert att.pending_corrections(None, path=db) == []


def test_pending_list_is_oldest_first(db):
    a = att.create_correction("EMP-001", "missing_check_in", path=db)["correction_id"]
    b = att.create_correction("EMP-002", "missing_check_in", path=db)["correction_id"]
    ids = [c["id"] for c in att.pending_corrections(None, path=db)]
    assert ids.index(a) < ids.index(b)


# ===================== التخزين =====================

def test_db_path_is_resolved_at_call_time(tmp_path, monkeypatch):
    """ربط المسار كقيمة افتراضية يجعل الوحدة تعمل على قاعدة غير المقصودة."""
    other = tmp_path / "other.db"
    build_db.build(other)
    migrations.migrate(other)
    monkeypatch.setattr(att, "DB_PATH", other)
    att.create_site("هنا", *RIYADH, path=None)
    assert len(att.sites(path=other)) == 1


def test_junk_coordinates_are_stored_as_null_not_zero(db, hq):
    """تخزين المجهول صفراً يجعل خليج غينيا إحداثيات صالحة."""
    att.check_in("EMP-001", "شمال", None, 10, path=db)
    row = att.records_for(["EMP-001"], path=db)[0]
    assert row["in_reported_lat"] is None
    assert row["in_reported_lon"] is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_coordinates_are_stored_as_null(db, hq, bad):
    """مسار مختلف عن النصّ: NaN و inf يعبران float() بلا استثناء،
    فيصلان إلى الفحص الأخير — ولولاه لخُزّنا كأرقام."""
    att.check_in("EMP-001", bad, bad, 10, path=db)
    row = att.records_for(["EMP-001"], path=db)[0]
    assert row["in_reported_lat"] is None
    assert row["in_reported_lon"] is None


def test_non_finite_accuracy_is_stored_as_null(db, hq):
    att.check_in("EMP-001", *RIYADH, float("nan"), path=db)
    assert att.records_for(["EMP-001"], path=db)[0]["in_reported_accuracy_m"] is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_values_round_trip_through_check_out(db, hq, bad):
    att.check_in("EMP-001", *RIYADH, 10, path=db)
    att.check_out("EMP-001", bad, bad, bad, path=db)
    row = att.records_for(["EMP-001"], path=db)[0]
    assert row["out_reported_lat"] is None
    assert row["out_reported_accuracy_m"] is None


def test_long_text_is_truncated_not_rejected(db):
    r = att.create_correction("EMP-001", "other", "ط" * 5000, "س" * 5000, path=db)
    assert r["created"]
    row = att.correction_by_id(r["correction_id"], path=db)
    assert len(row["requested_change"]) <= 400 and len(row["reason"]) <= 400


def test_no_sql_injection_through_employee_id(db, hq):
    att.check_in("EMP-001'; DROP TABLE attendance_records; --", *RIYADH, 10, path=db)
    conn = sqlite3.connect(db)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "attendance_records" in names
