"""التزامن — خيوط حقيقية على قاعدة حقيقية.

لا monkeypatch هنا: استثناء مُصطنَع يثبت مسار الخطأ، لا أن
عمليتين متزامنتين تنتهيان إلى حالة واحدة. الثانية تحتاج خيوطاً.

القياس الذي دعا إلى هذا الملف: ثمانية طلبات حضور متزامنة على
النسخة الأولى فتحت **سبع** جلسات لموظف واحد، وستّ منها كانت
ستبقى مفتوحة إلى الأبد لأن open_session يعيد LIMIT 1.
"""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

import attendance as att
import build_db
import geofence
import migrations

RIYADH = (24.7136, 46.6753)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "conc.db"
    build_db.build(path)
    migrations.migrate(path)
    monkeypatch.setattr(att, "DB_PATH", path)
    att.create_site("المقرّ", *RIYADH, radius_m=300, path=path)
    return path


def run_threads(fn, n):
    """يشغّل fn(i) في n خيطاً ويعيد النتائج مرتّبة بالفهرس."""
    out = {}
    errors = []

    def wrapped(i):
        try:
            out[i] = fn(i)
        except Exception as e:                      # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=wrapped, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, f"استثناء في خيط: {errors[0]!r}"
    return [out[i] for i in sorted(out)]


def open_count(db, employee_id="EMP-001") -> int:
    c = sqlite3.connect(db)
    try:
        return c.execute(
            "SELECT COUNT(*) FROM attendance_records WHERE employee_id=?"
            " AND in_verdict='inside' AND check_out_at IS NULL",
            (employee_id,)).fetchone()[0]
    finally:
        c.close()


def valid_times():
    end = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    return (end - timedelta(hours=8)).isoformat(), end.isoformat()


# ========================= الحضور =========================

def test_sixteen_concurrent_check_ins_open_exactly_one_session(db):
    results = run_threads(
        lambda i: att.check_in("EMP-001", *RIYADH, 10, path=db), 16)

    accepted = [r for r in results if r.get("accepted")]
    assert len(accepted) == 1, f"قُبل {len(accepted)} بدل واحد"
    assert open_count(db) == 1


def test_the_losers_get_a_polite_refusal_not_an_error(db):
    results = run_threads(
        lambda i: att.check_in("EMP-001", *RIYADH, 10, path=db), 16)

    losers = [r for r in results if not r.get("accepted")]
    assert len(losers) == 15
    assert all(r["recorded"] is False for r in losers), "لا سجل زائد"
    assert all("مفتوح" in r["reason"] for r in losers)
    assert len({r["reason"] for r in losers}) == 1, "ردّ حتميّ واحد"


def test_concurrent_check_ins_write_exactly_one_row(db):
    run_threads(lambda i: att.check_in("EMP-001", *RIYADH, 10, path=db), 16)
    assert len(att.records_for(["EMP-001"], path=db)) == 1


def test_different_employees_do_not_block_each_other(db):
    results = run_threads(
        lambda i: att.check_in(f"EMP-{100 + i}", *RIYADH, 10, path=db), 8)
    assert all(r.get("accepted") for r in results)


# ========================= الانصراف =========================

def test_two_concurrent_check_outs_keep_the_first_time(db):
    att.check_in("EMP-001", *RIYADH, 10, path=db)
    results = run_threads(
        lambda i: att.check_out("EMP-001", *RIYADH, 10, path=db), 2)

    won = [r for r in results if r.get("recorded")]
    assert len(won) == 1
    lost = [r for r in results if not r.get("recorded")]
    assert all(r["reason"] == att.NO_OPEN_SESSION for r in lost), \
        "ردّ حتميّ واحد مهما كانت لحظة الوصول"

    row = att.records_for(["EMP-001"], path=db)[0]
    assert row["check_out_at"] == won[0]["check_out_at"], "الوقت لم يُدهس"


def test_interleaved_check_in_and_out_leaves_no_orphan_session(db):
    """خمسون دورة متشابكة — لا جلسة يتيمة في النهاية."""
    def cycle(i):
        for _ in range(25):
            att.check_in("EMP-001", *RIYADH, 10, path=db)
            att.check_out("EMP-001", *RIYADH, 10, path=db)
        return True

    run_threads(cycle, 2)
    assert open_count(db) <= 1


# ========================= مراجعة التصحيح =========================

def _record_and_correction(db, owner="EMP-001"):
    """جلسة مفتوحة مؤرّخة في الماضي، وطلب إغلاقها.

    الحضور يُزرع مباشرةً لا عبر check_in: الأخير يكتب الوقت الحالي،
    وأي انصراف مطلوب بعده يقع في المستقبل فيُرفض. والواقع أن بين
    الحضور والانصراف ساعات.
    """
    start, end = valid_times()
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "INSERT INTO attendance_records (employee_id, check_in_at, in_verdict,"
            " created_at) VALUES (?,?,?,?)", (owner, start, "inside", start))
        rid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    cid = att.create_correction(owner, "missing_check_out", record_id=rid,
                                requested_check_out_at=end, path=db)["correction_id"]
    return rid, cid, end


def test_two_concurrent_approvals_apply_exactly_once(db):
    """الحالة النهائية معروفة سلفاً: كلا المراجعَين يعتمد."""
    rid, cid, wanted = _record_and_correction(db)
    results = run_threads(
        lambda i: att.review_correction(cid, "approve", f"EMP-90{i + 1}", path=db), 2)

    won = [r for r in results if r.get("reviewed")]
    assert len(won) == 1

    corr = att.correction_by_id(cid, path=db)
    assert corr["status"] == att.STATUS_APPROVED
    assert corr["reviewed_by"] in {"EMP-901", "EMP-902"}
    assert corr["decided_at"] is not None

    rec = att.record_by_id(rid, path=db)
    assert rec["check_out_at"] == wanted
    assert rec["out_verdict"] == att.VERDICT_CORRECTION_CLOSED
    assert rec["corrected_by_correction_id"] == cid


