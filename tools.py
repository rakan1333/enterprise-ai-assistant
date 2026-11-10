import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from logging_config import get_logger
log = get_logger(__name__)
DB_PATH = Path("company.db")

# أدوار يتجاوز اطلاعها سجل الفاعل نفسه.
ELEVATED_ROLES = ("manager", "admin")


def _is_elevated(actor) -> bool:
    """هل يملك الفاعل اطلاعاً يتجاوز بياناته الشخصية؟"""
    return bool(actor) and actor.get("role") in ELEVATED_ROLES


def _scope(actor) -> str:
    """نطاق الفاعل: سجله · فريقه · المؤسسة."""
    import auth
    return auth.scope_for(actor)


def _visible_employee_ids(actor) -> set[str] | None:
    """المعرّفات التي يحق للفاعل الاطلاع عليها.

    None تعني بلا حدّ (مسؤول أو استدعاء داخلي) — نتجنّب بذلك
    تحميل مئات المعرّفات بلا داع.

    المدير يرى شجرته كاملة لا تقاريره المباشرة فقط: مدير القسم
    يدير رئيس فريق، ورئيس الفريق يدير أعضاءه — وحصره في
    المباشرين يُخفي عنه نصف قسمه.
    """
    scope = _scope(actor)
    if scope == "org":
        return None

    actor_id = (actor.get("employee_id") or "").strip() if actor else ""
    if not actor_id:
        return set()

    if scope == "self":
        return {actor_id}

    import org
    org.DB_PATH = DB_PATH        # نفس القاعدة التي تعمل عليها الأدوات
    return {actor_id, *org.subordinate_ids(actor_id, path=DB_PATH)}


def _team_ids(actor) -> set[str] | None:
    """شجرة تقارير المدير — دون المدير نفسه.

    للعروض الجماعية (قائمة القسم، ملخّصه، القرارات): "فريقي" هم
    من أديرهم. مدير بلا تقارير يحصل على مجموعة فارغة — لا تراجع
    إلى المؤسسة ولا إلى القسم.

    None = بلا حد (مسؤول أو استدعاء داخلي). المجموعة الفارغة
    للموظف تعني: لا فريق.
    """
    scope = _scope(actor)
    if scope == "org":
        return None
    if scope == "self":
        return set()

    actor_id = (actor.get("employee_id") or "").strip() if actor else ""
    if not actor_id:
        return set()

    import org
    org.DB_PATH = DB_PATH
    return set(org.subordinate_ids(actor_id, path=DB_PATH))


def _can_view(actor, employee_id: str) -> bool:
    """هل يحق للفاعل رؤية سجل هذا الموظف؟"""
    allowed = _visible_employee_ids(actor)
    return allowed is None or (employee_id or "").strip() in allowed


def _visible_department_ids(actor) -> set[str] | None:
    """الأقسام ضمن نطاق الفاعل. None تعني الجميع."""
    scope = _scope(actor)
    if scope == "org":
        return None
    if scope == "self":
        return set()

    actor_id = (actor.get("employee_id") or "").strip() if actor else ""
    if not actor_id:
        return set()

    import org
    org.DB_PATH = DB_PATH
    return set(org.managed_department_ids(actor_id, path=DB_PATH))


def _owns(actor: dict, employee_id: str, employee_name: str = "") -> bool:
    """هل يملك الفاعل حق الوصول لسجل هذا الموظف؟

    الفحص بالمعرّف الوظيفي — مستقر وفريد. الأسماء تتقاطع:
    موظف مسجّل باسم "خالد" كان يجتاز فحص الاحتواء الجزئي
    على "خالد المطيري". المعرّف لا يتقاطع.

    الفحص هنا لا في الواجهة — إخفاء زر ليس حماية،
    والـ API يبقى مكشوفاً لمن يعرف عنوانه.
    """
    if not actor:
        return True                      # استدعاء داخلي: سكربت أو اختبار

    actor_id = (actor.get("employee_id") or "").strip()
    if actor_id:
        # النطاق يقرّر: الموظف سجله، والمدير شجرته، والمسؤول الجميع.
        return _can_view(actor, employee_id)

    # هوية بلا معرّف — مطابقة اسم تامّة لا احتواء جزئي.
    # لا يحدث في المسار المصادَق: الرمز يحمل employee_id دائماً.
    return (actor.get("name") or "").strip() == (employee_name or "").strip()


def _query(sql, params=()):
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    try: return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally: conn.close()

# رسالة واحدة لـ"غير موجود" و"غير مسموح": تمييزهما يتيح لأي موظف
# أن يعدّد زملاءه بتخمين الأسماء (هذا موجود، وذاك لا).
NOT_IN_SCOPE = "لا يوجد موظف ضمن نطاقك بهذا الاسم"


