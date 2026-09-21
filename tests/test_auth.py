"""اختبارات المصادقة والصلاحيات.

المبدأ الحاكم: الصلاحية تُفحص حيث تُنفَّذ العملية، لا في الواجهة.
إخفاء زر ليس حماية — الـ API يبقى مكشوفاً لمن يعرف عنوانه.
"""

import pytest

import auth


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "auth.db"
    auth.init_db(path)
    auth.create_user("khalid", "EMP-003", "خالد الدوسري", "Khalid@123", "employee", path)
    auth.create_user("noura", "EMP-002", "نورة العتيبي", "Noura@123", "manager", path)
    return path


# ---------- كلمات المرور ----------

def test_password_is_never_stored_plain():
    """لو سُرّبت القاعدة، لا تُستخرج الكلمات."""
    hashed = auth.hash_password("Secret123")
    assert "Secret123" not in hashed
    assert hashed.startswith("$2b$")


def test_same_password_gives_different_hashes():
    """الملح العشوائي يمنع كشف تطابق كلمتين بمقارنة البصمات."""
    assert auth.hash_password("same") != auth.hash_password("same")


def test_verify_accepts_correct_and_rejects_wrong():
    h = auth.hash_password("Right123")
    assert auth.verify_password("Right123", h)
    assert not auth.verify_password("Wrong123", h)


def test_verify_survives_malformed_hash():
    """بصمة تالفة في القاعدة لا تُسقط الخدمة."""
    assert not auth.verify_password("any", "not-a-hash")


def test_long_password_does_not_crash():
    """bcrypt يرفض ما يتجاوز 72 بايت — نقصّها بدل ترك استثناء يصعد."""
    long = "ط" * 500
    assert auth.verify_password(long, auth.hash_password(long))


# ---------- تسجيل الدخول ----------

def test_login_succeeds_with_correct_credentials(db):
    user = auth.authenticate("khalid", "Khalid@123", db)
    assert user["employee_name"] == "خالد الدوسري"
    assert user["role"] == "employee"


def test_username_is_case_insensitive(db):
    assert auth.authenticate("KHALID", "Khalid@123", db)


def test_wrong_password_rejected(db):
    with pytest.raises(auth.AuthError):
        auth.authenticate("khalid", "wrong", db)


def test_unknown_user_rejected(db):
    with pytest.raises(auth.AuthError):
        auth.authenticate("ghost", "anything", db)


def test_failure_message_is_identical_for_both_cases(db):
    """التمييز بين الاسم الخاطئ وكلمة المرور الخاطئة يكشف أي الأسماء موجود."""
    msgs = []
    for username, password in [("khalid", "wrong"), ("ghost", "wrong")]:
        with pytest.raises(auth.AuthError) as exc:
            auth.authenticate(username, password, db)
        msgs.append(str(exc.value))
    assert msgs[0] == msgs[1]


# ---------- الرموز ----------

def test_token_carries_identity_and_role(db):
    user = auth.authenticate("noura", "Noura@123", db)
    claims = auth.decode_token(auth.create_token(user))
    assert claims["name"] == "نورة العتيبي"
    assert claims["role"] == "manager"
    assert claims["employee_id"] == "EMP-002"


def test_tampered_token_is_rejected(db):
    """رفع الصلاحية بتعديل الرمز يفشل — التوقيع يحميه."""
    user = auth.authenticate("khalid", "Khalid@123", db)
    token = auth.create_token(user)
    with pytest.raises(auth.AuthError):
        auth.decode_token(token[:-6] + "AAAAAA")


@pytest.mark.parametrize("bad", ["", "not.a.token", "a.b.c", "x" * 60])
def test_garbage_tokens_rejected(bad):
    with pytest.raises(auth.AuthError):
        auth.decode_token(bad)


# ---------- الصلاحيات ----------

@pytest.fixture
def employee(db):
    return auth.decode_token(auth.create_token(auth.get_user("khalid", db)))


