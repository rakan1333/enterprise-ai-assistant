"""العلاقات التنظيمية: القسم والمدير والتقارير المباشرة.

لماذا وحدة مستقلة؟ لأن هذي علاقات بيانات لا أدوات وكيل ولا نقاط
REST. `tools.py` طبقة أدوات المساعد، و`api.py` طبقة النقل —
وخلط العلاقات بأيٍّ منهما يربك المسؤوليات.

لا مفاتيح أجنبية في SQLite هنا: إضافتها لجدول قائم تتطلب إعادة
بناءه كاملاً، وهي العملية التدميرية التي أُزيلت في المرحلة الثانية.
البديل المتفق عليه: القواعد تُفرَض هنا ويحرسها فحص السلامة
والاختبارات.

⚠️ هذي الوحدة لا تفرض صلاحيات الأدوار. نموذج الصلاحية الثلاثي
يأتي في خطوة لاحقة؛ ما هنا هو سلامة العلاقات فقط.
"""

import sqlite3
from pathlib import Path

from logging_config import get_logger

log = get_logger(__name__)

DB_PATH = Path("company.db")

MAX_CHAIN_DEPTH = 32          # حارس ضد سلسلة إدارية غير منتهية


def _db(path=None) -> Path:
    """يحلّ مسار القاعدة عند الاستدعاء لا عند الاستيراد."""
    return Path(path) if path else DB_PATH