def get_employee(name, actor=None):
    """actor: هوية المستخدم الطالب. None يعني استدعاءً داخلياً.

    الاسم يُستخدم للبحث فقط. الصلاحية تُفحص على السجل الموجود
    بمعرّفه الوظيفي — لا على النص المُدخَل.

    خارج نطاق المؤسسة: التطابقات تُضيَّق إلى المسموح قبل أي رد، ولا
    يُكشف وجود من خارجه — لا باسم ولا بعدد ولا برسالة مختلفة.
    """
    log.info("أداة get_employee | الاسم: %s", name)

    rows = _query("SELECT * FROM employees WHERE name LIKE ?", (f"%{name}%",))
    scoped = actor is not None and _scope(actor) != "org"

    if scoped:
        # التضييق قبل كل شيء — لا اسم خارج النطاق يغادر هذي الدالة
        rows = [r for r in rows if _owns(actor, r["employee_id"], r["name"])]
        if not rows:
            log.warning("لا نتيجة ضمن النطاق: %s سأل عن %s", actor.get("name"), name)
            return {"found": False, "message": NOT_IN_SCOPE}
    elif not rows:
        return {"found": False, "message": f"لا يوجد موظف بالاسم: {name}"}

    if len(rows) > 1:
        return {"found": True, "multiple": True,
                "matches": [r["name"] for r in rows],
                "message": "عدة تطابقات — حدّد الاسم أكثر"}

    return {"found": True, "employee": rows[0]}

def get_leave_balance(name, actor=None):
    log.info("أداة get_leave_balance | الاسم: %s", name)
    res = get_employee(name, actor)
    if not res.get("found") or res.get("multiple"): return res
    e = res["employee"]
    return {"found": True, "name": e["name"], "annual_leave": e["annual_leave"],
            "leave_used": e["leave_used"], "remaining": e["annual_leave"] - e["leave_used"]}

def list_department_employees(department, actor=None):
    """موظفو قسم — لأصحاب الصلاحية الإدارية فقط.

    كانت هذي الأداة بلا معامل actor وبلا فحص، و agent.py يستدعيها
    دون هوية. فأي موظف مصادَق يسأل المساعد عن أي قسم فيحصل على
    أسماء زملائه ومسمياتهم.

    الفحص هنا لا في الواجهة: المسار عبر /agent و /chat مكشوف
    لأي عميل يحمل رمزاً صالحاً.
    """
    log.info("أداة list_department_employees | القسم: %s", department)

    if actor and not _is_elevated(actor):
        log.warning(
            "منع عرض موظفي قسم | الطالب: %s (%s) | القسم: %s",
            actor.get("name"), actor.get("role"), department,
        )
        return {
            "error": "عرض موظفي قسم يتطلب صلاحية إدارية",
            "department": department,
            "count": 0,
            "employees": [],
        }

    # المدير: القسم المطلوب ∩ شجرة تقاريره. عضوية القسم وحدها
    # لا تمنح وصولاً — مديران في قسم واحد لا يرى أحدهما فريق الآخر.
    team = _team_ids(actor)

    if team is None:
        rows = _query(
            "SELECT employee_id, name, job_title FROM employees"
            " WHERE department LIKE ?", (f"%{department}%",)
        )
    elif not team:
        return {
            "error": "لا فريق لك في هذا القسم",
            "department": department, "count": 0, "employees": [],
        }
    else:
        marks = ",".join("?" for _ in team)
        rows = _query(
            f"SELECT employee_id, name, job_title FROM employees"
            f" WHERE department LIKE ? AND employee_id IN ({marks})",
            (f"%{department}%", *sorted(team)),
        )
        if not rows:
            return {
                "error": "هذا القسم خارج نطاقك",
                "department": department, "count": 0, "employees": [],
            }

    employees = [{"name": r["name"], "job_title": r["job_title"]} for r in rows]
    return {"department": department, "count": len(employees), "employees": employees}

