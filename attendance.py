"""طبقة بيانات الحضور — مواقع العمل والسجلات وطلبات التصحيح.

على نمط org.py: هذه الطبقة **لا تعرف الصلاحيات**. تستقبل معرّفات
وتعيد صفوفاً. من يقرّر أي الموظفين يحقّ للفاعل رؤيتهم هو api.py
و tools.py، كما يفعلان مع الإجازات. نقطة قرار واحدة لكل سؤال:
الهيكل يقرّر النطاق، وgeofence يقرّر الموقع، وهذه تخزّن.

الحكم لا يُستقبل أبداً من المستدعي: check_in تأخذ الإحداثيات
الخام وتستدعي geofence.evaluate بنفسها. لا يوجد معامل اسمه
verdict ولا inside ولا site_id — فلا مسار يلتفّ على الحساب.

قواعد المعاملات في هذا الملف
-----------------------------
**مالك المعاملة وحده يتراجع عنها.** الدالة التي تنفّذ BEGIN هي
التي تنفّذ COMMIT أو ROLLBACK. والدالة الداخلية التي تستقبل conn
من مستدعيها تكتب وترفع الاستثناء ولا تمسّ المعاملة — التراجع عن
معاملة ليست لها يُسقط عمل المستدعي بلا علمه.

**لا COMMIT في مسار استثناء، ولا اعتماد على ABORT للتراجع.**
RAISE(ABORT) في مُطلِقات SQLite يُلغي العبارة الفاشلة وحدها ويترك
المعاملة مفتوحة وما قبلها قابلاً للاعتماد. فكل التقاط لاستثناء
في دالة مالكة يسبقه ROLLBACK صريح.
"""

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import geofence
from logging_config import get_logger

log = get_logger(__name__)

DB_PATH = Path("company.db")

# مهلة انتظار القفل. الكاتب الثاني ينتظر بدل أن يفشل فوراً.
LOCK_TIMEOUT_S = 10

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

CORRECTION_KINDS = ("missing_check_in", "missing_check_out",
                    "wrong_time", "rejected_location", "other")

# أحكام البداية: الثلاثة من geofence، والرابع إداري.
VERDICT_MANUALLY_ADDED = "manually_added"

# أحكام النهاية الإدارية — مفصولة عمداً:
#   correction_closed: وقت انصراف طلبه الموظف وراجعه مديره → يُحتسب
#   conflict_closed:   وقت قرار المشغّل عند حلّ تعارض → لا يُحتسب
# قيمة واحدة للحالتين كانت ستُدخل وقت قرار إداري في ساعات العمل.
VERDICT_CORRECTION_CLOSED = "correction_closed"
VERDICT_CONFLICT_CLOSED = "conflict_closed"

# حارس إدخال لا سياسة دوام. لا توجد سياسة موثّقة في هذا المشروع،
# وفرض رقم من عندي يحوّل افتراضاً إلى قاعدة عمل بلا قرار.
# غير مفروض افتراضياً: اضبط MAX_SESSION_HOURS حين تُعتمد السياسة.
_raw_max = os.getenv("MAX_SESSION_HOURS", "").strip()
MAX_SESSION_HOURS = float(_raw_max) if _raw_max else None

MIN_RESOLUTION_NOTE = 10

# ردّ واحد لحالة واحدة: «لا جلسة مفتوحة الآن» — سواء لم يسجّل
# الموظف حضوراً أصلاً أو أغلقها طلب متزامن سبقه. تباين النصّ
# بحسب لحظة الوصول يجعل الردّ غير حتميّ.
NO_OPEN_SESSION = "لا يوجد حضور مفتوح لتسجيل انصراف منه"


class AttendanceTimeError(ValueError):
    """طابع زمني غير مقبول — صيغةً أو دقّةً أو إزاحةً."""


# ============================ الوقت ============================

