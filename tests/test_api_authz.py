"""مصفوفة صلاحيات طبقة HTTP.

خلفية: 198 اختباراً تغطي المنطق، ولا واحد يلمس طبقة HTTP —
وهي حيث تُفرَض الصلاحيات. هذي الفجوة أخفت ثغرة رفع المستندات.

مبدأ حاكم: إخفاء عنصر في الواجهة ليس حماية. كل فحص هنا
يستدعي الـ API مباشرة كما يفعل أي عميل يعرف العنوان.
"""

import pytest


# ---------- المصادقة: لا رمز = لا وصول ----------

PROTECTED = [
    ("get",    "/documents",              None),
    ("post",   "/documents",              None),
    ("delete", "/documents/abc123",       None),
    ("get",    "/stats",                  None),
    ("get",    "/auth/me",                None),
    ("get",    "/auth/users",             None),
    ("post",   "/auth/users",             {"username": "x1", "employee_name": "مستخدم تجريبي"}),
    ("delete", "/auth/users/khalid",      None),
    ("post",   "/ask",                    {"question": "ما شروط العمل عن بعد؟"}),
    ("post",   "/agent",                  {"question": "ما شروط العمل عن بعد؟"}),
    ("post",   "/chat",                   {"messages": [{"role": "user", "content": "سؤال"}]}),
    ("get",    "/leave/pending",          None),
    ("post",   "/leave/1/review",         {"decision": "approve"}),
]


def _call(client, method, path, body, headers=None):
    """GET و DELETE لا يقبلان جسماً في عميل الاختبار."""
    kw = {}
    if headers:
        kw["headers"] = headers
    if body is not None and method in ("post", "put", "patch"):
        kw["json"] = body
    return getattr(client, method)(path, **kw)


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_no_token_is_rejected(api_client, method, path, body):
    r = _call(api_client, method, path, body)
    assert r.status_code == 401, f"{method.upper()} {path} → {r.status_code}"


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_tampered_token_is_rejected(api_client, employee, method, path, body):
    """تعديل الرمز لرفع الصلاحية يفشل — التوقيع يحميه."""
    bad = employee["Authorization"][:-6] + "AAAAAA"
    r = _call(api_client, method, path, body, {"Authorization": bad})
    assert r.status_code == 401


def test_health_is_open_by_design(api_client):
    """/health مفتوحة عمداً — أنظمة المراقبة تستدعيها بلا اعتماد."""
    assert api_client.get("/health").status_code == 200


# ---------- رفع المستندات: الثغرة الأولى ----------

def test_employee_cannot_upload_document(api_client, employee, docx_upload):
    """الواجهة تخفي الرفع عن الموظف — والـ API كان يسمح به.

    هذا الاختبار يُثبت الثغرة: استدعاء مباشر يتجاوز إخفاء الواجهة.
    """
    r = api_client.post("/documents", files=docx_upload, headers=employee)
    assert r.status_code == 403, f"موظف رفع مستنداً! → {r.status_code}"


def test_manager_can_upload_document(api_client, manager, docx_upload):
    """الطرف الآخر — حماية تمنع الجميع عديمة الفائدة."""
    r = api_client.post("/documents", files=docx_upload, headers=manager)
    assert r.status_code in (201, 500), r.text


def test_employee_cannot_delete_document(api_client, employee):
    r = api_client.delete("/documents/whatever", headers=employee)
    assert r.status_code == 403


def test_manager_passes_delete_authorization(api_client, manager):
    """404 يعني تجاوز فحص الصلاحية ووصل لفحص الوجود."""
    r = api_client.delete("/documents/does-not-exist", headers=manager)
    assert r.status_code == 404


# ---------- الإحصاءات ----------

def test_employee_cannot_read_stats(api_client, employee):
    """الإحصاءات تكشف أسئلة المستخدمين."""
    assert api_client.get("/stats", headers=employee).status_code == 403