def test_sixteen_reviewers_race_and_the_winner_decides(db):
    """قرارات مختلطة: الحالة النهائية من الفائز لا من ثابت محفوظ."""
    rid, cid, wanted = _record_and_correction(db)
    decisions = ["approve", "reject"] * 8
    results = run_threads(
        lambda i: att.review_correction(cid, decisions[i], f"EMP-9{i:02d}", path=db), 16)

    won = [r for r in results if r.get("reviewed")]
    assert len(won) == 1, f"نجح {len(won)} مراجع"
    winner = won[0]

    corr = att.correction_by_id(cid, path=db)
    assert corr["status"] == winner["status"]
    assert corr["reviewed_by"] == winner["reviewed_by"]

    rec = att.record_by_id(rid, path=db)
    if winner["status"] == att.STATUS_APPROVED:
        assert rec["check_out_at"] == wanted
        assert rec["corrected_by_correction_id"] == cid
    else:
        assert rec["check_out_at"] is None, "الرفض لا يطبّق شيئاً"
        assert rec["corrected_by_correction_id"] is None


def test_the_losing_reviewers_share_one_deterministic_reason(db):
    _, cid, _ = _record_and_correction(db)
    results = run_threads(
        lambda i: att.review_correction(cid, "approve", f"EMP-9{i:02d}", path=db), 16)

    losers = [r for r in results if not r.get("reviewed")]
    assert len(losers) == 15
    assert all(r.get("reason") for r in losers), "لكل خاسر سبب"
    assert len({r["reason"] for r in losers}) == 1, "سبب واحد حتميّ"


def test_no_partial_change_under_review_contention(db):
    """لا قرار بلا أثر، ولا أثر بلا قرار."""
    rid, cid, _ = _record_and_correction(db)
    run_threads(
        lambda i: att.review_correction(cid, "approve", f"EMP-9{i:02d}", path=db), 12)

    corr = att.correction_by_id(cid, path=db)
    rec = att.record_by_id(rid, path=db)
    applied = rec["corrected_by_correction_id"] == cid
    decided = corr["status"] == att.STATUS_APPROVED
    assert applied == decided, "القرار والتطبيق متلازمان"


def test_self_review_never_wins_a_race(db):
    """فصل المهام يصمد تحت التزامن: صاحب الطلب يخسر دائماً."""
    _, cid, _ = _record_and_correction(db, owner="EMP-001")
    reviewers = ["EMP-001"] * 8 + ["EMP-902"] * 8
    results = run_threads(
        lambda i: att.review_correction(cid, "approve", reviewers[i], path=db), 16)

    won = [r for r in results if r.get("reviewed")]
    assert len(won) == 1
    assert att.correction_by_id(cid, path=db)["reviewed_by"] == "EMP-902"


# ========================= حلّ التعارض =========================

def test_resolving_concurrently_never_closes_the_last_session(db):
    """حارس الجلسة الأخيرة يصمد تحت التزامن."""
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO attendance_records (employee_id, check_in_at, in_verdict)"
        " VALUES (?,?,?)",
        [("EMP-777", "2026-10-12T07:00:00+00:00", "inside")])
    conn.commit(); conn.close()

    ids = [r["id"] for r in att.inspect_duplicate_sessions("EMP-777", path=db)]
    assert ids == [], "صفّ واحد ليس تعارضاً"

    rid = att.records_for(["EMP-777"], path=db)[0]["id"]
    r = att.resolve_duplicate_session(rid, "EMP-100", "محاولة إغلاق الوحيدة", path=db)
    assert not r["resolved"]
    # الحارسان مختلفان: الأول يرفض قبل الكتابة، والثاني يتراجع بعدها.
    # السبب يميّزهما — بلا هذا التأكيد يسقط الأوّل بلا أن يسقط اختبار.
    assert "جلسته المفتوحة الوحيدة" in r["reason"], \
        "الرفض جاء من الحارس القبلي لا من تراجع ما بعد الكتابة"
    assert open_count(db, "EMP-777") == 1


# ========================= حزام ثانٍ لا يُقاس وحده =========================

def test_the_review_update_keeps_its_compare_and_swap():
    """تأكيد بنيوي صريح، وسببه مُعلن.

    اختبار الطفرات أظهر أن إزالة `AND status = ?` من تحديث المراجعة
    لا تُسقط اختباراً — وهذا صحيح لا ثغرة: القراءة والكتابة داخل
    معاملة BEGIN IMMEDIATE واحدة، فلا كاتب آخر يبدّل الحالة بينهما،
    ولا مسار عامّ يُظهر أثر الشرط منفرداً.

    الحزام يبقى لأنه يصير الحارس الوحيد إن خرجت القراءة من القفل
    يوماً — وتلك الطفرة بعينها تُقتل باختبارات هذا الملف. والتأكيد
    هنا بنيوي صراحةً: يمنع حذف الشرط سهواً، ولا يدّعي قياس سلوك.
    """
    import inspect
    src = inspect.getsource(att.review_correction)
    assert "WHERE id = ? AND status = ?" in src
    assert "BEGIN IMMEDIATE" in inspect.getsource(att._locked_conn)
