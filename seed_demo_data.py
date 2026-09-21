"""بيانات تنظيمية تجريبية: أقسام وموظفون وعلاقات إدارية.

لماذا؟ الخطوة التالية تُضيّق صلاحية المدير من المؤسسة كلها إلى
فريقه — ولا يمكن بناء ذلك ولا اختباره على ستة موظفين بلا مديرين.

⚠️ بيانات تجريبية صريحة. لا تُشغَّل على قاعدة إنتاج.

الضمانات:
  حتمي     — بذرة ثابتة، فنفس البنية على أي جهاز
  غير مدمّر — INSERT OR IGNORE، والتشغيل المتكرر لا يكرّر شيئاً
  محافظ    — الموظفون والأقسام والطلبات والحسابات القائمة تبقى

التشغيل:
    python seed_demo_data.py              # يضيف الناقص فقط
    python seed_demo_data.py --scale 0.5  # مؤسسة أصغر
    python seed_demo_data.py --force      # يعيد بناء البيانات التجريبية
"""

import argparse
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import migrations
import org
from logging_config import get_logger

log = get_logger(__name__)

DB_PATH = Path("company.db")

# بذرة ثابتة: نفس المدخلات تعطي نفس المؤسسة دائماً
SEED = 20260501

# الموظفون التجريبيون يبدأون من 100 — فلا يصطدمون بـ EMP-001..006
DEMO_ID_START = 100
ADMIN_ID = "EMP-100"


# ---------- الأقسام ----------
# الخمسة الأولى موجودة أصلاً: نعيد استخدامها لا نكرّرها.

DEPARTMENTS = [
    # (الاسم, بدل الانتقال, عدد الموظفين, عدد المديرين)
    ("تقنية المعلومات",              800, 24, 2),
    ("العمليات",                     800, 22, 2),
    ("الهندسة",                      900, 20, 2),
    ("البيانات والذكاء الاصطناعي",  1000, 16, 1),
    ("إدارة المشاريع",               850, 15, 1),
    ("المالية",                      800, 14, 1),
    ("الصيانة",                      750, 14, 1),
    ("التسويق",                      800, 13, 1),
    ("الأمن السيبراني",             1000, 12, 1),
    ("الموارد البشرية",              800, 11, 1),
    ("المشتريات",                    750, 10, 1),
    ("الشؤون القانونية",             900,  8, 1),
]

MANAGER_TITLES = {
    "تقنية المعلومات":             ["مدير تقنية المعلومات", "رئيس فريق البنية التحتية"],
    "العمليات":                    ["مدير العمليات", "رئيس فريق التشغيل"],
    "الهندسة":                     ["مدير الهندسة", "رئيس فريق التطوير"],
    "البيانات والذكاء الاصطناعي":  ["مدير البيانات والذكاء الاصطناعي"],
    "إدارة المشاريع":              ["مدير إدارة المشاريع"],
    "المالية":                     ["المدير المالي"],
    "الصيانة":                     ["مدير الصيانة"],
    "التسويق":                     ["مدير التسويق"],
    "الأمن السيبراني":             ["مدير الأمن السيبراني"],
    "الموارد البشرية":             ["مدير الموارد البشرية"],
    "المشتريات":                   ["مدير المشتريات"],
    "الشؤون القانونية":            ["المستشار القانوني"],
}

JOB_TITLES = {
    "تقنية المعلومات":             ["مهندس نظم", "مسؤول شبكات", "أخصائي دعم فني",
                                    "مهندس سحابة", "مسؤول قواعد بيانات"],
    "العمليات":                    ["منسّق عمليات", "أخصائي تخطيط", "مشرف تشغيل",
                                    "محلل عمليات"],
    "الهندسة":                     ["مطوّر برمجيات", "مهندس اختبار", "مهندس واجهات",
                                    "مهندس خدمات", "مهندس موثوقية"],
    "البيانات والذكاء الاصطناعي":  ["عالم بيانات", "مهندس بيانات", "مهندس تعلّم آلي",
                                    "محلل بيانات"],
    "إدارة المشاريع":              ["مدير مشروع", "منسّق مشاريع", "محلل أعمال"],
    "المالية":                     ["محاسب أول", "محاسب", "أخصائي ميزانيات",
                                    "مدقّق داخلي"],
    "الصيانة":                     ["فنّي صيانة", "مشرف صيانة", "أخصائي سلامة"],
    "التسويق":                     ["أخصائي تسويق رقمي", "مصمّم جرافيك",
                                    "أخصائي محتوى", "أخصائي علاقات عامة"],
    "الأمن السيبراني":             ["محلل أمن معلومات", "مهندس أمن", "مختبِر اختراق"],
    "الموارد البشرية":             ["أخصائي توظيف", "أخصائي رواتب", "أخصائي تدريب"],
    "المشتريات":                   ["أخصائي مشتريات", "منسّق عقود", "محلل موردين"],
    "الشؤون القانونية":            ["مستشار قانوني مساعد", "باحث قانوني"],
}