def department_summary(actor=None):
    """ملخّص الأقسام: الأعداد والبدلات والإجماليات — لأصحاب الصلاحية فقط.

    كانت بلا فحص أيضاً: تكشف بنية البدلات وأعداد كل قسم
    وإجمالي الصرف الشهري لأي موظف مصادَق.
    """
    log.info("أداة department_summary")

    if actor and not _is_elevated(actor):
        log.warning(
            "منع ملخّص الأقسام | الطالب: %s (%s)",
            actor.get("name"), actor.get("role"),
        )
        return {
            "error": "ملخّص الأقسام يتطلب صلاحية إدارية",
            "departments": [],
            "total_headcount": 0,
            "total_monthly_transport": 0,
        }

    team = _team_ids(actor)

    if team is None:
        # المسؤول والاستدعاء الداخلي: كل الأقسام، والعدد من الموظفين
        # النشطين الفعليين — لا عمود headcount المخزّن الذي ابتعد عن
        # الواقع. نفس المصدر الذي يحسب به المدير، فلا رقمان لقسم واحد.
        rows = _query(
            """SELECT d.name AS name,
                      COUNT(e.employee_id) AS headcount,
                      d.transport_allowance AS transport_allowance,
                      COUNT(e.employee_id) * d.transport_allowance AS monthly_total
                 FROM departments d
                 LEFT JOIN employees e
                        ON e.department_id = d.department_id
                       AND COALESCE(e.employment_status, 'active') = 'active'
                GROUP BY d.department_id
                ORDER BY d.name"""
        )
    elif not team:
        # مدير بلا تقارير: صفر — لا تراجع إلى القسم ولا المؤسسة
        rows = []
    else:
        # المدير: العدد من فريقه الفعلي لا عمود القسم المخزّن،
        # فلا يتسرّب إليه حجم فريق زميله داخل نفس القسم.
        marks = ",".join("?" for _ in team)
        rows = _query(
            f"""SELECT d.name AS name,
                       COUNT(e.employee_id) AS headcount,
                       d.transport_allowance AS transport_allowance,
                       COUNT(e.employee_id) * d.transport_allowance AS monthly_total
                  FROM employees e
                  JOIN departments d ON d.department_id = e.department_id
                 WHERE e.employee_id IN ({marks})
                   AND COALESCE(e.employment_status, 'active') = 'active'
                 GROUP BY d.department_id
                 ORDER BY d.name""",
            tuple(sorted(team)),
        )

    return {"departments": rows,
            "total_headcount": sum(r["headcount"] for r in rows),
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


def requests_for_employee(employee_id: str) -> list[dict]:
    """طلبات موظف بعينه بمعرّفه — للمسار /me مهما كان دور صاحبه.

    الدور لا يوسّع نطاق "بياناتي": المدير في صفحته الشخصية
    يرى طلباته هو، لا طلبات فريقه.
    """
    rows = _query(
        """SELECT r.id, r.employee_name, r.start_date, r.end_date, r.days, r.status,
                  COALESCE(r.reason, '')        AS reason,
                  COALESCE(r.decision_note, '') AS decision_note,
                  COALESCE(r.decided_at, '')    AS decided_at,
                  COALESCE(e.name, '')          AS decided_by_name,
                  r.created_at
           FROM leave_requests r
           LEFT JOIN employees e ON e.employee_id = r.decided_by
           WHERE r.employee_id = ?
           ORDER BY r.id DESC LIMIT 50""",
        (employee_id,),
    )

    for r in rows:
        # النظام يدعم الإجازة السنوية فقط — الحقل صريح لا مخترَع
        r["leave_type"] = "annual"
        # ملاحظة القرار تُعرض عند الرفض فقط؛ الاعتماد لا يحتاج تبريراً
        r["rejection_reason"] = r["decision_note"] if r["status"] == "rejected" else ""
        r["can_cancel"] = r["status"] == "pending"

    return rows


def employee_by_id(employee_id: str) -> dict | None:
    """سجل موظف بمعرّفه — الهوية الحاكمة، لا الاسم."""
    rows = _query("SELECT * FROM employees WHERE employee_id = ?", (employee_id,))
    return rows[0] if rows else None


def leave_summary_for(employee_id: str) -> dict | None:
    """ملخّص رصيد موظف بمعرّفه.

    المتاح محسوب لا مخزّن: الطلب المعلّق حجز لا خصم، فالرفض
    والإلغاء يحرّرانه تلقائياً بلا عملية عكسية.
    """
    emp = employee_by_id(employee_id)
    if emp is None:
        return None

    pending = pending_days_for(employee_id)
    annual = emp["annual_leave"]
    used = emp["leave_used"]

    return {
        "employee_id": emp["employee_id"],
        "employee_name": emp["name"],
        "department": emp["department"],
        "annual_leave": annual,
        "leave_used": used,
        "pending_days": pending,
        "available": annual - used - pending,
        "calculation": "المتاح = الرصيد السنوي − المستخدم − المحجوز لطلبات معلّقة",
        "formula": f"{annual} − {used} − {pending} = {annual - used - pending}",
    }


def pending_days_for(employee_id: str) -> int:
    """مجموع أيام الطلبات المعلّقة لموظف — يُستخدم في حساب المتاح."""
    rows = _query(
        """SELECT COALESCE(SUM(days), 0) AS d FROM leave_requests
           WHERE employee_id = ? AND status = 'pending'""",
        (employee_id,),
    )
    return rows[0]["d"]


def preview_leave_request(name: str, start_date: str, days: int, actor=None) -> dict:
    """يتحقق من طلب إجازة ويحسب أثره — دون أي كتابة.

    يُستدعى دائماً قبل التنفيذ ليعرض المستخدم ما سيحدث.
    """
    log.info("أداة preview_leave_request | %s | %s | %s يوم", name, start_date, days)

    # التقديم لنفسك فقط، أياً كان دورك. لو قدّم المدير باسم موظفه ثم
    # اعتمده، صار هو الطالب الفعلي ومتخذ القرار معاً — فيُخصم رصيد
    # الموظف دون أن يطلب. نفس رسالة الرفض سواء وُجد الاسم أم لا.
    if actor is not None:
        own_id = (actor.get("employee_id") or "").strip()
        candidates = _query("SELECT employee_id FROM employees WHERE name LIKE ?", (f"%{name}%",))
        if not own_id or own_id not in {c["employee_id"] for c in candidates}:
            log.warning("منع طلب لغير النفس: %s باسم %s", actor.get("name"), name)
            return {"valid": False,
                    "reason": "لا يمكنك تقديم طلب باسم موظف آخر — يمكنك التقديم لنفسك فقط"}
        own = _query("SELECT name FROM employees WHERE employee_id = ?", (own_id,))
        name = own[0]["name"] if own else name     # الهوية من الرمز لا من النموذج

    emp = get_employee(name)

    if not emp.get("found"):
        return {"valid": False, "reason": emp.get("message", "الموظف غير موجود")}
    if emp.get("multiple"):
        return {"valid": False, "reason": "عدة تطابقات للاسم", "matches": emp["matches"]}

    e = emp["employee"]

    # الطلبات المعلّقة تُحجز من الرصيد — وإلا أمكن تقديم طلبات مجموعها يتجاوزه
    pending = pending_days_for(e["employee_id"])

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
    name: str, start_date: str, days: int, reason: str = "", actor=None
) -> dict:
    """يسجّل طلب إجازة فعلياً.

    لا يُستدعى إلا بعد معاينة وتأكيد صريح من المستخدم.
    يعيد التحقق كاملاً — لا يثق بأن المعاينة تمّت.
    """
    log.info("أداة submit_leave_request | %s | %s | %s يوم", name, start_date, days)

    check = preview_leave_request(name, start_date, days, actor)
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