@pytest.fixture
def manager(db):
    return auth.decode_token(auth.create_token(auth.get_user("noura", db)))


def test_employee_sees_only_self(employee):
    assert auth.can_access_employee(employee, "خالد الدوسري")
    assert not auth.can_access_employee(employee, "أحمد الشمري")


def test_manager_sees_everyone(manager):
    assert auth.can_access_employee(manager, "أحمد الشمري")
    assert auth.can_access_employee(manager, "سارة القحطاني")


def test_role_helpers(employee, manager):
    assert not auth.is_manager(employee)
    assert auth.is_manager(manager)


def test_require_manager_blocks_employee(employee, manager):
    auth.require_manager(manager)
    with pytest.raises(auth.AuthError):
        auth.require_manager(employee)


# ---------- إدارة المستخدمين ----------

def test_user_listing_hides_password_hashes(db):
    users = auth.list_users(db)
    assert len(users) == 2
    assert all("password_hash" not in u for u in users)


def test_invalid_role_rejected(db):
    with pytest.raises(ValueError):
        auth.create_user("x", "EMP-9", "س", "pass", "superadmin", db)


def test_unknown_user_returns_none(db):
    assert auth.get_user("nobody", db) is None


# ---------- توليد كلمات المرور ----------

import seed_users


def test_generated_passwords_are_unique():
    """التكرار يعني مصدر عشوائية ضعيفاً."""
    pwds = [seed_users.generate_password() for _ in range(200)]
    assert len(set(pwds)) == 200


def test_generated_password_length():
    assert len(seed_users.generate_password()) == 14
    assert len(seed_users.generate_password(24)) == 24


def test_generated_passwords_avoid_confusing_characters():
    """l · I · O · 0 · 1 تُقرأ خطأً عند النقل اليدوي."""
    joined = "".join(seed_users.generate_password() for _ in range(100))
    assert not any(c in joined for c in "lIO01")


def test_generated_password_mixes_character_classes():
    joined = "".join(seed_users.generate_password() for _ in range(60))
    assert any(c.islower() for c in joined)
    assert any(c.isupper() for c in joined)
    assert any(c.isdigit() for c in joined)
    assert any(c in "@#$%&*" for c in joined)


def test_generated_password_authenticates(tmp_path):
    path = tmp_path / "gen.db"
    auth.init_db(path)
    pwd = seed_users.generate_password()
    auth.create_user("tester", "EMP-9", "مختبِر", pwd, "employee", path)
    assert auth.authenticate("tester", pwd, path)["employee_name"] == "مختبِر"


# ---------- إدارة المستخدمين ----------

def test_username_validation_rejects_bad_input(db):
    """اسم المستخدم يدخل في الروابط والاستعلامات — نقيّده بحروف آمنة."""
    for bad in ["a", "has space", "a@b", "a/b", ""]:
        with pytest.raises(ValueError):
            auth.create_user(bad, "EMP-9", "اسم صالح", "LongEnough1", "employee", db)


def test_short_password_rejected(db):
    with pytest.raises(ValueError):
        auth.create_user("valid", "EMP-9", "اسم صالح", "short", "employee", db)


def test_username_with_dot_and_underscore_allowed(db):
    auth.create_user("saad.g_1", "EMP-9", "سعد", "LongEnough1", "employee", db)
    assert auth.user_exists("saad.g_1", db)


def test_user_exists_checks(db):
    assert auth.user_exists("khalid", db)
    assert not auth.user_exists("ghost", db)


def test_delete_user_removes_account(db):
    assert auth.delete_user("khalid", db)
    assert not auth.user_exists("khalid", db)


def test_delete_unknown_user_returns_false(db):
    assert not auth.delete_user("ghost", db)


def test_deleted_user_cannot_login(db):
    auth.delete_user("khalid", db)
    with pytest.raises(auth.AuthError):
        auth.authenticate("khalid", "Khalid@123", db)
