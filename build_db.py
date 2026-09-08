"""ينشئ قاعدة بيانات تجريبية للموظفين — بيانات وهمية بالكامل."""

import sqlite3
from pathlib import Path

DB_PATH = Path("company.db")

EMPLOYEES = [
    ("EMP-001", "أحمد الشمري", "تقنية المعلومات", "مهندس نظم", 30, 12),
    ("EMP-002", "نورة العتيبي", "الموارد البشرية", "أخصائي توظيف", 30, 5),
    ("EMP-003", "خالد الدوسري", "المالية", "محاسب أول", 30, 22),
    ("EMP-004", "سارة القحطاني", "التسويق", "أخصائي تسويق رقمي", 30, 0),
    ("EMP-005", "فهد المطيري", "تقنية المعلومات", "مطوّر برمجيات", 30, 18),
    ("EMP-006", "ريم الحربي", "العمليات", "منسّق عمليات", 30, 9),
]

DEPARTMENTS = [
    ("تقنية المعلومات", 24, 800),
    ("الموارد البشرية", 8, 800),
    ("المالية", 11, 800),
    ("التسويق", 15, 800),
    ("العمليات", 32, 800),
]


def build(path=DB_PATH) -> None:
    p = Path(path)
    if p.exists():
        p.unlink()

    conn = sqlite3.connect(p)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE employees (
            employee_id   TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            department    TEXT NOT NULL,
            job_title     TEXT NOT NULL,
            annual_leave  INTEGER NOT NULL,
            leave_used    INTEGER NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE departments (
            name              TEXT PRIMARY KEY,
            headcount         INTEGER NOT NULL,
            transport_allowance INTEGER NOT NULL
        )
    """)

    cur.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?)", EMPLOYEES)
    cur.executemany("INSERT INTO departments VALUES (?,?,?)", DEPARTMENTS)

    conn.commit()

    print("عدد الموظفين:", cur.execute("SELECT COUNT(*) FROM employees").fetchone()[0])
    print("عدد الأقسام:", cur.execute("SELECT COUNT(*) FROM departments").fetchone()[0])

    total = cur.execute(
        "SELECT SUM(headcount * transport_allowance) FROM departments"
    ).fetchone()[0]
    print("إجمالي بدل الانتقال:", total)

    conn.close()


if __name__ == "__main__":
    build()