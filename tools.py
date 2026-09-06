"""أدوات الوكيل — كل دالة أداة يمكن للنموذج استدعاؤها."""

import sqlite3
from pathlib import Path

from logging_config import get_logger

log = get_logger(__name__)
DB_PATH = Path("company.db")


def _query(sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------- الأدوات ----------

def get_employee(name: str) -> dict:
    """يبحث عن موظف بالاسم (أو جزء منه) ويعيد بياناته."""
    log.info("أداة get_employee | الاسم: %s", name)
    rows = _query(
        "SELECT * FROM employees WHERE name LIKE ?", (f"%{name}%",)
    )
    if not rows:
        return {"found": False, "message": f"لا يوجد موظف بالاسم: {name}"}
    if len(rows) > 1:
        return {
            "found": True,
            "multiple": True,
            "matches": [r["name"] for r in rows],
            "message": "عدة تطابقات — حدّد الاسم أكثر",
        }
    return {"found": True, "employee": rows[0]}


def get_leave_balance(name: str) -> dict:
    """يحسب رصيد الإجازة المتبقي لموظف."""
    log.info("أداة get_leave_balance | الاسم: %s", name)
    result = get_employee(name)
    if not result.get("found") or result.get("multiple"):
        return result
    emp = result["employee"]
    return {
        "found": True,
        "name": emp["name"],
        "annual_leave": emp["annual_leave"],
        "leave_used": emp["leave_used"],
        "remaining": emp["annual_leave"] - emp["leave_used"],
    }


def list_department_employees(department: str) -> dict:
    """يعيد قائمة موظفي قسم معيّن."""
    log.info("أداة list_department_employees | القسم: %s", department)
    rows = _query(
        "SELECT name, job_title FROM employees WHERE department LIKE ?",
        (f"%{department}%",),
    )
    return {"department": department, "count": len(rows), "employees": rows}


def department_summary() -> dict:
    """يعيد ملخّصاً لكل الأقسام مع الإجماليات الدقيقة."""
    log.info("أداة department_summary")
    rows = _query("""
        SELECT name, headcount, transport_allowance,
               headcount * transport_allowance AS monthly_total
        FROM departments
        ORDER BY name
    """)
    total = sum(r["monthly_total"] for r in rows)
    return {
        "departments": rows,
        "total_headcount": sum(r["headcount"] for r in rows),
        "total_monthly_transport": total,
    }


def calculate(expression: str) -> dict:
    """يحسب تعبيراً رياضياً بسيطاً — أرقام وعمليات فقط."""
    log.info("أداة calculate | التعبير: %s", expression)
    allowed = set("0123456789+-*/(). ")
    if not set(expression) <= allowed:
        return {"error": "التعبير يحتوي محارف غير مسموحة"}
    try:
        return {"expression": expression, "result": eval(expression, {"__builtins__": {}}, {})}
    except Exception as e:
        return {"error": f"تعذّر الحساب: {e}"}
        