def list_leave_requests(name: str = "", actor=None) -> dict:
    """يعرض طلبات الإجازة — لموظف محدد أو كلها."""
    log.info("أداة list_leave_requests | %s", name or "الكل")

    # النطاق يحكم: الموظف طلباته، والمدير فريقه، والمسؤول الجميع
    visible = _visible_employee_ids(actor) if actor else None
    if visible is not None and _scope(actor) == "team":
        marks = ",".join("?" for _ in visible) if visible else "''"
        rows = _query(
            f"""SELECT id, employee_name, start_date, end_date, days, status
                  FROM leave_requests WHERE employee_id IN ({marks})
                 ORDER BY id DESC LIMIT 50""",
            tuple(sorted(visible)),
        )
        return {"count": len(rows), "requests": rows}

    if actor and _scope(actor) == "self":
        actor_id = (actor.get("employee_id") or "").strip()
        if actor_id:
            rows = _query(
                """SELECT id, employee_name, start_date, end_date, days, status
                   FROM leave_requests WHERE employee_id = ?
                   ORDER BY id DESC LIMIT 20""",
                (actor_id,),
            )
            return {"count": len(rows), "requests": rows}
        name = actor.get("name", "")

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


def cancel_leave_request(request_id: int, name: str = "", actor=None) -> dict:
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

    if not _owns(actor, req["employee_id"], req["employee_name"]):
        log.warning(
            "منع إلغاء: %s حاول إلغاء طلب %s",
            actor.get("name"), req["employee_name"],
        )
        return {"cancelled": False, "reason": "لا يمكنك إلغاء طلب موظف آخر"}

    if name and name.strip() and name.strip() not in req["employee_name"]:
        log.warning(
            "محاولة إلغاء طلب موظف آخر: %s ≠ %s", name, req["employee_name"]
        )
        return {"cancelled": False, "reason": "لا يمكنك إلغاء طلب موظف آخر"}

    if req["status"] == "approved":
        return {
            "cancelled": False,
            "reason": (
                "الطلب معتمد وخُصمت أيامه من الرصيد — "
                "إلغاؤه يحتاج قراراً إدارياً خارج النظام."
            ),
        }

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


# ---------- مراجعة الطلبات ----------
#
# الاعتماد هو اللحظة الوحيدة التي يتغيّر فيها رصيد الموظف.
# قبله الطلب حجز محسوب، وبعده خصم فعلي مخزّن.

