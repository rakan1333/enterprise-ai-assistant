"""ينشئ حسابات الموظفين بكلمات مرور عشوائية.

التوليد العشوائي هو الممارسة الصحيحة: المطوّر لا يختار كلمة المرور،
ولا تُكتب في الكود، ولا تتكرر بين الحسابات. تُطبَع مرة واحدة عند
الإنشاء — ولا يمكن استرجاعها بعدها لأن المخزَّن بصمة لا كلمة.
"""

import argparse
import secrets
import string

import auth

# (اسم الدخول، الرقم الوظيفي، الاسم، الدور)
EMPLOYEES = [
    ("ahmed",  "EMP-001", "أحمد الشمري",   "employee"),
    ("noura",  "EMP-002", "نورة العتيبي",  "manager"),
    ("khalid", "EMP-003", "خالد الدوسري",  "employee"),
    ("sara",   "EMP-004", "سارة القحطاني", "employee"),
    ("fahad",  "EMP-005", "فهد المطيري",   "employee"),
    ("reem",   "EMP-006", "ريم الحربي",    "employee"),
]

# نستبعد المحارف المتشابهة (O/0 · l/1/I) — تُقرأ خطأً عند النقل اليدوي
_ALPHABET = (
    "".join(c for c in string.ascii_letters if c not in "lIO")
    + "".join(c for c in string.digits if c not in "01")
    + "@#$%&*"
)


def generate_password(length: int = 14) -> str:
    """كلمة مرور عشوائية من مصدر مناسب للتشفير.

    secrets لا random — الأخير قابل للتنبؤ إن عُرفت بذرته.
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def seed(length: int = 14) -> list[tuple[str, str, str, str]]:
    auth.init_db()
    created = []

    for username, emp_id, name, role in EMPLOYEES:
        password = generate_password(length)
        auth.create_user(username, emp_id, name, password, role)
        created.append((username, password, role, name))

    return created


def main() -> None:
    parser = argparse.ArgumentParser(description="إنشاء حسابات الموظفين")
    parser.add_argument("--length", type=int, default=14, help="طول كلمة المرور")
    parser.add_argument(
        "--save",
        metavar="FILE",
        help="حفظ البيانات في ملف (لا يُرفع على Git)",
    )
    args = parser.parse_args()

    created = seed(args.length)

    lines = [
        f"{'اسم الدخول':<12} {'كلمة المرور':<18} {'الدور':<8} الاسم",
        "-" * 66,
    ]
    for username, password, role, name in created:
        label = "مدير" if role == "manager" else "موظف"
        lines.append(f"{username:<12} {password:<18} {label:<8} {name}")

    report = "\n".join(lines)
    print("\nأُنشئت الحسابات التالية:\n")
    print(report)
    print(f"\nالمجموع: {len(created)} حساب")

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"\nحُفظت في: {args.save}")
        print("⚠️ أضف الملف إلى .gitignore — يحوي كلمات مرور صالحة.")

    print(
        "\n⚠️ كلمات المرور تظهر مرة واحدة فقط.\n"
        "   المخزَّن بصمة لا كلمة — لا يمكن استرجاعها لاحقاً.\n"
        "   إعادة تشغيل هذا السكربت تولّد كلمات جديدة وتستبدل القديمة."
    )


if __name__ == "__main__":
    main()
