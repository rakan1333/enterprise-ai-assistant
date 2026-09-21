"""المصادقة والصلاحيات.

ثلاثة مبادئ حاكمة:

1. لا نخزّن كلمات المرور — نخزّن بصمات bcrypt. لو سُرّبت القاعدة
   لا تُستخرج الكلمات، وحتى المطوّر لا يستطيع رؤيتها.

2. الرمز موقّع لا مشفّر. المحتوى ليس سراً؛ المهم ألا يُزوَّر.
   والخادم يبقى بلا حالة — يفك التوقيع ويعرف الهوية دون تخزين جلسات.

3. الصلاحية تُفحص حيث تُنفَّذ العملية، لا في الواجهة.
   إخفاء زر ليس حماية — الـ API يبقى مكشوفاً.
"""

import os
import secrets
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from logging_config import get_logger

log = get_logger(__name__)

DB_PATH = Path("company.db")

ALGORITHM = "HS256"
TOKEN_HOURS = int(os.getenv("TOKEN_HOURS", "8"))
MIN_SECRET_LENGTH = 32

# ملف تطوير محلي — يُولَّد مرة ويُعاد استخدامه، فلا تُبطل الجلسات
# عند كل إعادة تشغيل. مستبعَد من Git.
_DEV_SECRET_FILE = Path(".auth_secret")


def _db(path=None) -> Path:
    """يحلّ مسار القاعدة عند الاستدعاء لا عند الاستيراد.

    ربطه كقيمة افتراضية كان يعني أن إعادة تعيين DB_PATH بلا أثر —
    فتقرأ الاختبارات قاعدة التطوير بصمت.
    """
    return Path(path) if path else DB_PATH


class AuthConfigError(RuntimeError):
    """إعداد مصادقة غير صالح — يمنع الإقلاع."""


def _load_secret() -> str:
    """يقرأ المفتاح من البيئة، أو يولّد ملفاً محلياً للتطوير.

    المفتاح العشوائي في الذاكرة كان يُبطل كل الجلسات عند كل إقلاع.
    الآن: البيئة أولاً، ثم ملف محلي ثابت، ولا شيء مكتوب في الكود.
    """
    env = (os.getenv("AUTH_SECRET") or "").strip()
    if env:
        if len(env) < MIN_SECRET_LENGTH:
            raise AuthConfigError(
                f"AUTH_SECRET قصير جداً ({len(env)} محرفاً). "
                f"الحد الأدنى {MIN_SECRET_LENGTH}."
            )
        return env

    if os.getenv("APP_ENV", "development").lower() == "production":
        raise AuthConfigError(
            "AUTH_SECRET مطلوب في بيئة الإنتاج. "
            "ولّده بـ: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )

    # تطوير: ملف ثابت بدل قيمة عابرة في الذاكرة
    try:
        if _DEV_SECRET_FILE.exists():
            saved = _DEV_SECRET_FILE.read_text(encoding="utf-8").strip()
            if len(saved) >= MIN_SECRET_LENGTH:
                return saved

        generated = secrets.token_urlsafe(48)
        _DEV_SECRET_FILE.write_text(generated, encoding="utf-8")
        log.warning(
            "AUTH_SECRET غير مضبوط — وُلّد مفتاح تطوير في %s. "
            "اضبط AUTH_SECRET في .env قبل النشر.",
            _DEV_SECRET_FILE,
        )
        return generated
    except OSError as e:
        log.warning("تعذّر حفظ مفتاح التطوير (%s) — سيُولَّد في الذاكرة.", e)
        return secrets.token_urlsafe(48)


SECRET_KEY = _load_secret()

# "admin" مضاف ليصحّ حساب المسؤول التجريبي.
# ⚠️ سلوك الصلاحية لم يتغيّر في هذي الخطوة: is_manager لا يزال
# يقارن بـ "manager" وحده، فالمسؤول يُعامَل معاملة الموظف على
# مستوى الـ API حتى تُنفَّذ الخطوة 3 (نموذج الصلاحية الثلاثي).
ROLES = ("employee", "manager", "admin")

# bcrypt يتجاهل ما بعد 72 بايت. القص صريح هنا بدل ترك المكتبة
# ترمي استثناءً — كلمة مرور طويلة يجب أن تعمل لا أن تُسقط النظام.
_MAX_PASSWORD_BYTES = 72


class AuthError(Exception):
    """يُرمى عند فشل المصادقة أو التحقق من الصلاحية."""


# ---------- كلمات المرور ----------

def _to_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_MAX_PASSWORD_BYTES]


def hash_password(password: str) -> str:
    """ينتج بصمة bcrypt بملح عشوائي — بصمتان مختلفتان لنفس الكلمة."""
    return bcrypt.hashpw(_to_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_to_bytes(plain), hashed.encode("utf-8"))
    except Exception:
        return False


# ---------- الجدول ----------

def init_db(path=None) -> None:
    conn = sqlite3.connect(_db(path))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username      TEXT PRIMARY KEY,
                employee_id   TEXT NOT NULL,
                employee_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'employee',
                created_at    TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()


