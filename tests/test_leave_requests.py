"""اختبارات طلبات الإجازة.

المبدأ الحاكم: الأداة التي تقرأ منفصلة عن الأداة التي تكتب.
معظم هذي الاختبارات تحرس هذا الفصل.
"""

from datetime import date, timedelta

import pytest

import build_db
import tools


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """قاعدة معزولة لكل اختبار — الكتابة تلوّث ما بعدها."""
    db = tmp_path / "test.db"
    build_db.build(db)
    original = tools.DB_PATH
    tools.DB_PATH = db
    yield db
    tools.DB_PATH = original


def future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


# ---------- المعاينة لا تكتب ----------

def test_preview_never_writes():
    """الضمان الأساسي — preview لا تستطيع تغيير الحالة."""
    before = tools.list_leave_requests()["count"]
    tools.preview_leave_request("خالد", future(10), 5)
    tools.preview_leave_request("نورة", future(15), 3)
    assert tools.list_leave_requests()["count"] == before


def test_preview_computes_effect_on_balance():
    p = tools.preview_leave_request("خالد", future(10), 5)
    assert p["valid"]
    assert p["remaining_before"] == 8
    assert p["remaining_after"] == 3


def test_preview_computes_end_date():
    p = tools.preview_leave_request("خالد", future(10), 5)
    start = date.fromisoformat(p["start_date"])
    end = date.fromisoformat(p["end_date"])
    assert (end - start).days == 4  # خمسة أيام شاملة الطرفين


# ---------- الرفض ----------

def test_rejects_insufficient_balance():
    r = tools.preview_leave_request("خالد", future(10), 20)
    assert not r["valid"]
    assert "الرصيد" in r["reason"]


def test_rejects_past_date():
    past = (date.today() - timedelta(days=3)).isoformat()
    assert not tools.preview_leave_request("خالد", past, 2)["valid"]


def test_rejects_unknown_employee():
    assert not tools.preview_leave_request("شخص وهمي", future(10), 2)["valid"]


def test_rejects_unparseable_date():
    """اللغة الطبيعية للتواريخ مصدر خطأ — نطلب صيغة صريحة."""
    r = tools.preview_leave_request("خالد", "الأحد القادم", 3)
    assert not r["valid"]
    assert "YYYY-MM-DD" in r["reason"]


@pytest.mark.parametrize("days", [0, -1, 31, 500])
def test_rejects_invalid_day_counts(days):
    assert not tools.preview_leave_request("خالد", future(10), days)["valid"]


def test_rejects_non_numeric_days():
    assert not tools.preview_leave_request("خالد", future(10), "خمسة")["valid"]


# ---------- التحذيرات ----------

def test_short_notice_warns_but_allows():
    """السياسة تتطلب 7 أيام مهلة — نحذّر ولا نمنع.

    المنع قرار عمل لا تقني؛ التحذير يترك القرار للمستخدم.
    """
    r = tools.preview_leave_request("خالد", future(2), 2)
    assert r["valid"]
    assert r["warnings"]


def test_sufficient_notice_has_no_warning():
    assert not tools.preview_leave_request("خالد", future(20), 2)["warnings"]


# ---------- التنفيذ ----------

def test_submit_creates_record():
    before = tools.list_leave_requests()["count"]
    r = tools.submit_leave_request("خالد", future(10), 5, "ظروف عائلية")
    assert r["submitted"]
    assert tools.list_leave_requests()["count"] == before + 1


def test_submit_revalidates_independently():
    """التنفيذ لا يثق بأن المعاينة تمّت — يعيد كل الفحوص.

    لو استدعى النموذج submit مباشرة متجاوزاً preview، يبقى محمياً.
    """
    past = (date.today() - timedelta(days=5)).isoformat()
    r = tools.submit_leave_request("خالد", past, 3)
    assert not r["submitted"]
    assert tools.list_leave_requests()["count"] == 0