FIRST_NAMES = [
    "محمد", "عبدالله", "أحمد", "خالد", "سعد", "فهد", "بندر", "ماجد", "طلال", "نايف",
    "سلطان", "عبدالعزيز", "يوسف", "إبراهيم", "عمر", "زياد", "راكان", "مشعل", "وليد",
    "تركي", "ناصر", "سامي", "هاني", "رائد", "أنس",
    "نورة", "سارة", "ريم", "لمى", "هند", "منى", "عبير", "أسماء", "دانة", "جواهر",
    "الجوهرة", "شهد", "رنا", "مها", "غادة", "أمل", "لطيفة", "موضي", "بشاير", "وعد",
]

FAMILY_NAMES = [
    "الشمري", "العتيبي", "الدوسري", "القحطاني", "المطيري", "الحربي", "الغامدي",
    "الزهراني", "السبيعي", "العنزي", "الرشيدي", "البقمي", "الشهري", "العمري",
    "الخالدي", "الجهني", "المالكي", "الثبيتي", "الصاعدي", "الحازمي", "السهلي",
    "البلوي", "العسيري", "الشريف", "الأنصاري",
]


def _rng():
    return random.Random(SEED)


def _people(rng, count):
    """أسماء فريدة مولّدة حتمياً بتوزيع متوازن.

    الاختيار العشوائي المتكرر يجمّع الاسم الأول: أربعة مديرين
    باسم واحد يبدو خطأ بيانات لا صدفة. نخلط حاصل الضرب الكامل
    بدلاً منه فيتوزّع الاسمان معاً.
    """
    pool = [f"{first} {family}" for first in FIRST_NAMES for family in FAMILY_NAMES]
    rng.shuffle(pool)

    if count > len(pool):
        raise ValueError(f"المطلوب {count} اسماً والمتاح {len(pool)}")
    return pool[:count]


def plan(scale: float = 1.0) -> dict:
    """يبني خطة المؤسسة في الذاكرة — حتمية وقابلة للفحص قبل الكتابة."""
    rng = _rng()

    departments, roster = [], []
    for name, allowance, size, mgr_count in DEPARTMENTS:
        scaled = max(mgr_count + 1, round(size * scale))
        departments.append({"name": name, "transport_allowance": allowance,
                            "size": scaled, "managers": mgr_count})

    total = sum(d["size"] for d in departments)
    names = _people(rng, total + 1)          # +1 للمسؤول
    cursor = DEMO_ID_START + 1               # EMP-100 محجوز للمسؤول

    for dept in departments:
        mgr_titles = MANAGER_TITLES[dept["name"]]
        staff_titles = JOB_TITLES[dept["name"]]

        managers = []
        for i in range(dept["managers"]):
            person = {
                "employee_id": f"EMP-{cursor:03d}",
                "name": names.pop(0),
                "department": dept["name"],
                "job_title": mgr_titles[i % len(mgr_titles)],
                "is_manager": True,
                "manager_index": i,
            }
            managers.append(person)
            roster.append(person)
            cursor += 1

        for j in range(dept["size"] - dept["managers"]):
            # التوزيع بالتناوب: مديران في قسم واحد يقتسمان الفريق،
            # فلا يدير أحدهما كل موظفي قسمه.
            lead = managers[j % len(managers)]
            roster.append({
                "employee_id": f"EMP-{cursor:03d}",
                "name": names.pop(0),
                "department": dept["name"],
                "job_title": staff_titles[j % len(staff_titles)],
                "is_manager": False,
                "reports_to": lead["employee_id"],
            })
            cursor += 1

        dept["manager_ids"] = [m["employee_id"] for m in managers]

    admin = {
        "employee_id": ADMIN_ID,
        "name": "ركان العتيبي",
        "department": "تقنية المعلومات",
        "job_title": "مسؤول النظام",
        "is_manager": False,
        "is_admin": True,
    }

    return {"departments": departments, "roster": roster, "admin": admin,
            "total": len(roster) + 1}