def create_user(
    username: str,
    employee_id: str,
    employee_name: str,
    password: str,
    role: str = "employee",
    path=None,
) -> None:
    if role not in ROLES:
        raise ValueError(f"دور غير معروف: {role}")

    username = username.strip().lower()
    if not username or len(username) < 2:
        raise ValueError("اسم المستخدم قصير جداً")
    if not username.replace("_", "").replace(".", "").isalnum():
        raise ValueError("اسم المستخدم يقبل الحروف والأرقام و _ . فقط")
    if len(password) < 8:
        raise ValueError("كلمة المرور يجب ألا تقل عن 8 محارف")

    conn = sqlite3.connect(_db(path))
    try:
        conn.execute(
            """INSERT OR REPLACE INTO users
               (username, employee_id, employee_name, password_hash, role, created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                username,
                employee_id,
                employee_name,
                hash_password(password),
                role,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def user_exists(username: str, path=None) -> bool:
    return get_user(username, path) is not None


def delete_user(username: str, path=None) -> bool:
    conn = sqlite3.connect(_db(path))
    try:
        cur = conn.execute(
            "DELETE FROM users WHERE username = ?", (username.strip().lower(),)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_user(username: str, path=None) -> dict | None:
    conn = sqlite3.connect(_db(path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username.strip().lower(),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_users(path=None) -> list[dict]:
    """يعيد المستخدمين دون بصمات كلمات المرور."""
    conn = sqlite3.connect(_db(path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT username, employee_id, employee_name, role FROM users ORDER BY username"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------- تسجيل الدخول ----------

def authenticate(username: str, password: str, path=None) -> dict:
    """يتحقق من البيانات ويعيد بيانات المستخدم.

    رسالة الفشل واحدة للاسم الخاطئ وكلمة المرور الخاطئة —
    التمييز بينهما يكشف أي الأسماء موجودة.
    """
    user = get_user(username, path)

    if user is None or not verify_password(password, user["password_hash"]):
        log.warning("فشل تسجيل دخول: %s", username)
        raise AuthError("بيانات الدخول غير صحيحة")

    log.info("تسجيل دخول ناجح: %s (%s)", username, user["role"])
    return user


def create_token(user: dict) -> str:
    """ينشئ رمزاً موقّعاً يحمل الهوية والدور."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user["username"],
        "employee_id": user["employee_id"],
        "name": user["employee_name"],
        "role": user["role"],
        "iat": now,
        "exp": now + timedelta(hours=TOKEN_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """يفك الرمز ويتحقق من توقيعه وصلاحيته."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as e:
        log.warning("رمز غير صالح: %s", e)
        raise AuthError("الجلسة منتهية أو غير صالحة")


# ---------- الصلاحيات ----------

# ترتيب الصلاحية تصاعدياً. "أعلى أو يساوي" يُقاس بالفهرس.
ROLE_ORDER = ("employee", "manager", "admin")

# نطاق البيانات لكل دور
SCOPE_SELF = "self"     # سجله وحده
SCOPE_TEAM = "team"     # سجله وشجرة من يديرهم
SCOPE_ORG = "org"       # المؤسسة كاملة

_SCOPE_BY_ROLE = {
    "employee": SCOPE_SELF,
    "manager": SCOPE_TEAM,
    "admin": SCOPE_ORG,
}


def is_manager(claims: dict) -> bool:
    """الدور مدير تحديداً — لا يشمل المسؤول.

    يبقى كما هو للتوافق. لبوابات "مدير فأعلى" استخدم is_elevated.
    """
    return claims.get("role") == "manager"


def is_admin(claims: dict) -> bool:
    return claims.get("role") == "admin"


def is_elevated(claims: dict) -> bool:
    """مدير أو مسؤول — بوابة العمليات الإدارية.

    البوابة تسمح بالدخول؛ وتضييق النطاق يحدث داخل الأدوات:
    المدير يمرّ ثم يرى فريقه، والمسؤول يمرّ ثم يرى الجميع.
    """
    return claims.get("role") in ("manager", "admin")


def scope_for(claims: dict | None) -> str:
    """نطاق البيانات المسموح لهذي الهوية.

    غياب الهوية يعني استدعاءً داخلياً (سكربت أو زرع) فينال
    نطاق المؤسسة. الطلبات المصادَقة تحمل دوراً دائماً.
    """
    if not claims:
        return SCOPE_ORG
    return _SCOPE_BY_ROLE.get(claims.get("role"), SCOPE_SELF)


def can_access_employee(claims: dict, employee_name: str) -> bool:
    """المدير يرى الجميع، والموظف يرى نفسه فقط."""
    if is_manager(claims):
        return True
    own = (claims.get("name") or "").strip()
    target = (employee_name or "").strip()
    return bool(own) and (target in own or own in target)


def require_manager(claims: dict) -> None:
    if not is_manager(claims):
        raise AuthError("هذي العملية تتطلب صلاحية مدير")