def list_pending_requests(actor=None) -> dict:
    """يعرض الطلبات المعلّقة — للمدير فقط."""
    log.info("أداة list_pending_requests")

    if actor and not _is_elevated(actor):
        return {"error": "مراجعة الطلبات تتطلب صلاحية مدير", "count": 0, "requests": []}

    # المدير يراجع طلبات فريقه؛ المسؤول يراجع الجميع.
    # وطلب المراجِع نفسه ليس في طابوره: لا يستطيع اعتماده (فصل المهام)،
    # فعرضه يوحي بقدرة غير موجودة. يصل طابور مديره الأعلى أو المسؤول.
    visible = _visible_employee_ids(actor)
    self_id = ((actor or {}).get("employee_id") or "").strip()
    if visible is not None:
        visible = visible - {self_id}
        if not visible:
            return {"count": 0, "requests": []}

    if visible is None:
        rows = _query(
            """SELECT r.id, r.employee_id, r.employee_name, r.start_date, r.end_date,
                      r.days, r.reason, r.created_at,
                      e.annual_leave, e.leave_used, e.department
               FROM leave_requests r
               LEFT JOIN employees e ON e.employee_id = r.employee_id
               WHERE r.status = 'pending' AND r.employee_id != ?
               ORDER BY r.id""",
            (self_id,),
        )
    else:
        marks = ",".join("?" for _ in visible)
        rows = _query(
            f"""SELECT r.id, r.employee_id, r.employee_name, r.start_date, r.end_date,
                       r.days, r.reason, r.created_at,
                       e.annual_leave, e.leave_used, e.department
                FROM leave_requests r
                LEFT JOIN employees e ON e.employee_id = r.employee_id
                WHERE r.status = 'pending' AND r.employee_id IN ({marks})
                ORDER BY r.id""",
            tuple(sorted(visible)),
        )

    for r in rows:
        annual = r.get("annual_leave") or 0
        used = r.get("leave_used") or 0
        r["balance_now"] = annual - used
        r["balance_after_approval"] = annual - used - r["days"]

    return {"count": len(rows), "requests": rows}


def recent_decisions(limit: int = 10, actor=None) -> dict:
    """آخر الطلبات المقرَّرة — للمدير فقط.

    لا مصدر قائم يعيد الطلبات المعتمدة أو المرفوضة لغير صاحبها:
    `list_pending_requests` تقتصر على المعلّقة، و`requests_for_employee`
    تقتصر على صاحب الرمز. هذي تسدّ الفجوة بنفس فحص الدور.
    """
    log.info("أداة recent_decisions | الحد: %s", limit)

    if actor and not _is_elevated(actor):
        log.warning("منع عرض القرارات: %s ليس مديراً", actor.get("name"))
        return {"error": "عرض القرارات يتطلب صلاحية مدير", "count": 0, "decisions": []}

    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 10

    # النطاق داخل الاستعلام لا بعده: الحدّ يُطبَّق على ما يحق
    # للمدير رؤيته، ولا صف خارج نطاقه يُجلب أصلاً.
    team = _team_ids(actor)
    if team is not None and not team:
        return {"count": 0, "decisions": []}

    scope_sql, scope_args = "", ()
    if team is not None:
        scope_sql = f" AND r.employee_id IN ({','.join('?' for _ in team)})"
        scope_args = tuple(sorted(team))

    rows = _query(
        f"""SELECT r.id, r.employee_name, r.start_date, r.end_date, r.days,
                  r.status,
                  COALESCE(r.reason, '')        AS reason,
                  COALESCE(r.decision_note, '') AS decision_note,
                  COALESCE(r.decided_at, '')    AS decided_at,
                  COALESCE(e.name, '')          AS decided_by_name,
                  r.created_at
           FROM leave_requests r
           LEFT JOIN employees e ON e.employee_id = r.decided_by
           WHERE r.status IN ('approved', 'rejected'){scope_sql}
           ORDER BY COALESCE(r.decided_at, r.created_at) DESC, r.id DESC
           LIMIT ?""",
        (*scope_args, limit),
    )

    for r in rows:
        r["leave_type"] = "annual"

    return {"count": len(rows), "decisions": rows}