def _to_utc_iso(value) -> str:
    """ISO-8601 بإزاحة صريحة ← طابع UTC قانوني بدقّة الثانية.

    التوحيد عند الكتابة والاستعلام معاً. بدونه تكذب المقارنة النصّية:
    "07:00:00+03:00" أسبق من "05:00:00+00:00" زمنياً، والنصّ يقول
    العكس. وبعد التوحيد يصير الترتيب المعجمي مطابقاً للزمني.

    يُرفض:
      - الطابع بلا إزاحة — تفسيره يتطلّب افتراض منطقة زمنية.
      - الكسر غير الصفري — القصّ يفقد البيانات بصمت ثم يرفض
        التحقّق طلباً صحيحاً بحجّة أن الانصراف سبق الحضور.
    """
    text = str(value or "").strip()
    if not text:
        raise AttendanceTimeError("الطابع الزمني مطلوب")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise AttendanceTimeError(
            "صيغة الوقت غير صالحة؛ المتوقَّع مثل 2026-10-12T07:00:00+03:00") from None
    if dt.tzinfo is None:
        raise AttendanceTimeError(
            "يلزم طابع زمني بإزاحة صريحة، مثل 2026-10-12T07:00:00+03:00")
    if dt.microsecond:
        raise AttendanceTimeError("يُقبل الوقت بدقّة الثانية؛ أزل أجزاء الثانية")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _minutes_between(start: str, end: str) -> int | None:
    """المدّة بالدقائق. None إن تعذّر الحساب، و0 لا سالب."""
    if not start or not end:
        return None
    try:
        a = datetime.fromisoformat(start)
        b = datetime.fromisoformat(end)
    except (TypeError, ValueError):
        return None
    return max(0, int((b - a).total_seconds() // 60))


def _num(value):
    """يحوّل إلى float للتخزين، ويحفظ None بدل قيمة فاسدة.

    تخزين 'غير معروف' كصفر يجعل خليج غينيا إحداثيات صالحة.
    NaN و inf يعبران float() بلا استثناء فيُفحصان هنا.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


# ============================ الاتصال ============================

def _db(path=None) -> Path:
    """يحلّ المسار عند الاستدعاء لا عند الاستيراد.

    ربطه كقيمة افتراضية يجعل إعادة تعيين DB_PATH بعد الاستيراد
    بلا أثر، فتعمل الوحدة على قاعدة غير المقصودة بصمت.
    """
    return Path(path) if path else DB_PATH


def _conn(path=None):
    c = sqlite3.connect(_db(path), timeout=LOCK_TIMEOUT_S)
    c.row_factory = sqlite3.Row
    return c


def _locked_conn(path=None):
    """اتصال بمعاملة صريحة يملكها المستدعي.

    isolation_level=None ضروري: بدونه يتحكّم Python في بدء المعاملة
    فلا يصل BEGIN IMMEDIATE.
    """
    c = sqlite3.connect(_db(path), timeout=LOCK_TIMEOUT_S, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("BEGIN IMMEDIATE")
    return c


def _rollback(conn) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def _query(sql, params=(), path=None) -> list[dict]:
    c = _conn(path)
    try:
        return [dict(r) for r in c.execute(sql, params)]
    finally:
        c.close()


def _one(sql, params=(), path=None) -> dict | None:
    rows = _query(sql, params, path)
    return rows[0] if rows else None


# ============================ مواقع العمل ============================

def sites(active_only: bool = True, path=None) -> list[dict]:
    sql = "SELECT * FROM sites"
    if active_only:
        sql += " WHERE is_active = 1"
    return _query(sql + " ORDER BY name", path=path)


def site_by_id(site_id: str, path=None) -> dict | None:
    return _one("SELECT * FROM sites WHERE site_id = ?",
                ((site_id or "").strip(),), path)


def create_site(name: str, latitude, longitude, radius_m=150,
                department_id: str | None = None, path=None) -> dict:
    """ينشئ موقع عمل. يتحقّق من الإحداثيات ونصف القطر قبل الكتابة.

    الموقع بإحداثيات فاسدة أخطر من غياب الموقع: السياج يُحسب عليه،
    فيرفض كل الحضور أو يقبله كله.
    """
    name = str(name or "").strip()
    if len(name) < 2:
        raise geofence.GeofenceError("اسم الموقع قصير جداً")
    if not geofence.valid_coordinates(latitude, longitude):
        raise geofence.GeofenceError("إحداثيات الموقع غير صالحة")
    if not geofence.valid_radius(radius_m):
        raise geofence.GeofenceError(
            f"نصف القطر يجب أن يكون بين {geofence.MIN_RADIUS_M} "
            f"و{geofence.MAX_RADIUS_M} متراً")

    site_id = "SITE-" + uuid.uuid4().hex[:8].upper()
    c = _conn(path)
    try:
        c.execute(
            """INSERT INTO sites (site_id, name, latitude, longitude, radius_m,
                                  department_id, is_active, created_at)
               VALUES (?,?,?,?,?,?,1,?)""",
            (site_id, name, float(latitude), float(longitude), int(float(radius_m)),
             (department_id or "").strip() or None, _now()),
        )
        c.commit()
    finally:
        c.close()

    log.info("أُنشئ موقع عمل: %s (%s)", name, site_id)
    return site_by_id(site_id, path)


def update_site(site_id: str, name=None, latitude=None, longitude=None,
                radius_m=None, department_id=None, path=None) -> dict | None:
    """يعدّل حقول موقع. ما يُترك None يبقى كما هو."""
    site = site_by_id(site_id, path)
    if site is None:
        return None

    if name is not None:
        name = str(name).strip()
        if len(name) < 2:
            raise geofence.GeofenceError("اسم الموقع قصير جداً")
    if latitude is not None or longitude is not None:
        la = site["latitude"] if latitude is None else latitude
        lo = site["longitude"] if longitude is None else longitude
        if not geofence.valid_coordinates(la, lo):
            raise geofence.GeofenceError("إحداثيات الموقع غير صالحة")
    if radius_m is not None and not geofence.valid_radius(radius_m):
        raise geofence.GeofenceError("نصف القطر خارج الحدود المقبولة")

    c = _conn(path)
    try:
        c.execute(
            """UPDATE sites SET name = ?, latitude = ?, longitude = ?,
                                radius_m = ?, department_id = ?
                WHERE site_id = ?""",
            (name if name is not None else site["name"],
             float(latitude) if latitude is not None else site["latitude"],
             float(longitude) if longitude is not None else site["longitude"],
             int(float(radius_m)) if radius_m is not None else site["radius_m"],
             ((department_id or "").strip() or None) if department_id is not None
             else site["department_id"],
             site["site_id"]),
        )
        c.commit()
    finally:
        c.close()
    return site_by_id(site_id, path)


def set_site_active(site_id: str, active: bool, path=None) -> dict | None:
    """يعطّل موقعاً أو يعيد تفعيله. لا حذف — السجلات التاريخية تشير إليه."""
    if site_by_id(site_id, path) is None:
        return None
    c = _conn(path)
    try:
        c.execute("UPDATE sites SET is_active = ? WHERE site_id = ?",
                  (1 if active else 0, (site_id or "").strip()))
        c.commit()
    finally:
        c.close()
    return site_by_id(site_id, path)


# ============================ الحضور ============================

_OPEN_SESSION_SQL = (
    "SELECT * FROM attendance_records"
    " WHERE employee_id = ? AND in_verdict = ? AND check_out_at IS NULL"
    " ORDER BY check_in_at DESC LIMIT 1"
)


def open_session(employee_id: str, path=None) -> dict | None:
    """الجلسة المفتوحة للموظف — حضور مقبول بلا انصراف.

    المحاولات المرفوضة لا تفتح جلسة، ولا السجلات الإدارية: الشرط
    in_verdict='inside' هو نفسه شرط الفهرس الفريد في الهجرة 007.
    """
    return _one(_OPEN_SESSION_SQL,
                ((employee_id or "").strip(), geofence.VERDICT_INSIDE), path)


def check_in(employee_id: str, latitude, longitude, accuracy_m,
             request_ip: str = "", path=None) -> dict:
    """يسجّل محاولة حضور ويحكم عليها في الخادم.

    يُكتب صفّ لكل محاولة — المقبولة والمرفوضة. لماذا نحفظ المرفوضة؟
    لأن الموظف الذي رُفض حضوره يحتاج شيئاً يقدّم عليه طلب تصحيح،
    والمسؤول يحتاج أن يرى تكرار الرفض في موقع ما: قد يكون السياج
    ضيّقاً لا الموظف مخطئاً.

    الفحص والكتابة تحت قفل كتابة واحد. بدونه كان ثمانية طلبات
    متزامنة يفتحون سبع جلسات: كلٌّ يقرأ «لا جلسة مفتوحة» قبل أن
    يكتب أيٌّ منهم. والفهرس الفريد حزام ثانٍ يلتقط ما يفلت.
    """
    employee_id = (employee_id or "").strip()
    if not employee_id:
        return {"recorded": False, "reason": "تعذّر التحقق من هويتك"}

    result = geofence.evaluate(latitude, longitude, accuracy_m,
                               sites(active_only=True, path=path))
    accepted = result["verdict"] == geofence.VERDICT_INSIDE
    now = _now()

    conn = _locked_conn(path)
    try:
        existing = conn.execute(
            _OPEN_SESSION_SQL, (employee_id, geofence.VERDICT_INSIDE)).fetchone()
        if existing is not None and accepted:
            _rollback(conn)
            return {"recorded": False,
                    "reason": "لديك حضور مفتوح — سجّل انصرافك أولاً",
                    "record_id": existing["id"], "since": existing["check_in_at"]}

        cur = conn.execute(
            """INSERT INTO attendance_records
                 (employee_id, site_id, check_in_at,
                  in_reported_lat, in_reported_lon, in_reported_accuracy_m,
                  in_computed_distance_m, in_verdict, in_verdict_reason,
                  in_request_ip, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (employee_id,
             result["site_id"] if accepted else None,
             now,
             _num(latitude), _num(longitude), _num(accuracy_m),
             result["distance_m"], result["verdict"], result["reason"],
             str(request_ip or "")[:45], now),
        )
        record_id = cur.lastrowid
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        # الفهرس الفريد التقط جلسة ثانية أفلتت من الفحص.
        _rollback(conn)
        existing = open_session(employee_id, path)
        return {"recorded": False,
                "reason": "لديك حضور مفتوح — سجّل انصرافك أولاً",
                "record_id": existing["id"] if existing else None,
                "since": existing["check_in_at"] if existing else None}
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()

    log.info("حضور %s | %s | %s", employee_id, result["verdict"],
             result["reason"] or "—")

    return {
        "recorded": True,
        "accepted": accepted,
        "record_id": record_id,
        "verdict": result["verdict"],
        "reason": result["reason"],
        "site_id": result["site_id"] if accepted else None,
        "site_name": result["site_name"] if accepted else None,
        "distance_m": result["distance_m"],
        "check_in_at": now,
    }


def check_out(employee_id: str, latitude, longitude, accuracy_m,
              request_ip: str = "", path=None) -> dict:
    """يغلق الجلسة المفتوحة.

    الانصراف خارج النطاق يُسجَّل ويُغلق: منع الموظف من الانصراف
    لأن إشارته ضعيفة يتركه «حاضراً» إلى الأبد. نحفظ الحكم ليُراجع،
    ولا نحتجز الجلسة.

    الإغلاق بمقارنة-وتبديل: الشرط `check_out_at IS NULL` في جملة
    WHERE هو القفل. طلبان متزامنان — الأول يُرجع rowcount=1 والثاني
    صفراً، فلا يدهس أحدهما وقت انصراف الآخر. لا حاجة لمعاملة صريحة.
    """
    employee_id = (employee_id or "").strip()
    if not employee_id:
        return {"recorded": False, "reason": "تعذّر التحقق من هويتك"}

    session = open_session(employee_id, path)
    if session is None:
        return {"recorded": False, "reason": NO_OPEN_SESSION}

    result = geofence.evaluate(latitude, longitude, accuracy_m,
                               sites(active_only=True, path=path))
    now = _now()

    c = _conn(path)
    try:
        cur = c.execute(
            """UPDATE attendance_records
                  SET check_out_at = ?,
                      out_reported_lat = ?, out_reported_lon = ?,
                      out_reported_accuracy_m = ?,
                      out_computed_distance_m = ?, out_verdict = ?,
                      out_verdict_reason = ?, out_request_ip = ?
                WHERE id = ? AND check_out_at IS NULL""",
            (now, _num(latitude), _num(longitude), _num(accuracy_m),
             result["distance_m"], result["verdict"], result["reason"],
             str(request_ip or "")[:45], session["id"]),
        )
        if cur.rowcount == 0:
            # أُغلقت بين القراءة والكتابة. نفس الرسالة لا رسالة أخرى:
            # الردّ يجب أن يكون حتميّاً، ولو تباين بحسب لحظة وصول
            # الطلب لصار المستخدم يرى نصّين لحالة واحدة.
            c.rollback()
            return {"recorded": False, "reason": NO_OPEN_SESSION}
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

    log.info("انصراف %s | %s", employee_id, result["verdict"])
    return {
        "recorded": True,
        "record_id": session["id"],
        "verdict": result["verdict"],
        "reason": result["reason"],
        "check_in_at": session["check_in_at"],
        "check_out_at": now,
        "distance_m": result["distance_m"],
    }


def records_for(employee_ids, start: str = "", end: str = "",
                limit: int = 200, path=None) -> list[dict]:
    """سجلات مجموعة موظفين. المستدعي يحدّد المجموعة من النطاق.

    employee_ids = None تعني بلا حدّ (مسؤول أو استدعاء داخلي) —
    نفس اصطلاح _visible_employee_ids في tools.py. القائمة الفارغة
    تعني لا أحد، فنعيد فارغاً بلا استعلام.

    النافذة نصف مفتوحة: start **شامل** و end **غير شامل**. فنافذتان
    متتاليتان لا تحسبان السجل الواقع على الحدّ مرّتين.

    الحدّان طابعان كاملان بإزاحة صريحة. التاريخ العاري مرفوض: يومٌ
    بلا منطقة زمنية يتطلّب افتراضها، والمستدعي وحده يعرف منطقته
    فيحسب حدوده ويرسلها.
    """
    where, params = [], []

    if employee_ids is not None:
        ids = [str(e).strip() for e in employee_ids if str(e).strip()]
        if not ids:
            return []
        where.append("employee_id IN (%s)" % ",".join("?" * len(ids)))
        params.extend(ids)

    if start:
        where.append("check_in_at >= ?")
        params.append(_to_utc_iso(start))
    if end:
        where.append("check_in_at < ?")
        params.append(_to_utc_iso(end))

    sql = "SELECT * FROM attendance_records"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY check_in_at DESC LIMIT ?"
    params.append(max(1, min(int(limit or 200), 1000)))

    return _query(sql, tuple(params), path)


def employee_names(employee_ids, path=None) -> dict:
    """أسماء مجموعة معرّفات. استعلام واحد لا واحد لكل صفّ."""
    ids = sorted({str(e).strip() for e in (employee_ids or []) if str(e).strip()})
    if not ids:
        return {}
    rows = _query(
        "SELECT employee_id, name FROM employees WHERE employee_id IN (%s)"
        % ",".join("?" * len(ids)), tuple(ids), path)
    return {r["employee_id"]: r["name"] for r in rows}


def record_by_id(record_id, path=None) -> dict | None:
    try:
        rid = int(record_id)
    except (TypeError, ValueError):
        return None
    return _one("SELECT * FROM attendance_records WHERE id = ?", (rid,), path)


# ---------- الاحتساب ----------

def counts_toward_work(record: dict) -> bool:
    """هل لهذا السجل مدّة عمل معتبَرة؟

    conflict_closed لا: وقت إغلاقه قرار إداري لا انصراف، وطرحه من
    وقت الحضور يعطي رقماً بلا معنى.
    """
    return bool(record.get("check_out_at")) and \
        record.get("out_verdict") != VERDICT_CONFLICT_CLOSED


def record_minutes(record: dict) -> int | None:
    """مدّة السجل، أو None إن كانت غير معروفة.

    None لا صفر: الصفر يقول «عمل صفر دقيقة»، وNone يقول «غير معروف».
    """
    if not counts_toward_work(record):
        return None
    return _minutes_between(record.get("check_in_at"), record.get("check_out_at"))


def summarize(records) -> dict:
    """يفصل الدقائق بمصدرها. المجموع لا يخفي من أين جاء.

    verified: قاسه السياج.  administrative: اعتمده إنسان.
    unresolved_sessions: عدد لا دقائق — مدّتها غير معروفة.
    """
    verified = administrative = 0
    unresolved = 0

    for r in records or []:
        if r.get("out_verdict") == VERDICT_CONFLICT_CLOSED:
            unresolved += 1
            continue
        minutes = record_minutes(r)
        if minutes is None:
            continue
        if r.get("in_verdict") == geofence.VERDICT_INSIDE and \
                r.get("out_verdict") != VERDICT_CORRECTION_CLOSED:
            verified += minutes
        else:
            administrative += minutes

    out = {
        "verified_minutes": verified,
        "administrative_minutes": administrative,
        "total_minutes": verified + administrative,
        "unresolved_sessions": unresolved,
    }
    if unresolved:
        out["unresolved_note"] = (
            f"{unresolved} جلسة أُغلقت إدارياً؛ مدّتها غير معروفة وغير محتسبة")
    return out


# ==================== الجلسات المتعارضة ====================

def inspect_duplicate_sessions(employee_id: str | None = None, path=None) -> list[dict]:
    """يعرض الجلسات المتعارضة بأدلّتها. لا يكتب شيئاً ولا يرشّح أحداً.

    لا تقترح أيّها «الزائد» عمداً. صفّان متباعدان ثانيتين من نفس
    الـIP بنفس الإحداثيات سباق تزامن، والأقدم هو الحقيقي. وصفّان
    بينهما ساعتان من موقعين مختلفين شيء آخر لا تحلّه هذه الأداة.
    التمييز حكم بشري، وتولّيه آلياً هو الإغلاق التلقائي الذي رُفض.
    """
    params = []
    having = """
        SELECT employee_id FROM attendance_records
         WHERE in_verdict = 'inside' AND check_out_at IS NULL
         GROUP BY employee_id HAVING COUNT(*) > 1
    """
    sql = f"""
        SELECT id, employee_id, check_in_at, created_at,
               in_reported_lat, in_reported_lon, in_reported_accuracy_m,
               in_computed_distance_m, site_id, in_request_ip
          FROM attendance_records
         WHERE in_verdict = 'inside' AND check_out_at IS NULL
           AND employee_id IN ({having})
    """
    if employee_id:
        sql += " AND employee_id = ?"
        params.append(str(employee_id).strip())
    sql += " ORDER BY employee_id, check_in_at, id"
    return _query(sql, tuple(params), path)


def resolve_duplicate_session(record_id, resolved_by_claimed: str,
                              note: str, path=None) -> dict:
    """يغلق جلسة مكرّرة بقرار موثَّق. لا يحذف شيئاً ولا يختلق انصرافاً.

    **سطر أوامر فقط.** لا نقطة API ولا واجهة ولا أداة وكيل تبلغها:
    هي المسار الوحيد الذي يكتب وقت انصراف لم يقسه السياج، فإبقاؤها
    خارج السطح المكشوف يعني أن لا طلب HTTP يصل إليها مهما كانت
    صلاحيته.

    resolved_by_claimed إقرار المشغّل بهويته، **لا هوية متحقَّقة**.
    لا توجد آلية تحقّق في هذه الطبقة، ومن يصل إلى سطر أوامر الخادم
    يملك القاعدة كلها فيكون فحص الدور هنا مسرحية. وإن أُضيف مسار
    مصادَق عليه لاحقاً فليكتب الهوية المتحقَّقة في عمود آخر.

    check_out_at المكتوب هو **وقت القرار** لا وقت انصراف مزعوم،
    و out_verdict='conflict_closed' يقول ذلك لكل قارئ، ومدّة هذا
    السجل لا تُحتسب في ساعات العمل إطلاقاً.
    """
    resolved_by_claimed = (resolved_by_claimed or "").strip()
    if not resolved_by_claimed:
        return {"resolved": False, "reason": "يلزم تحديد من اتخذ القرار"}

    note = (note or "").strip()
    if len(note) < MIN_RESOLUTION_NOTE:
        return {"resolved": False,
                "reason": f"يلزم سبب مكتوب لا يقلّ عن {MIN_RESOLUTION_NOTE} أحرف"}

    try:
        rid = int(record_id)
    except (TypeError, ValueError):
        return {"resolved": False, "reason": "لا يوجد سجل بهذا الرقم"}

    now = _now()
    conn = _locked_conn(path)
    try:
        rec = conn.execute(
            "SELECT * FROM attendance_records WHERE id = ?", (rid,)).fetchone()
        if rec is None:
            _rollback(conn)
            return {"resolved": False, "reason": "لا يوجد سجل بهذا الرقم"}
        if rec["in_verdict"] != geofence.VERDICT_INSIDE:
            _rollback(conn)
            return {"resolved": False, "reason": "هذا السجل ليس جلسة حضور مقبولة"}
        if rec["check_out_at"] is not None:
            _rollback(conn)
            return {"resolved": False, "reason": "هذه الجلسة مغلقة بالفعل"}

        before = conn.execute(
            "SELECT COUNT(*) FROM attendance_records"
            " WHERE employee_id = ? AND in_verdict = 'inside' AND check_out_at IS NULL",
            (rec["employee_id"],)).fetchone()[0]
        if before <= 1:
            # حارس يمنع تحوّل أداة الإصلاح إلى أداة إتلاف.
            _rollback(conn)
            return {"resolved": False,
                    "reason": "هذه جلسته المفتوحة الوحيدة — إغلاقها يفقده حضوره"}

        cur = conn.execute(
            """UPDATE attendance_records
                  SET check_out_at = ?, out_verdict = ?, out_verdict_reason = ?,
                      resolved_by_claimed = ?, resolved_at = ?
                WHERE id = ? AND check_out_at IS NULL""",
            (now, VERDICT_CONFLICT_CLOSED, note, resolved_by_claimed, now, rid),
        )
        if cur.rowcount == 0:
            _rollback(conn)
            return {"resolved": False, "reason": "أُغلقت أثناء المعالجة"}

        remaining = conn.execute(
            "SELECT COUNT(*) FROM attendance_records"
            " WHERE employee_id = ? AND in_verdict = 'inside' AND check_out_at IS NULL",
            (rec["employee_id"],)).fetchone()[0]
        if remaining == 0:
            _rollback(conn)
            return {"resolved": False, "reason": "الإغلاق كان سيفقده جلسته الأخيرة"}

        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()

    log.warning("أُغلقت جلسة مكرّرة #%s بإقرار %s — %s", rid, resolved_by_claimed, note)
    return {"resolved": True, "record_id": rid, "remaining_open": remaining,
            "closed_at": now}


# ============================ طلبات التصحيح ============================

def create_correction(employee_id: str, kind: str, requested_change: str = "",
                      reason: str = "", record_id=None,
                      requested_check_in_at: str | None = None,
                      requested_check_out_at: str | None = None,
                      path=None) -> dict:
    """يفتح طلب تصحيح على سجل حضور.

    السجل المشار إليه يجب أن يخصّ الموظف نفسه — فحص ملكية هنا لا
    صلاحية: لا معنى لطلب تصحيح على سجل غيرك مهما كان دورك.

    الأوقات المطلوبة تُوحَّد إلى UTC عند الكتابة، فتُقارَن لاحقاً
    بقيم مخزّنة بالصيغة نفسها.
    """
    employee_id = (employee_id or "").strip()
    if not employee_id:
        return {"created": False, "reason": "تعذّر التحقق من هويتك"}

    kind = str(kind or "").strip()
    if kind not in CORRECTION_KINDS:
        return {"created": False, "reason": "نوع طلب التصحيح غير معروف"}

    try:
        want_in = _to_utc_iso(requested_check_in_at) if requested_check_in_at else None
        want_out = _to_utc_iso(requested_check_out_at) if requested_check_out_at else None
    except AttendanceTimeError as e:
        return {"created": False, "reason": str(e)}

    if record_id is not None:
        rec = record_by_id(record_id, path)
        if rec is None:
            return {"created": False, "reason": "لا يوجد سجل بهذا الرقم"}
        if (rec["employee_id"] or "").strip() != employee_id:
            return {"created": False, "reason": "هذا السجل لا يخصّك"}
        record_id = rec["id"]

    now = _now()
    c = _conn(path)
    try:
        cur = c.execute(
            """INSERT INTO attendance_corrections
                 (employee_id, record_id, kind, requested_change, reason,
                  status, created_at, requested_check_in_at, requested_check_out_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (employee_id, record_id, kind,
             str(requested_change or "")[:400], str(reason or "")[:400],
             STATUS_PENDING, now, want_in, want_out),
        )
        correction_id = cur.lastrowid
        c.commit()
    finally:
        c.close()

    log.info("طلب تصحيح حضور #%d من %s (%s)", correction_id, employee_id, kind)
    return {"created": True, "correction_id": correction_id, "status": STATUS_PENDING}


def correction_by_id(correction_id, path=None) -> dict | None:
    try:
        cid = int(correction_id)
    except (TypeError, ValueError):
        return None
    return _one("SELECT * FROM attendance_corrections WHERE id = ?", (cid,), path)


def pending_corrections(employee_ids, limit: int = 100, path=None) -> list[dict]:
    """الطلبات المعلّقة لمجموعة موظفين. None = بلا حدّ."""
    where = ["c.status = ?"]
    params = [STATUS_PENDING]

    if employee_ids is not None:
        ids = [str(e).strip() for e in employee_ids if str(e).strip()]
        if not ids:
            return []
        where.append("c.employee_id IN (%s)" % ",".join("?" * len(ids)))
        params.extend(ids)

    params.append(max(1, min(int(limit or 100), 500)))
    return _query(
        "SELECT c.*, e.name AS employee_name FROM attendance_corrections c "
        "LEFT JOIN employees e ON e.employee_id = c.employee_id "
        "WHERE " + " AND ".join(where) +
        " ORDER BY c.created_at ASC LIMIT ?", tuple(params), path)


# ---------- التحقّق والتطبيق ----------

def _overlaps_existing(conn, employee_id, start, end, exclude_id=None) -> bool:
    """هل تتداخل المدّة مع سجل آخر للموظف نفسه؟"""
    sql = """SELECT 1 FROM attendance_records
              WHERE employee_id = ? AND check_out_at IS NOT NULL
                AND check_in_at < ? AND check_out_at > ?"""
    params = [employee_id, end, start]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def _duration_ok(start, end) -> tuple[bool, str]:
    """ترتيب الأوقات دائماً، والحدّ الأقصى فقط إن اعتُمدت سياسة."""
    if end <= start:
        return False, "وقت الانصراف يجب أن يكون بعد وقت الحضور"
    if end > _now():
        return False, "لا يمكن أن يكون الوقت في المستقبل"
    if MAX_SESSION_HOURS is not None:
        hours = (datetime.fromisoformat(end) - datetime.fromisoformat(start)) \
            .total_seconds() / 3600
        if hours > MAX_SESSION_HOURS:
            return False, f"المدّة تتجاوز الحدّ المعتمد ({MAX_SESSION_HOURS} ساعة)"
    return True, ""


def _validate_correction(conn, corr) -> tuple[bool, str]:
    """يتحقّق من قابلية التطبيق. يُقرأ كل شيء من داخل المعاملة.

    النسخة المقروءة قبل BEGIN قد تبطل قبل أن تُستعمل، فلا يُبنى
    قرار عليها.
    """
    kind = corr["kind"]
    emp = (corr["employee_id"] or "").strip()
    want_in = corr["requested_check_in_at"]
    want_out = corr["requested_check_out_at"]

    rec = None
    if corr["record_id"] is not None:
        rec = conn.execute("SELECT * FROM attendance_records WHERE id = ?",
                           (corr["record_id"],)).fetchone()
        if rec is None:
            return False, "السجل المشار إليه لم يعد موجوداً"
        if (rec["employee_id"] or "").strip() != emp:
            return False, "السجل لا يخصّ صاحب الطلب"

    has_open = conn.execute(
        "SELECT id FROM attendance_records WHERE employee_id = ?"
        " AND in_verdict = 'inside' AND check_out_at IS NULL", (emp,)).fetchone()

    if kind == "missing_check_in":
        # السجل الإداري مُغلق دائماً: جلسة مفتوحة تعني «في الموقع
        # الآن» وهي حقيقة يقيسها السياج، والتصحيح يوثّق ماضياً.
        if not (want_in and want_out):
            return False, "يلزم وقتا الحضور والانصراف معاً"
        if has_open:
            return False, "لدى الموظف جلسة مفتوحة؛ أغلقها أو صحّحها قبل إضافة حضور"
        ok, why = _duration_ok(want_in, want_out)
        if not ok:
            return False, why
        if _overlaps_existing(conn, emp, want_in, want_out):
            return False, "يتداخل مع سجل حضور قائم"
        return True, ""

    if kind == "missing_check_out":
        if not want_out:
            return False, "يلزم وقت الانصراف"
        if rec is not None and rec["check_out_at"] is not None:
            return False, "الجلسة مغلقة؛ استخدم wrong_time لتصحيح وقت الانصراف"
        if not has_open:
            return False, "لا توجد جلسة مفتوحة لإغلاقها"
        target = rec if rec is not None else conn.execute(
            "SELECT * FROM attendance_records WHERE id = ?", (has_open["id"],)).fetchone()
        ok, why = _duration_ok(target["check_in_at"], want_out)
        if not ok:
            return False, why
        return True, ""

    if kind == "wrong_time":
        if rec is None:
            return False, "يلزم تحديد السجل المراد تصحيحه"
        if rec["check_out_at"] is None:
            return False, "الجلسة ما زالت مفتوحة؛ سجّل انصرافك بدل التصحيح"
        new_in = want_in or rec["check_in_at"]
        new_out = want_out or rec["check_out_at"]
        if not (want_in or want_out):
            return False, "لم يُحدَّد أي وقت للتصحيح"
        ok, why = _duration_ok(new_in, new_out)
        if not ok:
            return False, why
        if _overlaps_existing(conn, emp, new_in, new_out, exclude_id=rec["id"]):
            return False, "يتداخل مع سجل حضور قائم"
        return True, ""

    if kind == "rejected_location":
        if rec is None:
            return False, "يلزم تحديد المحاولة المرفوضة"
        if rec["in_verdict"] not in (geofence.VERDICT_OUTSIDE, geofence.VERDICT_REJECTED):
            return False, "هذا السجل ليس محاولة مرفوضة"
        if rec["check_out_at"] is not None:
            return False, "عولجت هذه المحاولة بالفعل"
        if not want_out:
            return False, "يلزم وقت الانصراف"
        # البداية من وقت المحاولة الذي كتبه الخادم، لا من الطلب:
        # لحظة المحاولة واقعة مسجَّلة، وقبول بديل لها يفتح باباً بلا حاجة.
        ok, why = _duration_ok(rec["check_in_at"], want_out)
        if not ok:
            return False, why
        return True, ""

    return True, ""      # other: قرار موثَّق بلا أثر آلي


def _apply_correction(conn, corr, now) -> None:
    """يطبّق أثر التصحيح. **داخلية**: تكتب وترفع، ولا تمسّ المعاملة.

    المعاملة ملك review_correction، والتراجع عنها من هنا يُسقط
    عمل المستدعي بلا علمه.

    original_* يُكتب مرّة واحدة بشرط IS NULL، فتصحيحان متتاليان
    لا يمحوان الأصل.
    """
    kind = corr["kind"]
    emp = (corr["employee_id"] or "").strip()
    cid = corr["id"]
    want_in = corr["requested_check_in_at"]
    want_out = corr["requested_check_out_at"]

    if kind == "missing_check_in":
        # in_verdict='manually_added' لا 'inside': ليس جلسة حيّة فلا
        # يدخل الفهرس الفريد. و out_verdict مطلوب لأن الصفّ مُغلق —
        # مُطلِق الاقتران يوجب كتابة الطرفين معاً. طرفاه إداريان كلاهما.
        conn.execute(
            """INSERT INTO attendance_records
                 (employee_id, site_id, check_in_at, check_out_at,
                  in_verdict, in_verdict_reason,
                  out_verdict, out_verdict_reason,
                  corrected_by_correction_id, created_at)
               VALUES (?,NULL,?,?,?,?,?,?,?,?)""",
            (emp, want_in, want_out,
             VERDICT_MANUALLY_ADDED, "أُضيف بقرار إداري معتمَد",
             VERDICT_CORRECTION_CLOSED, "أُضيف بقرار إداري معتمَد",
             cid, now),
        )
        return

    if kind == "missing_check_out":
        target_id = corr["record_id"]
        if target_id is None:
            row = conn.execute(
                "SELECT id FROM attendance_records WHERE employee_id = ?"
                " AND in_verdict = 'inside' AND check_out_at IS NULL", (emp,)).fetchone()
            target_id = row["id"]
        conn.execute(
            """UPDATE attendance_records
                  SET check_out_at = ?, out_verdict = ?, out_verdict_reason = ?,
                      corrected_by_correction_id = ?
                WHERE id = ? AND check_out_at IS NULL""",
            (want_out, VERDICT_CORRECTION_CLOSED, "انصراف معتمَد بتصحيح", cid, target_id),
        )
        return

    if kind == "wrong_time":
        rid = corr["record_id"]
        conn.execute(
            "UPDATE attendance_records SET original_check_in_at = check_in_at"
            " WHERE id = ? AND original_check_in_at IS NULL", (rid,))
        conn.execute(
            "UPDATE attendance_records SET original_check_out_at = check_out_at"
            " WHERE id = ? AND original_check_out_at IS NULL", (rid,))

        sets, params = [], []
        if want_in:
            sets.append("check_in_at = ?")
            params.append(want_in)
        if want_out:
            # الوقت والحكم معاً: السجل الذي أُغلق لحلّ تعارض ينتقل
            # هنا إلى correction_closed فتُحتسب مدّته. انتقال حالة
            # لا إعادة فتح — check_out_at لا يصير NULL لحظةً واحدة.
            sets.append("check_out_at = ?")
            params.append(want_out)
            sets.append("out_verdict = ?")
            params.append(VERDICT_CORRECTION_CLOSED)
            sets.append("out_verdict_reason = ?")
            params.append("وقت معتمَد بتصحيح")
        sets.append("corrected_by_correction_id = ?")
        params.append(cid)
        params.append(rid)
        conn.execute(f"UPDATE attendance_records SET {', '.join(sets)} WHERE id = ?",
                     params)
        return

    if kind == "rejected_location":
        # in_verdict لا يُمسّ. حكم السياج حقيقة مقيسة في لحظة، وقبول
        # المدير قرار إداري لاحق. دمجهما يُفقد السجل قدرته على
        # الإجابة: أكان داخل النطاق، أم قُبل رغم أنه لم يكن؟
        rid = corr["record_id"]
        conn.execute(
            """UPDATE attendance_records
                  SET check_out_at = ?, out_verdict = ?, out_verdict_reason = ?,
                      corrected_by_correction_id = ?
                WHERE id = ? AND check_out_at IS NULL""",
            (want_out, VERDICT_CORRECTION_CLOSED,
             "قبول إداري لمحاولة رفضها السياج", cid, rid),
        )
        return

    # other: لا أثر آلي


def review_correction(correction_id, decision: str, reviewer_id: str,
                      note: str = "", path=None) -> dict:
    """يعتمد أو يرفض طلب تصحيح، ويطبّق أثره في المعاملة نفسها.

    فصل المهام: صاحب الطلب لا يقرّر فيه مهما كان دوره. يُفحص داخل
    المعاملة وقبل أي كتابة، تماماً كما في review_leave_request —
    وللسبب نفسه: سجل المدير داخل نطاقه، فطلبه يظهر في طابوره.

    النطاق يُفحص في الطبقة الأعلى (api/tools) لا هنا، كما يفعل
    org.py: هذه الوحدة تخزّن ولا تعرف شجرة الإدارة.

    القرار والتطبيق في معاملة واحدة: فشل التطبيق يُرجع الطلب إلى
    pending، فلا يبقى قرار بلا أثر ولا أثر بلا قرار.
    """
    decision = str(decision or "").strip().lower()
    if decision not in ("approve", "reject"):
        return {"reviewed": False, "reason": "القرار يجب أن يكون approve أو reject"}

    reviewer_id = (reviewer_id or "").strip()
    if not reviewer_id:
        return {"reviewed": False, "reason": "تعذّر التحقق من هوية المراجع"}

    try:
        cid = int(correction_id)
    except (TypeError, ValueError):
        return {"reviewed": False, "reason": "لا يوجد طلب تصحيح بهذا الرقم"}

    new_status = STATUS_APPROVED if decision == "approve" else STATUS_REJECTED
    now = _now()

    conn = _locked_conn(path)
    try:
        corr = conn.execute(
            "SELECT * FROM attendance_corrections WHERE id = ?", (cid,)).fetchone()
        if corr is None:
            _rollback(conn)
            return {"reviewed": False, "reason": "لا يوجد طلب تصحيح بهذا الرقم"}

        if reviewer_id == (corr["employee_id"] or "").strip():
            _rollback(conn)
            log.warning("منع مراجعة ذاتية لطلب تصحيح #%s من %s", cid, reviewer_id)
            return {"reviewed": False,
                    "reason": "لا يمكنك مراجعة طلبك — يعتمده مديرك"}

        if corr["status"] != STATUS_PENDING:
            _rollback(conn)
            return {"reviewed": False, "reason": "حُسم الطلب بالفعل"}

        if new_status == STATUS_APPROVED:
            ok, why = _validate_correction(conn, corr)
            if not ok:
                _rollback(conn)
                return {"reviewed": False, "reason": why}

        cur = conn.execute(
            """UPDATE attendance_corrections
                  SET status = ?, reviewed_by = ?, decision_note = ?, decided_at = ?
                WHERE id = ? AND status = ?""",
            (new_status, reviewer_id, str(note or "")[:300], now, cid, STATUS_PENDING),
        )
        if cur.rowcount == 0:
            _rollback(conn)
            return {"reviewed": False, "reason": "حُسم الطلب بالفعل"}

        if new_status == STATUS_APPROVED:
            _apply_correction(conn, corr, now)

        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()

    log.info("طلب تصحيح #%s → %s بواسطة %s", cid, new_status, reviewer_id)
    return {"reviewed": True, "correction_id": cid, "status": new_status,
            "decided_at": now, "reviewed_by": reviewer_id}