def _hire_date(rng) -> str:
    """تاريخ توظيف تجريبي خلال السنوات الست الماضية."""
    return (date(2020, 1, 1) + timedelta(days=rng.randrange(0, 2100))).isoformat()


def seed(path=None, scale: float = 1.0) -> dict:
    """يزرع البنية التنظيمية. آمن للتكرار."""
    db = Path(path) if path else DB_PATH
    migrations.migrate(db)

    blueprint = plan(scale)
    rng = _rng()
    conn = sqlite3.connect(db)

    try:
        # --- الأقسام: الموجود يبقى، والناقص يُضاف ---
        for dept in blueprint["departments"]:
            conn.execute(
                """INSERT OR IGNORE INTO departments (name, headcount, transport_allowance)
                   VALUES (?,?,?)""",
                (dept["name"], dept["size"], dept["transport_allowance"]),
            )

        # --- الموظفون ---
        rows = []
        for p in blueprint["roster"] + [blueprint["admin"]]:
            rows.append((
                p["employee_id"], p["name"], p["department"], p["job_title"],
                30, rng.randrange(0, 18),
                f"{p['employee_id'].lower().replace('-', '')}@company.example",
                "active", _hire_date(rng),
                date.today().isoformat(),
            ))

        conn.executemany(
            """INSERT OR IGNORE INTO employees
               (employee_id, name, department, job_title, annual_leave, leave_used,
                email, employment_status, hire_date, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    # معرّفات الأقسام للصفوف الجديدة
    migrations.m005_organization_structure_backfill(db)

    # --- العلاقات عبر طبقة الخدمة، فتُفحص قواعدها ---
    assigned = 0
    for p in blueprint["roster"]:
        if p.get("reports_to"):
            org.assign_manager(p["employee_id"], p["reports_to"], path=db)
            assigned += 1

    # مديرو الأقسام: المدير الأول في كل قسم
    dept_by_name = {d["name"]: d for d in org.departments(path=db)}
    for dept in blueprint["departments"]:
        row = dept_by_name.get(dept["name"])
        if row and dept["manager_ids"]:
            org.assign_department_manager(row["department_id"], dept["manager_ids"][0], path=db)

    # --- الموظفون الستة الأصليون ---
    # بياناتهم لا تُمسّ؛ نُسند لهم مديراً فقط بوصفه بيانات تجريبية.
    _link_existing_employees(db, blueprint)

    integrity = migrations.validate_integrity(db)
    total_rows = len(org._query("SELECT employee_id FROM employees", path=db))

    return {
        "departments": len(blueprint["departments"]),
        "seeded_employees": blueprint["total"],
        "total_employees": total_rows,
        "preexisting_employees": total_rows - blueprint["total"],
        "managers": sum(len(d["manager_ids"]) for d in blueprint["departments"]),
        "reports_assigned": assigned,
        "integrity_ok": integrity["ok"],
    }


def _link_existing_employees(db, blueprint) -> None:
    """يربط الموظفين الأصليين بمديري أقسامهم.

    ⚠️ الإسناد بيانات تجريبية: النظام لا يعرف مديرهم الحقيقي.
    لا نلمس أسماءهم ولا أرصدتهم ولا نضيف لهم بريداً أو تاريخ
    توظيف — تلك حقائق لا نملكها.

    نورة العتيبي دورها في `users` مدير أصلاً، فتبقى بلا مدير
    أعلى: هذي حقيقة قائمة لا اختلاق.
    """
    managers_by_dept = {}
    for dept in blueprint["departments"]:
        if dept["manager_ids"]:
            managers_by_dept[dept["name"]] = dept["manager_ids"][0]

    existing = org._query(
        "SELECT employee_id, name, department, manager_id FROM employees"
        " WHERE CAST(SUBSTR(employee_id, 5) AS INTEGER) < ? ORDER BY employee_id",
        (DEMO_ID_START,), db,
    )

    manager_usernames = {
        r["employee_id"] for r in org._query(
            "SELECT employee_id FROM users WHERE role IN ('manager','admin')", path=db)
    }

    for emp in existing:
        if emp["manager_id"]:
            continue
        if emp["employee_id"] in manager_usernames:
            continue                    # مدير قائم — لا نضع فوقه أحداً
        lead = managers_by_dept.get(emp["department"])
        if lead and lead != emp["employee_id"]:
            org.assign_manager(emp["employee_id"], lead, path=db)


def seed_admin_account(path=None, password: str | None = None) -> dict | None:
    """ينشئ حساب المسؤول التجريبي.

    ⚠️ الصلاحية تأتي من `users.role` لا من الاسم. لا سطر في
    النظام يقارن باسم شخص.
    """
    import secrets

    import auth

    if "admin" not in auth.ROLES:
        log.warning("دور admin غير مدعوم في auth.ROLES — تخطّي حساب المسؤول.")
        return None

    db = Path(path) if path else DB_PATH
    auth.init_db(db)

    if auth.user_exists("rakan", db):
        return None

    pw = password or secrets.token_urlsafe(12)
    auth.create_user("rakan", ADMIN_ID, "ركان العتيبي", pw, "admin", db)
    return {"username": "rakan", "password": pw, "role": "admin"}


def purge_demo_rows(path=None) -> int:
    """يحذف الصفوف التجريبية فقط — الأصلية والطلبات تبقى."""
    db = Path(path) if path else DB_PATH
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE employees SET manager_id = NULL"
                     " WHERE manager_id IN (SELECT employee_id FROM employees"
                     "   WHERE CAST(SUBSTR(employee_id, 5) AS INTEGER) >= ?)",
                     (DEMO_ID_START,))
        conn.execute("UPDATE departments SET manager_id = NULL"
                     " WHERE manager_id IN (SELECT employee_id FROM employees"
                     "   WHERE CAST(SUBSTR(employee_id, 5) AS INTEGER) >= ?)",
                     (DEMO_ID_START,))
        cur = conn.execute("DELETE FROM employees"
                           " WHERE CAST(SUBSTR(employee_id, 5) AS INTEGER) >= ?",
                           (DEMO_ID_START,))
        conn.execute("DELETE FROM users WHERE employee_id = ?", (ADMIN_ID,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="زرع بنية تنظيمية تجريبية")
    ap.add_argument("--scale", type=float, default=1.0, help="حجم المؤسسة (1.0 = كامل)")
    ap.add_argument("--force", action="store_true",
                    help="⚠️ يحذف البيانات التجريبية ويعيد بناءها")
    ap.add_argument("--admin-password", help="كلمة مرور المسؤول بدل توليدها")
    args = ap.parse_args()

    if args.force:
        print("⚠️ سيُحذف كل الموظفين التجريبيين (EMP-100 فما فوق) وحساب المسؤول.")
        print("   الموظفون الأصليون والطلبات والحسابات الأخرى تبقى.")
        if input("اكتب yes للمتابعة: ").strip().lower() != "yes":
            raise SystemExit("أُلغيت العملية.")
        removed = purge_demo_rows()
        print(f"حُذف {removed} صفاً تجريبياً.\n")

    result = seed(scale=args.scale)
    account = seed_admin_account(password=args.admin_password)

    print("البنية التنظيمية جاهزة:")
    print(f"  الأقسام:        {result['departments']}")
    print(f"  موظفون مزروعون: {result['seeded_employees']}")
    print(f"  موظفون قائمون:  {result['preexisting_employees']}")
    print(f"  الإجمالي:       {result['total_employees']}")
    print(f"  المديرون:       {result['managers']}")
    print(f"  علاقات إدارية:  {result['reports_assigned']}")
    print(f"  سلامة العلاقات: {'سليمة ✓' if result['integrity_ok'] else '⚠️ فيها مشاكل'}")

    if account:
        print(f"\n  حساب المسؤول: {account['username']} / {account['password']}")
        print("  ⚠️ انسخها الآن — تظهر مرة واحدة.")
    else:
        print("\n  حساب المسؤول موجود مسبقاً أو غير مدعوم.")


if __name__ == "__main__":
    main()