def review_leave_request(
    request_id: int, decision: str, note: str = "", actor=None
) -> dict:
    """يعتمد أو يرفض طلب إجازة معلّقاً — للمدير فقط.

    الاعتماد يخصم الأيام من رصيد الموظف فعلياً.
    الرفض يترك الرصيد كما هو ويحرّر الحجز.
    """
    log.info("أداة review_leave_request | #%s | %s", request_id, decision)

    if actor and not _is_elevated(actor):
        log.warning("منع مراجعة: %s ليس مديراً", actor.get("name"))
        return {"reviewed": False, "reason": "مراجعة الطلبات تتطلب صلاحية مدير"}

    decision = str(decision).strip().lower()
    if decision not in ("approve", "reject"):
        return {"reviewed": False, "reason": "القرار يجب أن يكون approve أو reject"}

    try:
        request_id = int(request_id)
    except (TypeError, ValueError):
        return {"reviewed": False, "reason": "رقم الطلب يجب أن يكون رقماً"}

    rows = _query("SELECT * FROM leave_requests WHERE id = ?", (request_id,))
    if not rows:
        return {"reviewed": False, "reason": f"لا يوجد طلب برقم {request_id}"}

    req = rows[0]

    # فصل المهام: مقدّم الطلب لا يقرّر فيه أبداً — ولا نطاق المؤسسة
    # يتجاوز ذلك. يُفحص قبل النطاق وقبل أي كتابة، فالطلب يبقى كما هو.
    #
    # لماذا كان ممكناً؟ سجل المدير داخل نطاقه (ليرى ملفه)، فكان طلبه
    # يظهر في طابوره ويجتاز فحص النطاق.
    if actor is not None:
        actor_id = (actor.get("employee_id") or "").strip()
        if not actor_id:
            # بلا معرّف لا يمكن إثبات أن المراجع غير صاحب الطلب
            log.warning("منع مراجعة: هوية بلا معرّف (%s)", actor.get("role"))
            return {"reviewed": False, "reason": "تعذّر التحقق من هوية المراجع"}
        if actor_id == (req["employee_id"] or "").strip():
            log.warning("منع اعتماد ذاتي | %s حاول مراجعة طلبه #%d",
                        actor.get("name"), request_id)
            return {"reviewed": False,
                    "reason": "لا يمكنك مراجعة طلبك — يعتمده مديرك"}

    # النطاق يُفحص على الطلب لا على الدور: مدير لا يراجع فريق زميله.
    if not _can_view(actor, req["employee_id"]):
        log.warning(
            "منع مراجعة خارج النطاق | %s حاول مراجعة طلب %s",
            (actor or {}).get("name"), req["employee_name"],
        )
        return {"reviewed": False, "reason": "هذا الطلب خارج نطاق فريقك"}

    if req["status"] != "pending":
        return {
            "reviewed": False,
            "reason": f"الطلب في حالة '{req['status']}' — لا يمكن مراجعته",
        }

    new_status = "approved" if decision == "approve" else "rejected"

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """UPDATE leave_requests
               SET status = ?, decision_note = ?, decided_at = ?, decided_by = ?
               WHERE id = ?""",
            (
                new_status,
                str(note or "")[:300],
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                (actor or {}).get("employee_id", "") if actor else "",
                request_id,
            ),
        )

        # الخصم يحدث عند الاعتماد فقط
        if new_status == "approved":
            conn.execute(
                "UPDATE employees SET leave_used = leave_used + ? WHERE employee_id = ?",
                (req["days"], req["employee_id"]),
            )

        conn.commit()
    finally:
        conn.close()

    emp = _query(
        "SELECT annual_leave, leave_used FROM employees WHERE employee_id = ?",
        (req["employee_id"],),
    )
    remaining = (
        emp[0]["annual_leave"] - emp[0]["leave_used"] if emp else None
    )

    log.warning(
        "قرار على الطلب #%d: %s | %s | %d يوم",
        request_id, new_status, req["employee_name"], req["days"],
    )

    return {
        "reviewed": True,
        "request_id": request_id,
        "employee_name": req["employee_name"],
        "days": req["days"],
        "status": new_status,
        "remaining_balance": remaining,
        "note": note,
    }


# ---------- أدوات "أنا" وهيكل المؤسسة (المرحلة 5، الخطوة 6) ----------
#
# لا تقبل أي معامل من النموذج: الهوية تأتي من actor الذي يحقنه
# الخادم بعد التحقق من الرمز. الموزّع في agent.py يستدعيها بلا
# **args، فما يرسله النموذج (employee_id، manager_id، role، scope)
# لا يصل إليها أصلاً.

_NO_IDENTITY = {"authorized": False, "reason": "تعذّر التحقق من هويتك"}


def _actor_id(actor) -> str:
    return ((actor or {}).get("employee_id") or "").strip()


def get_my_profile(actor=None) -> dict:
    """بياناتي الوظيفية — نفس حقول /auth/me."""
    log.info("أداة get_my_profile")
    me_id = _actor_id(actor)
    if not me_id:
        return dict(_NO_IDENTITY)

    import org
    emp = org.employee_by_id(me_id, path=DB_PATH)
    if not emp:
        return {"found": False, "message": "لا يوجد سجل موظف مرتبط بحسابك"}

    mgr = org.employee_by_id(emp["manager_id"], path=DB_PATH) if emp.get("manager_id") else None
    return {"found": True, "profile": {
        "employee_id": emp["employee_id"],
        "name": emp["name"],
        "role": actor.get("role"),
        "department": emp.get("department"),
        "department_id": emp.get("department_id"),
        "job_title": emp.get("job_title"),
        "manager_id": emp.get("manager_id"),
        "manager_name": mgr["name"] if mgr else None,
        "employment_status": emp.get("employment_status") or "active",
        "email": emp.get("email"),
        "hire_date": emp.get("hire_date"),
    }}