def test_manager_cannot_read_stats(api_client, manager):
    """⚠️ تغيير مُعلَن (الخطوة 3.1): الإحصاءات تكشف أسئلة كل الموظفين
    في المؤسسة، وهذا يتجاوز نطاق فريق المدير. كان هذا الاختبار
    manager_can_read_stats."""
    assert api_client.get("/stats", headers=manager).status_code == 403


def test_admin_can_read_stats(api_client, admin):
    assert api_client.get("/stats", headers=admin).status_code == 200


# ---------- إدارة المستخدمين ----------

def test_employee_cannot_list_users(api_client, employee):
    assert api_client.get("/auth/users", headers=employee).status_code == 403


def test_employee_cannot_create_user(api_client, employee):
    r = api_client.post(
        "/auth/users",
        json={"username": "sneak", "employee_name": "حساب متسلل"},
        headers=employee,
    )
    assert r.status_code == 403
    assert api_client.get("/auth/users", headers=employee).status_code == 403


def test_employee_cannot_delete_user(api_client, employee):
    r = api_client.delete("/auth/users/noura", headers=employee)
    assert r.status_code == 403


def test_manager_cannot_list_users(api_client, manager):
    """⚠️ تغيير مُعلَن (الخطوة 3.1): إدارة الحسابات للمسؤول وحده.
    كان هذا الاختبار manager_can_list_users."""
    assert api_client.get("/auth/users", headers=manager).status_code == 403


def test_admin_can_list_users(api_client, admin):
    r = api_client.get("/auth/users", headers=admin)
    assert r.status_code == 200
    assert "password_hash" not in r.text


# ---------- مراجعة الإجازات ----------

def test_employee_cannot_view_pending(api_client, employee):
    assert api_client.get("/leave/pending", headers=employee).status_code == 403


def test_employee_cannot_review_request(api_client, employee):
    r = api_client.post("/leave/1/review", json={"decision": "approve"}, headers=employee)
    assert r.status_code == 403


def test_manager_can_view_pending(api_client, manager):
    assert api_client.get("/leave/pending", headers=manager).status_code == 200


# ---------- الهوية ----------

def test_token_carries_correct_identity(api_client, employee, manager):
    e = api_client.get("/auth/me", headers=employee).json()
    m = api_client.get("/auth/me", headers=manager).json()
    assert e["role"] == "employee" and e["id"] == "EMP-003"
    assert m["role"] == "manager" and m["id"] == "EMP-002"


def test_login_rejects_wrong_password(api_client):
    r = api_client.post("/auth/login", json={"username": "khalid", "password": "wrong"})
    assert r.status_code == 401


