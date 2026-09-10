import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from logging_config import get_logger
log = get_logger(__name__)
DB_PATH = Path("company.db")

def _query(sql, params=()):
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    try: return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally: conn.close()

def get_employee(name):
    log.info("أداة get_employee | الاسم: %s", name)
    rows = _query("SELECT * FROM employees WHERE name LIKE ?", (f"%{name}%",))
    if not rows: return {"found": False, "message": f"لا يوجد موظف بالاسم: {name}"}
    if len(rows) > 1:
        return {"found": True, "multiple": True, "matches": [r["name"] for r in rows],
                "message": "عدة تطابقات — حدّد الاسم أكثر"}
    return {"found": True, "employee": rows[0]}

def get_leave_balance(name):
    log.info("أداة get_leave_balance | الاسم: %s", name)
    res = get_employee(name)
    if not res.get("found") or res.get("multiple"): return res
    e = res["employee"]
    return {"found": True, "name": e["name"], "annual_leave": e["annual_leave"],
            "leave_used": e["leave_used"], "remaining": e["annual_leave"] - e["leave_used"]}

def list_department_employees(department):
    log.info("أداة list_department_employees | القسم: %s", department)
    rows = _query("SELECT name, job_title FROM employees WHERE department LIKE ?", (f"%{department}%",))
    return {"department": department, "count": len(rows), "employees": rows}

def department_summary():
    log.info("أداة department_summary")
    rows = _query("""SELECT name, headcount, transport_allowance,
        headcount * transport_allowance AS monthly_total FROM departments ORDER BY name""")
    return {"departments": rows, "total_headcount": sum(r["headcount"] for r in rows),
            "total_monthly_transport": sum(r["monthly_total"] for r in rows)}

def calculate(expression):
    log.info("أداة calculate | التعبير: %s", expression)
    allowed = set("0123456789+-*/(). ")
    if not set(expression) <= allowed:
        return {"error": "التعبير يحتوي محارف غير مسموحة"}
    try:
        return {"expression": expression, "result": eval(expression, {"__builtins__": {}}, {})}
    except Exception as e:
        return {"error": f"تعذّر الحساب: {e}"}


# ---------- طلبات الإجازة ----------
#
# مبدأ حاكم: الأداة التي تقرأ منفصلة عن الأداة التي تكتب.
# preview لا تستطيع الكتابة أصلاً — الأمان بنيوي لا سلوكي.

from datetime import date, timedelta

MAX_REQUEST_DAYS = 30
ADVANCE_NOTICE_DAYS = 7


def _parse_date(value: str) -> date | None:
    """يقبل YYYY-MM-DD فقط — صيغة لا لبس فيها."""
    try:
        y, m, d = (int(p) for p in str(value).strip().split("-"))
        return date(y, m, d)
    except Exception:
        return None


def preview_leave_request(name: str, start_date: str, days: int) -> dict:
    """يتحقق من طلب إجازة ويحسب أثره — دون أي كتابة.

    يُستدعى دائماً قبل التنفيذ ليعرض المستخدم ما سيحدث.
    """
    log.info("أداة preview_leave_request | %s | %s | %s يوم", name, start_date, days)

    emp = get_employee(name)
    if not emp.get("found"):
        return {"valid": False, "reason": emp.get("message", "الموظف غير موجود")}
    if emp.get("multiple"):
        return {"valid": False, "reason": "عدة تطابقات للاسم", "matches": emp["matches"]}

    e = emp["employee"]

    # الطلبات المعلّقة تُحجز من الرصيد — وإلا أمكن تقديم طلبات مجموعها يتجاوزه
    pending = _query(
        """SELECT COALESCE(SUM(days), 0) AS d FROM leave_requests
           WHERE employee_id = ? AND status = 'pending'""",
        (e["employee_id"],),
    )[0]["d"]

    remaining = e["annual_leave"] - e["leave_used"] - pending

    try:
        days = int(days)
    except (TypeError, ValueError):
        return {"valid": False, "reason": "عدد الأيام يجب أن يكون رقماً"}

    if days < 1 or days > MAX_REQUEST_DAYS:
        return {"valid": False, "reason": f"عدد الأيام يجب أن يكون بين 1 و{MAX_REQUEST_DAYS}"}

    start = _parse_date(start_date)
    if start is None:
        return {"valid": False, "reason": "التاريخ غير صالح — استخدم صيغة YYYY-MM-DD"}

    today = date.today()
    if start < today:
        return {"valid": False, "reason": "لا يمكن طلب إجازة بتاريخ ماضٍ"}

    notice = (start - today).days
    end = start + timedelta(days=days - 1)

    warnings = []
    if notice < ADVANCE_NOTICE_DAYS:
        warnings.append(
            f"السياسة تتطلب تقديم الطلب قبل {ADVANCE_NOTICE_DAYS} أيام "
            f"— المتبقي {notice} يوم فقط."
        )

    if days > remaining:
        detail = f"الرصيد لا يكفي — المتاح {remaining} يوم والمطلوب {days}"
        if pending:
            detail += f" (منها {pending} يوم محجوزة لطلبات معلّقة)"
        return {
            "valid": False,
            "reason": detail,
            "remaining_before": remaining,
            "pending_days": pending,
        }

    return {
        "valid": True,
        "employee_id": e["employee_id"],
        "employee_name": e["name"],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "days": days,
        "remaining_before": remaining,
        "remaining_after": remaining - days,
        "pending_days": pending,
        "warnings": warnings,
        "note": "هذي معاينة فقط — لم يُسجَّل أي طلب بعد.",
    }