def _query(sql, params=(), path=None):
    conn = sqlite3.connect(_db(path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _one(sql, params=(), path=None):
    rows = _query(sql, params, path)
    return rows[0] if rows else None


class OrgIntegrityError(ValueError):
    """يُرمى عند محاولة إنشاء علاقة غير صالحة."""


# ---------- قراءة ----------

def departments(active_only: bool = True, path=None) -> list[dict]:
    sql = """SELECT department_id, name, headcount, transport_allowance,
                    manager_id, COALESCE(is_active, 1) AS is_active
               FROM departments"""
    if active_only:
        sql += " WHERE COALESCE(is_active, 1) = 1"
    return _query(sql + " ORDER BY department_id", path=path)


def department_by_id(department_id: str, path=None) -> dict | None:
    return _one(
        """SELECT department_id, name, headcount, transport_allowance,
                  manager_id, COALESCE(is_active, 1) AS is_active
             FROM departments WHERE department_id = ?""",
        (department_id,), path,
    )


def employee_by_id(employee_id: str, path=None) -> dict | None:
    return _one("SELECT * FROM employees WHERE employee_id = ?", (employee_id,), path)


def employees_in_department(department_id: str, path=None) -> list[dict]:
    """أعضاء قسم — بالمعرّف لا بالاسم، فإعادة التسمية لا تكسر شيئاً."""
    return _query(
        """SELECT employee_id, name, job_title, manager_id,
                  COALESCE(employment_status, 'active') AS employment_status
             FROM employees WHERE department_id = ? ORDER BY employee_id""",
        (department_id,), path,
    )


def direct_reports(manager_employee_id: str, path=None) -> list[dict]:
    """من يرفع تقاريره لهذا الموظف مباشرة."""
    return _query(
        """SELECT employee_id, name, job_title, department_id,
                  COALESCE(employment_status, 'active') AS employment_status
             FROM employees WHERE manager_id = ? ORDER BY employee_id""",
        (manager_employee_id,), path,
    )


def manager_of(employee_id: str, path=None) -> dict | None:
    """مدير الموظف المباشر، أو None إن لم يُسنَد."""
    emp = employee_by_id(employee_id, path)
    if not emp or not emp.get("manager_id"):
        return None
    return employee_by_id(emp["manager_id"], path)


def management_chain(employee_id: str, path=None) -> list[dict]:
    """سلسلة المديرين صعوداً — تتوقف عند الجذر أو عند حدّ العمق."""
    chain, seen, current = [], {employee_id}, employee_id

    for _ in range(MAX_CHAIN_DEPTH):
        mgr = manager_of(current, path)
        if mgr is None or mgr["employee_id"] in seen:
            break
        chain.append(mgr)
        seen.add(mgr["employee_id"])
        current = mgr["employee_id"]

    return chain


def team_member_ids(manager_employee_id: str, path=None) -> list[str]:
    """معرّفات التقارير المباشرة فقط."""
    return [r["employee_id"] for r in direct_reports(manager_employee_id, path)]


def subordinate_ids(manager_employee_id: str, path=None) -> list[str]:
    """كل من تحت المدير في الشجرة — مباشرين وغير مباشرين.

    مدير القسم يدير رئيس فريق، ورئيس الفريق يدير أعضاءه. حصر
    نطاق مدير القسم في تقاريره المباشرة يُخفي عنه نصف قسمه.

    الاجتياز بعرض الشجرة مع حارس عمق وسجل زيارة: بيانات تالفة
    فيها دورة لا تُدخلنا في حلقة لا نهائية.
    """
    seen, frontier, out = {manager_employee_id}, [manager_employee_id], []

    for _ in range(MAX_CHAIN_DEPTH):
        if not frontier:
            break
        placeholders = ",".join("?" for _ in frontier)
        rows = _query(
            f"SELECT employee_id FROM employees WHERE manager_id IN ({placeholders})",
            tuple(frontier), path,
        )
        frontier = [r["employee_id"] for r in rows if r["employee_id"] not in seen]
        seen.update(frontier)
        out.extend(frontier)

    return out


def manages(manager_employee_id: str, employee_id: str, path=None) -> bool:
    """هل هذا الموظف ضمن شجرة هذا المدير؟"""
    if not manager_employee_id or not employee_id:
        return False
    return employee_id in set(subordinate_ids(manager_employee_id, path))


def managed_department_ids(manager_employee_id: str, path=None) -> list[str]:
    """الأقسام التي يقودها المدير: قسمه إن كان مديره، وأقسام فريقه."""
    out = set()

    for row in _query(
        "SELECT department_id FROM departments WHERE manager_id = ?",
        (manager_employee_id,), path,
    ):
        if row["department_id"]:
            out.add(row["department_id"])

    team = subordinate_ids(manager_employee_id, path)
    if team:
        placeholders = ",".join("?" for _ in team)
        for row in _query(
            f"SELECT DISTINCT department_id FROM employees"
            f" WHERE employee_id IN ({placeholders}) AND department_id IS NOT NULL",
            tuple(team), path,
        ):
            out.add(row["department_id"])

    return sorted(out)


# ---------- قواعد السلامة ----------

def _would_create_cycle(employee_id: str, manager_id: str, path=None) -> bool:
    """هل يصبح الموظف مديراً لنفسه عبر سلسلة؟

    أ يدير ب، وب يدير أ — شجرة مستحيلة تُدخل أي اجتياز في حلقة.
    """
    current, hops = manager_id, 0
    while current and hops < MAX_CHAIN_DEPTH:
        if current == employee_id:
            return True
        row = _one("SELECT manager_id FROM employees WHERE employee_id = ?", (current,), path)
        if not row:
            return False
        current = row["manager_id"]
        hops += 1
    return False


def assign_manager(employee_id: str, manager_id: str | None, path=None) -> dict:
    """يُسند مديراً لموظف بعد فحص القواعد.

    القواعد: الطرفان موجودان · لا إسناد للنفس · لا دورة.
    """
    employee_id = (employee_id or "").strip()
    manager_id = (manager_id or "").strip() or None

    if not employee_by_id(employee_id, path):
        raise OrgIntegrityError(f"لا يوجد موظف بالمعرّف: {employee_id}")

    if manager_id is not None:
        if manager_id == employee_id:
            raise OrgIntegrityError("لا يمكن أن يكون الموظف مديراً لنفسه")
        if not employee_by_id(manager_id, path):
            raise OrgIntegrityError(f"لا يوجد مدير بالمعرّف: {manager_id}")
        if _would_create_cycle(employee_id, manager_id, path):
            raise OrgIntegrityError("هذا الإسناد ينشئ دورة في سلسلة الإدارة")

    conn = sqlite3.connect(_db(path))
    try:
        conn.execute("UPDATE employees SET manager_id = ? WHERE employee_id = ?",
                     (manager_id, employee_id))
        conn.commit()
    finally:
        conn.close()

    log.info("إسناد مدير | %s → %s", employee_id, manager_id or "(بلا)")
    return {"employee_id": employee_id, "manager_id": manager_id}


def assign_department(employee_id: str, department_id: str, path=None) -> dict:
    """ينقل موظفاً لقسم، ويحدّث عمود الاسم معه للتوافق الخلفي."""
    employee_id = (employee_id or "").strip()
    department_id = (department_id or "").strip()

    if not employee_by_id(employee_id, path):
        raise OrgIntegrityError(f"لا يوجد موظف بالمعرّف: {employee_id}")

    dept = department_by_id(department_id, path)
    if not dept:
        raise OrgIntegrityError(f"لا يوجد قسم بالمعرّف: {department_id}")

    conn = sqlite3.connect(_db(path))
    try:
        # الاسم يبقى متزامناً: استعلامات قائمة كثيرة تقرأه
        conn.execute(
            "UPDATE employees SET department_id = ?, department = ? WHERE employee_id = ?",
            (department_id, dept["name"], employee_id),
        )
        conn.commit()
    finally:
        conn.close()

    log.info("نقل موظف | %s → %s (%s)", employee_id, department_id, dept["name"])
    return {"employee_id": employee_id, "department_id": department_id,
            "department": dept["name"]}


def assign_department_manager(department_id: str, manager_id: str | None, path=None) -> dict:
    """يُسند مديراً لقسم."""
    department_id = (department_id or "").strip()
    manager_id = (manager_id or "").strip() or None

    if not department_by_id(department_id, path):
        raise OrgIntegrityError(f"لا يوجد قسم بالمعرّف: {department_id}")
    if manager_id is not None and not employee_by_id(manager_id, path):
        raise OrgIntegrityError(f"لا يوجد موظف بالمعرّف: {manager_id}")

    conn = sqlite3.connect(_db(path))
    try:
        conn.execute("UPDATE departments SET manager_id = ? WHERE department_id = ?",
                     (manager_id, department_id))
        conn.commit()
    finally:
        conn.close()

    return {"department_id": department_id, "manager_id": manager_id}


def department_headcount(department_id: str, path=None) -> int:
    """العدد الفعلي المحسوب — لا عمود headcount المخزّن.

    العمود المخزّن بيانات زرع ثابتة وسيختلف عن الواقع؛ نتركه
    كما هو للتوافق ونحسب الحقيقة عند الحاجة.
    """
    row = _one(
        """SELECT COUNT(*) AS n FROM employees
            WHERE department_id = ? AND COALESCE(employment_status,'active') = 'active'""",
        (department_id,), path,
    )
    return row["n"] if row else 0


# ---------- عروض للواجهات البرمجية ----------
#
# دوال قراءة تجمع العلاقات القائمة في أشكال جاهزة للعرض. لا تفرض
# صلاحية بنفسها: المستدعي يمرّر مجموعة المعرّفات المسموحة المحسوبة
# من نطاق الفاعل، فيبقى قرار الصلاحية في مكان واحد.

ACTIVE = "COALESCE(employment_status, 'active') = 'active'"


def direct_reports_view(manager_employee_id: str, path=None) -> list[dict]:
    """التقارير المباشرة مع اسم القسم وعدد تقارير كل عضو.

    المباشرون فقط — لا الشجرة. العدد يميّز رؤساء الفرق.
    """
    return _query(
        f"""SELECT e.employee_id, e.name, e.job_title,
                   e.department AS department, e.department_id,
                   COALESCE(e.employment_status, 'active') AS employment_status,
                   (SELECT COUNT(*) FROM employees r
                     WHERE r.manager_id = e.employee_id) AS direct_reports
              FROM employees e
             WHERE e.manager_id = ?
             ORDER BY e.employee_id""",
        (manager_employee_id,), path,
    )


def department_views(allowed_ids: set[str] | None, path=None) -> list[dict]:
    """الأقسام مع عدد محسوب من الموظفين الفعليين.

    allowed_ids = None  ← كل الموظفين النشطين (مسؤول)
    allowed_ids = مجموعة ← العدد من داخلها فقط، والأقسام الخالية منها تُحذف

    العمود المخزّن headcount لا يُستعمل: بيانات زرع ثابتة ابتعدت
    عن الواقع (العمليات: 32 مخزّناً مقابل 23 فعلاً).
    """
    if allowed_ids is not None and not allowed_ids:
        return []

    scope_sql, args = "", ()
    if allowed_ids is not None:
        scope_sql = f" AND e.employee_id IN ({','.join('?' for _ in allowed_ids)})"
        args = tuple(sorted(allowed_ids))

    rows = _query(
        f"""SELECT d.department_id, d.name, d.manager_id,
                   COALESCE(m.name, '') AS manager_name,
                   COALESCE(d.is_active, 1) AS is_active,
                   COUNT(e.employee_id) AS headcount
              FROM departments d
              LEFT JOIN employees m ON m.employee_id = d.manager_id
              LEFT JOIN employees e ON e.department_id = d.department_id
                                   AND COALESCE(e.employment_status,'active') = 'active'
                                   {scope_sql}
             GROUP BY d.department_id
             ORDER BY d.department_id""",
        args, path,
    )
    if allowed_ids is not None:
        rows = [r for r in rows if r["headcount"] > 0]
    return rows


def organization_tree(path=None) -> dict:
    """الهيكل الكامل: أقسام ← جذور الرفع ← التقارير المتداخلة.

    استعلام واحد ثم تجميع في الذاكرة. كل موظف يظهر مرة واحدة
    تحت مديره المخزّن، والجذور (بلا مدير) تُجمَع تحت أقسامها.

    موظفون لا يُبلَغون من أي جذر (دورة في بيانات تالفة) لا يُفقَدون
    بصمت — يُعادون في `unplaced` ليراهم المسؤول.
    """
    people = _query(
        """SELECT employee_id, name, job_title, department_id, manager_id,
                  COALESCE(employment_status, 'active') AS employment_status
             FROM employees ORDER BY employee_id""",
        path=path,
    )
    ids = {p["employee_id"] for p in people}
    children: dict[str, list[dict]] = {}
    roots = []
    for p in people:
        mgr = p["manager_id"]
        if mgr and mgr in ids and mgr != p["employee_id"]:
            children.setdefault(mgr, []).append(p)
        else:
            roots.append(p)

    placed = set()

    def build(person, depth=0):
        placed.add(person["employee_id"])
        node = {k: person[k] for k in
                ("employee_id", "name", "job_title", "department_id", "employment_status")}
        node["reports"] = []
        if depth < MAX_CHAIN_DEPTH:
            for child in children.get(person["employee_id"], []):
                if child["employee_id"] not in placed:
                    node["reports"].append(build(child, depth + 1))
        return node

    forest = [build(r) for r in roots]

    by_dept: dict[str | None, list[dict]] = {}
    for node in forest:
        by_dept.setdefault(node["department_id"], []).append(node)

    departments = []
    for d in department_views(None, path):
        departments.append({**d, "roots": by_dept.pop(d["department_id"], [])})

    unassigned = [n for group in by_dept.values() for n in group]
    unplaced = [{"employee_id": p["employee_id"], "name": p["name"]}
                for p in people if p["employee_id"] not in placed]

    return {
        "departments": departments,
        "unassigned": unassigned,
        "unplaced": unplaced,
        "totals": {
            "departments": len(departments),
            "employees": len(people),
            "managers": len(children),
            "reporting_links": sum(len(v) for v in children.values()),
        },
    }