def get_my_manager(actor=None) -> dict:
    """مديري المباشر — الاسم والمسمى والقسم فقط، لا بياناته الشخصية."""
    log.info("أداة get_my_manager")
    me_id = _actor_id(actor)
    if not me_id:
        return dict(_NO_IDENTITY)

    import org
    mgr = org.manager_of(me_id, path=DB_PATH)
    if not mgr:
        return {"has_manager": False, "message": "لا يوجد مدير مُسنَد لك في النظام"}

    return {"has_manager": True, "manager": {
        "employee_id": mgr["employee_id"],
        "name": mgr["name"],
        "job_title": mgr.get("job_title"),
        "department": mgr.get("department"),
    }}


def get_my_team(actor=None) -> dict:
    """تقاريري المباشرة فقط — نفس ما تعيده /me/team.

    الموظف: قائمة فارغة. المدير والمسؤول: من يرفعون له مباشرة،
    لا الشجرة كاملة.
    """
    log.info("أداة get_my_team")
    me_id = _actor_id(actor)
    if not me_id:
        return dict(_NO_IDENTITY)

    members = []
    if _scope(actor) != "self":
        import org
        members = [{
            "employee_id": m["employee_id"],
            "name": m["name"],
            "job_title": m["job_title"],
            "department": m.get("department"),
            "direct_reports": m.get("direct_reports", 0),
        } for m in org.direct_reports_view(me_id, path=DB_PATH)]

    return {"relationship": "direct_reports", "count": len(members), "members": members}


def get_organization(actor=None) -> dict:
    """ملخّص هيكل المؤسسة — للمسؤول وحده.

    ملخّص لا شجرة: الشجرة الكاملة ~31 ألف حرف تزاحم سياق النموذج
    لكل سؤال. التفاصيل على مستوى الموظف متاحة عبر
    list_department_employees المقيّدة بالنطاق.
    """
    log.info("أداة get_organization")
    if actor is not None and _scope(actor) != "org":
        log.warning("منع هيكل المؤسسة: %s (%s)", actor.get("name"), actor.get("role"))
        return {"authorized": False, "reason": "هيكل المؤسسة يتطلب صلاحية مسؤول"}

    import org
    tree = org.organization_tree(path=DB_PATH)

    def leads(nodes):
        for n in nodes:
            if n["reports"]:
                yield {"name": n["name"], "job_title": n["job_title"],
                       "direct_reports": len(n["reports"])}
            yield from leads(n["reports"])

    return {"authorized": True, "totals": tree["totals"], "departments": [{
        "department": d["name"],
        "manager": d.get("manager_name") or None,
        "headcount": d["headcount"],
        "team_leads": list(leads(d["roots"])),
    } for d in tree["departments"]]}


# ===================== نطاق الحضور =====================

def attendance_scope(actor) -> set[str] | None:
    """المعرّفات التي يحقّ للفاعل رؤية حضورها.

    نطاق واحد لا نطاقان: يعيد استخدام _visible_employee_ids ذاتها
    التي تحكم الإجازات والأقسام. إدخال مفهوم ثانٍ للنطاق هنا يعني
    مكانين يجب أن يتفقا، وهما يفترقان عند أول تعديل.

    None = بلا حدّ (مسؤول). المجموعة الفارغة = لا أحد.
    والموظف يحصل على معرّفه وحده، فـ/me/attendance لا تحتاجها
    أصلاً — تستعمل هوية الفاعل مباشرةً.
    """
    return _visible_employee_ids(actor)


# ===================== أدوات الحضور (قراءة فقط) =====================
#
# ثلاث أدوات قراءة، ولا أداة كتابة. check_in و check_out **ليستا**
# أداتَي وكيل بقرار موثَّق:
#
#   في الواجهة يسلّم navigator.geolocation الإحداثيات للمتصفّح
#   مباشرةً. وعبر الوكيل يكتبها المستخدم نصّاً وينقلها النموذج،
#   فيزول الفرق بين «الجهاز أبلغ» و«المستخدم ادّعى» — وهو الفرق
#   الوحيد الذي يقوم عليه فصل in_reported_* عن in_computed_*.
#
#   «سجّل حضوري من 24.7136, 46.6753» تزوير مكتمل بلا أداة اختراق،
#   والإحداثيات متاحة لكل موظف من /sites بحكم التصميم. وكل حارس
#   نصّي يُوضع ضدّه سباق مع صياغات لا تنتهي، فالحدّ الوحيد الذي لا
#   يُلتفّ عليه هو ألّا يوجد المسار.
#
# أثر ذلك: WRITE_TOOLS و WRITE_SUCCESS_KEY في agent.py لا تتغيّران.

MAX_ATTENDANCE_DAYS = 31
_BAD_DAYS = {"authorized": False,
             "reason": f"عدد الأيام يجب أن يكون بين 1 و{MAX_ATTENDANCE_DAYS}"}


