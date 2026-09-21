"""بيانات تجريبية للموظفين والأقسام.

⚠️ هذا السكربت للزرع لا للإقلاع. المخطّط يُنشأ بـ migrations.migrate()،
والإقلاع العادي لا يستدعي هذا الملف إطلاقاً.

سابقاً كان يحذف القاعدة ويعيد بناءها بلا سؤال — فتضيع الطلبات
والحسابات والإحصاءات. الآن الحذف يتطلب --force وتأكيداً صريحاً.
"""

import argparse
import sqlite3
from pathlib import Path

import migrations
from logging_config import get_logger

log = get_logger(__name__)

DB_PATH = Path("company.db")

EMPLOYEES = [
    ("EMP-001", "أحمد الشمري",   "تقنية المعلومات", "مهندس نظم",         30, 12),
    ("EMP-002", "نورة العتيبي",  "الموارد البشرية", "أخصائي توظيف",      30, 5),
    ("EMP-003", "خالد الدوسري",  "المالية",         "محاسب أول",         30, 22),
    ("EMP-004", "سارة القحطاني", "التسويق",         "أخصائي تسويق رقمي", 30, 0),
    ("EMP-005", "فهد المطيري",   "تقنية المعلومات", "مطوّر برمجيات",     30, 18),
    ("EMP-006", "ريم الحربي",    "العمليات",        "منسّق عمليات",      30, 9),
]

DEPARTMENTS = [
    ("تقنية المعلومات", 24, 800),
    ("الموارد البشرية",  8, 800),
    ("المالية",         11, 800),
    ("التسويق",         15, 800),
    ("العمليات",        32, 800),
]


def _db(path=None) -> Path:
    """يحلّ مسار القاعدة عند الاستدعاء لا عند الاستيراد.

    ربطه كقيمة افتراضية يعني أن إعادة تعيين DB_PATH بعد الاستيراد
    بلا أثر — فتعمل الوحدة على قاعدة غير المقصودة بصمت.
    """
    return Path(path) if path else DB_PATH


def _count(path, table) -> int:
    conn = sqlite3.connect(_db(path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def has_data(path=None) -> bool:
    """هل تحوي القاعدة بيانات تشغيل يجب ألا تُمسّ؟"""
    p = _db(path)
    if not p.exists():
        return False
    return any(_count(p, t) for t in ("employees", "leave_requests", "users"))


def build(path=None, *, force: bool = False) -> None:
    """ينشئ المخطّط ويزرع البيانات التجريبية.

    آمن للتشغيل المتكرر: لا يحذف شيئاً ولا يكرّر صفوفاً.
    force=True يحذف القاعدة كاملة — للتطوير فقط.
    """
    p = _db(path)

    if force and p.exists():
        log.warning("حذف تدميري للقاعدة: %s", p)
        p.unlink()

    migrations.migrate(p)

    conn = sqlite3.connect(p)
    try:
        # أعمدة مسمّاة لا ترتيبية: إضافة عمود في هجرة لاحقة
        # كانت تكسر الإدراج الترتيبي بصمت.
        conn.executemany(
            """INSERT OR IGNORE INTO employees
               (employee_id, name, department, job_title, annual_leave, leave_used)
               VALUES (?,?,?,?,?,?)""",
            EMPLOYEES,
        )
        conn.executemany(
            """INSERT OR IGNORE INTO departments
               (name, headcount, transport_allowance)
               VALUES (?,?,?)""",
            DEPARTMENTS,
        )

        # الزرع يسبق تعبئة 005 حين تُنشأ القاعدة من الصفر،
        # فنعيد تطبيق الربط بعد إدراج الصفوف.
        conn.commit()
        conn.commit()
    finally:
        conn.close()

    # الربط بالأقسام يحدث داخل الهجرة؛ نعيد تشغيلها لتلتقط الصفوف المزروعة
    migrations.m005_organization_structure_backfill(p)

    log.info(
        "القاعدة جاهزة: %d موظفاً · %d قسماً · %d طلباً",
        _count(p, "employees"), _count(p, "departments"), _count(p, "leave_requests"),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="زرع بيانات تجريبية")
    ap.add_argument("--force", action="store_true",
                    help="⚠️ يحذف القاعدة كاملة ويعيد بناءها")
    args = ap.parse_args()

    if args.force:
        print("⚠️ سيُحذف كل شيء: الطلبات · الحسابات · الإحصاءات · الهجرات")
        if input("اكتب yes للمتابعة: ").strip().lower() != "yes":
            raise SystemExit("أُلغيت العملية.")
    elif has_data():
        print("القاعدة تحوي بيانات — سنكمل الناقص فقط دون مسّ الموجود.")

    build(force=args.force)
    print(f"عدد الموظفين: {_count(DB_PATH, 'employees')}")
    print(f"عدد الأقسام:  {_count(DB_PATH, 'departments')}")
if __name__ == "__main__":
    main()