def test_submit_rejects_over_balance():
    r = tools.submit_leave_request("خالد", future(10), 25)
    assert not r["submitted"]
    assert tools.list_leave_requests()["count"] == 0


def test_submitted_request_starts_pending():
    """الطلب لا يُعتمد تلقائياً — الاعتماد قرار بشري."""
    tools.submit_leave_request("خالد", future(10), 3)
    assert tools.list_leave_requests()["requests"][0]["status"] == "pending"


def test_reason_is_truncated():
    tools.submit_leave_request("خالد", future(10), 2, "س" * 900)
    # لا ينهار، والسجل أُنشئ
    assert tools.list_leave_requests()["count"] == 1


# ---------- العرض ----------

def test_listing_filters_by_name():
    tools.submit_leave_request("خالد", future(10), 2)
    tools.submit_leave_request("نورة", future(12), 3)

    assert tools.list_leave_requests("خالد")["count"] == 1
    assert tools.list_leave_requests()["count"] == 2


def test_listing_empty_when_none():
    assert tools.list_leave_requests()["count"] == 0


def test_listing_newest_first():
    tools.submit_leave_request("خالد", future(10), 2)
    tools.submit_leave_request("نورة", future(12), 3)
    assert tools.list_leave_requests()["requests"][0]["employee_name"] == "نورة العتيبي"


# ---------- حجز الرصيد للطلبات المعلّقة ----------

def test_pending_requests_reserve_balance():
    """بدون الحجز، يمكن تقديم طلبات مجموعها يتجاوز الرصيد.

    ثغرة منطق عمل تُسمّى double booking — الكود سليم والقاعدة خاطئة.
    """
    tools.submit_leave_request("خالد", future(10), 5)
    p = tools.preview_leave_request("خالد", future(30), 5)
    assert not p["valid"]
    assert p["pending_days"] == 5
    assert p["remaining_before"] == 3


def test_pending_days_reported_in_preview():
    tools.submit_leave_request("خالد", future(10), 3)
    p = tools.preview_leave_request("خالد", future(30), 2)
    assert p["pending_days"] == 3
    assert p["remaining_before"] == 5


def test_no_pending_means_full_balance():
    p = tools.preview_leave_request("خالد", future(10), 2)
    assert p["pending_days"] == 0
    assert p["remaining_before"] == 8


# ---------- الإلغاء ----------

def test_cancel_returns_days_to_balance():
    tools.submit_leave_request("خالد", future(10), 5)
    assert tools.preview_leave_request("خالد", future(30), 5)["valid"] is False

    tools.cancel_leave_request(1, "خالد الدوسري")
    assert tools.preview_leave_request("خالد", future(30), 5)["valid"] is True


def test_cancel_sets_status():
    tools.submit_leave_request("خالد", future(10), 2)
    tools.cancel_leave_request(1)
    assert tools.list_leave_requests()["requests"][0]["status"] == "cancelled"


def test_cannot_cancel_another_employees_request():
    """أول قاعدة صلاحيات: العملية على مورد تتحقق من ملكيته."""
    tools.submit_leave_request("نورة", future(10), 2)
    r = tools.cancel_leave_request(1, "خالد الدوسري")
    assert not r["cancelled"]
    assert tools.list_leave_requests()["requests"][0]["status"] == "pending"


def test_cannot_cancel_twice():
    tools.submit_leave_request("خالد", future(10), 2)
    tools.cancel_leave_request(1)
    r = tools.cancel_leave_request(1)
    assert not r["cancelled"]
    assert "cancelled" in r["reason"]


def test_cancel_unknown_request():
    r = tools.cancel_leave_request(999)
    assert not r["cancelled"]


def test_cancel_rejects_non_numeric_id():
    assert not tools.cancel_leave_request("واحد")["cancelled"]


def test_cancel_without_name_is_allowed():
    """اسم فارغ يعني استدعاءً إدارياً — لا نمنعه."""
    tools.submit_leave_request("خالد", future(10), 2)
    assert tools.cancel_leave_request(1, "")["cancelled"]