def test_login_failure_message_is_uniform(api_client):
    """التمييز بين اسم خاطئ وكلمة خاطئة يكشف أي الأسماء موجود."""
    a = api_client.post("/auth/login", json={"username": "khalid", "password": "wrongpass"})
    b = api_client.post("/auth/login", json={"username": "ghostuser", "password": "wrongpass"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]


# ---------- CORS ----------

def test_cors_is_not_wildcard():
    """wildcard مع رموز في الترويسة يعني أن أي موقع يستدعي الـ API."""
    import api
    assert "*" not in api.ALLOWED_ORIGINS


def test_cors_defaults_to_local_only():
    import api
    assert all("localhost" in o or "127.0.0.1" in o for o in api.ALLOWED_ORIGINS), api.ALLOWED_ORIGINS


def test_cors_reads_from_environment(monkeypatch):
    """قابل للضبط بلا تعديل كود — ولا نطاق إنتاج مكتوب في المصدر."""
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example.com, https://b.example.com")
    import importlib, os
    origins = [o.strip() for o in os.getenv("CORS_ORIGINS").split(",") if o.strip()]
    assert origins == ["https://a.example.com", "https://b.example.com"]


def test_no_production_origin_hardcoded():
    import inspect, api
    src = inspect.getsource(api)
    head = src.split("_DEFAULT_ORIGINS")[1].split("\n")[0]
    assert "localhost" in head or "127.0.0.1" in head


def test_allowed_origin_receives_cors_header(api_client):
    r = api_client.get("/health", headers={"Origin": "http://localhost:8501"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:8501"


def test_foreign_origin_gets_no_cors_header(api_client):
    r = api_client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert r.headers.get("access-control-allow-origin") is None


# ---------- سقف الرفع ----------

def test_oversized_upload_rejected(api_client, manager):
    """القراءة على دفعات تتوقف عند التجاوز — لا تحميل كامل في الذاكرة."""
    import api
    big = b"x" * (api.MAX_UPLOAD_BYTES + 1024)
    r = api_client.post(
        "/documents",
        files={"file": ("huge.docx", big,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        headers=manager,
    )
    assert r.status_code == 413


def test_upload_limit_is_configurable():
    import api
    assert api.MAX_UPLOAD_BYTES == api.MAX_UPLOAD_MB * 1024 * 1024
    assert 1 <= api.MAX_UPLOAD_MB <= 500


def test_empty_upload_rejected(api_client, manager):
    r = api_client.post(
        "/documents",
        files={"file": ("empty.docx", b"", "application/octet-stream")},
        headers=manager,
    )
    assert r.status_code == 400


# ---------- معرّف الطلب ----------

def test_response_carries_request_id(api_client):
    assert api_client.get("/health").headers.get("X-Request-ID")


def test_client_request_id_is_echoed(api_client):
    """يسمح بربط سجل العميل بسجل الخادم."""
    r = api_client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert r.headers["X-Request-ID"] == "trace-123"


# ---------- تحديد المعدل ----------

def test_repeated_login_failures_are_throttled(api_client):
    import ratelimit
    ratelimit.login_limiter.reset()
    codes = [
        api_client.post("/auth/login",
                        json={"username": "khalid", "password": "wrongpass"}).status_code
        for _ in range(ratelimit.login_limiter.limit + 3)
    ]
    assert 429 in codes, codes
    ratelimit.login_limiter.reset()


def test_throttled_response_has_retry_after(api_client):
    import ratelimit
    ratelimit.login_limiter.reset()
    last = None
    for _ in range(ratelimit.login_limiter.limit + 2):
        last = api_client.post("/auth/login",
                               json={"username": "khalid", "password": "wrongpass"})
    assert last.status_code == 429
    assert int(last.headers["Retry-After"]) > 0
    ratelimit.login_limiter.reset()


def test_successful_login_clears_the_counter(api_client):
    """الحد ضد الفشل المتكرر لا ضد الاستخدام المشروع."""
    import ratelimit
    ratelimit.login_limiter.reset()
    for _ in range(ratelimit.login_limiter.limit - 1):
        api_client.post("/auth/login", json={"username": "khalid", "password": "wrongpass"})
    ok = api_client.post("/auth/login", json={"username": "khalid", "password": "Employee@123"})
    assert ok.status_code == 200
    again = api_client.post("/auth/login", json={"username": "khalid", "password": "Employee@123"})
    assert again.status_code == 200
    ratelimit.login_limiter.reset()


def test_agent_endpoint_is_rate_limited(api_client, employee):
    import ratelimit
    ratelimit.agent_limiter.reset()
    codes = []
    for _ in range(ratelimit.agent_limiter.limit + 2):
        codes.append(api_client.post("/agent", json={"question": "سؤال اختباري"},
                                     headers=employee).status_code)
    assert 429 in codes
    ratelimit.agent_limiter.reset()


def test_rate_limit_is_per_identity(api_client, employee, other_employee):
    """موظف يستنفد حده لا يمنع زميله."""
    import ratelimit
    ratelimit.agent_limiter.reset()
    for _ in range(ratelimit.agent_limiter.limit + 1):
        api_client.post("/agent", json={"question": "سؤال"}, headers=employee)
    r = api_client.post("/agent", json={"question": "سؤال"}, headers=other_employee)
    assert r.status_code != 429
    ratelimit.agent_limiter.reset()