def _days_window(days) -> tuple[str, str] | None:
    """يحوّل عدد أيام إلى حدّي نافذة بـ UTC، أو None إن كان خارج المدى.

    النموذج يملك هذا المعامل وحده، فيُقصّ هنا لا في الطبقة الأدنى:
    days=100000 يسحب القاعدة كلها إلى سياق النموذج.
    """
    try:
        n = int(days)
    except (TypeError, ValueError):
        return None
    if not 1 <= n <= MAX_ATTENDANCE_DAYS:
        return None

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return (now - timedelta(days=n)).isoformat(), (now + timedelta(days=1)).isoformat()


def get_my_attendance(days: int = 7, actor=None) -> dict:
    """سجل حضوري — ملخّص بلا إحداثيات.

    الهوية من الجلسة لا من النموذج: لا معامل employee_id في
    التوقيع أصلاً، فلا يوجد ما يُزوَّر.

    الدقائق مفصولة بمصدرها كما في /me/attendance: ما قاسه السياج
    منفصلٌ عمّا اعتمده إنسان. دمجهما في رقم واحد يُفقد الجواب
    معناه حين يُسأل عنه.
    """
    log.info("أداة get_my_attendance")
    me_id = _actor_id(actor)
    if not me_id:
        return dict(_NO_IDENTITY)

    window = _days_window(days)
    if window is None:
        return dict(_BAD_DAYS)

    import attendance
    attendance.DB_PATH = DB_PATH
    start, end = window
    rows = attendance.records_for([me_id], start=start, end=end)
    openrow = attendance.open_session(me_id)

    return {
        "authorized": True,
        "days": int(days),
        "count": len(rows),
        "open_session": None if openrow is None else {
            "check_in_at": openrow["check_in_at"],
            "site_id": openrow["site_id"],
        },
        "totals": attendance.summarize(rows),
        "sessions": [{
            "check_in_at": r["check_in_at"],
            "check_out_at": r["check_out_at"],
            "verdict": r["in_verdict"],
            "closure": r["out_verdict"],
            "duration_minutes": attendance.record_minutes(r),
            "counts_toward_work": attendance.counts_toward_work(r),
        } for r in rows],
    }


def get_team_attendance(days: int = 7, actor=None) -> dict:
    """ملخّص حضور فريقي — للمدير والمسؤول.

    ملخّص لكل موظف لا سجلات مفردة: المدير يحتاج أن يعرف من داوم
    وكم، لا أن يتتبّع دخول موظفه وخروجه ساعةً بساعة. والسجل
    المفصّل متاح له في /team/attendance حين يحتاجه فعلاً.

    بلا إحداثيات ولا دقّة ولا مسافة ولا IP — لا في هذه ولا في تلك.
    """
    log.info("أداة get_team_attendance")
    if actor and not _is_elevated(actor):
        return {"authorized": False,
                "reason": "عرض حضور الفريق يتطلب صلاحية مدير",
                "count": 0, "employees": []}

    window = _days_window(days)
    if window is None:
        return dict(_BAD_DAYS)

    scope_ids = attendance_scope(actor)
    if scope_ids is not None and not scope_ids:
        return {"authorized": True, "days": int(days), "count": 0, "employees": []}

    import attendance
    attendance.DB_PATH = DB_PATH
    start, end = window
    rows = attendance.records_for(scope_ids, start=start, end=end)

    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(r["employee_id"], []).append(r)

    names = attendance.employee_names(grouped.keys())
    employees = [{
        "employee_id": emp,
        "name": names.get(emp),
        "sessions": len(recs),
        "open_now": any(r["check_out_at"] is None for r in recs),
        **attendance.summarize(recs),
    } for emp, recs in sorted(grouped.items())]

    return {"authorized": True, "days": int(days),
            "scope": "org" if scope_ids is None else "team",
            "count": len(employees), "employees": employees}


def list_sites(actor=None) -> dict:
    """مواقع العمل المعتمدة — الاسم والنطاق بلا إحداثيات.

    الواجهة تحصل على الإحداثيات من /sites لأنها ترسم الخريطة
    وتقيس القرب. والنموذج لا يرسم شيئاً، فحجبها هنا بلا تكلفة —
    وإعطاؤه إياها يضع نصّ «المقرّ الرئيسي عند 24.7136, 46.6753»
    في سياق محادثة يقرأها المستخدم.

    المعطّلة للمسؤول وحده، كما في /sites تماماً.
    """
    log.info("أداة list_sites")
    import auth
    import attendance
    attendance.DB_PATH = DB_PATH

    rows = attendance.sites(active_only=not auth.is_admin(actor or {}))
    return {"authorized": True, "count": len(rows), "sites": [{
        "site_id": s["site_id"],
        "name": s["name"],
        "radius_m": s["radius_m"],
        "is_active": bool(s["is_active"]),
    } for s in rows]}