def submit_leave_request(
    name: str, start_date: str, days: int, reason: str = ""
) -> dict:
    """يسجّل طلب إجازة فعلياً.

    لا يُستدعى إلا بعد معاينة وتأكيد صريح من المستخدم.
    يعيد التحقق كاملاً — لا يثق بأن المعاينة تمّت.
    """
    log.info("أداة submit_leave_request | %s | %s | %s يوم", name, start_date, days)

    check = preview_leave_request(name, start_date, days)
    if not check.get("valid"):
        log.warning("رُفض طلب إجازة: %s", check.get("reason"))
        return {"submitted": False, "reason": check.get("reason")}

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            """INSERT INTO leave_requests
               (employee_id, employee_name, start_date, end_date, days, reason,
                status, created_at)
               VALUES (?,?,?,?,?,?,'pending',?)""",
            (
                check["employee_id"],
                check["employee_name"],
                check["start_date"],
                check["end_date"],
                check["days"],
                str(reason)[:200],
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        request_id = cur.lastrowid
    finally:
        conn.close()

    log.info("سُجّل طلب إجازة #%d لـ %s", request_id, check["employee_name"])

    return {
        "submitted": True,
        "request_id": request_id,
        "employee_name": check["employee_name"],
        "start_date": check["start_date"],
        "end_date": check["end_date"],
        "days": check["days"],
        "status": "pending",
        "remaining_after": check["remaining_after"],
    }


def list_leave_requests(name: str = "") -> dict:
    """يعرض طلبات الإجازة — لموظف محدد أو كلها."""
    log.info("أداة list_leave_requests | %s", name or "الكل")

    if name:
        rows = _query(
            """SELECT id, employee_name, start_date, end_date, days, status
               FROM leave_requests WHERE employee_name LIKE ?
               ORDER BY id DESC LIMIT 20""",
            (f"%{name}%",),
        )
    else:
        rows = _query(
            """SELECT id, employee_name, start_date, end_date, days, status
               FROM leave_requests ORDER BY id DESC LIMIT 20"""
        )

    return {"count": len(rows), "requests": rows}


def cancel_leave_request(request_id: int, name: str = "") -> dict:
    """يلغي طلب إجازة معلّقاً.

    الطلبات المعتمدة لا تُلغى من هنا — تحتاج قراراً إدارياً.
    فحص الملكية: لا يلغي موظف طلب موظف آخر.
    """
    log.info("أداة cancel_leave_request | #%s | %s", request_id, name)

    try:
        request_id = int(request_id)
    except (TypeError, ValueError):
        return {"cancelled": False, "reason": "رقم الطلب يجب أن يكون رقماً"}

    rows = _query("SELECT * FROM leave_requests WHERE id = ?", (request_id,))
    if not rows:
        return {"cancelled": False, "reason": f"لا يوجد طلب برقم {request_id}"}

    req = rows[0]

    if name and name.strip() and name.strip() not in req["employee_name"]:
        log.warning(
            "محاولة إلغاء طلب موظف آخر: %s ≠ %s", name, req["employee_name"]
        )
        return {"cancelled": False, "reason": "لا يمكنك إلغاء طلب موظف آخر"}

    if req["status"] != "pending":
        return {
            "cancelled": False,
            "reason": f"الطلب في حالة '{req['status']}' — لا يمكن إلغاؤه",
        }

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE leave_requests SET status = 'cancelled' WHERE id = ?",
            (request_id,),
        )
        conn.commit()
    finally:
        conn.close()

    log.info("أُلغي الطلب #%d لـ %s", request_id, req["employee_name"])
    return {
        "cancelled": True,
        "request_id": request_id,
        "employee_name": req["employee_name"],
        "days_returned": req["days"],
    }
