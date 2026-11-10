"""واجهة REST للمساعد المؤسسي."""

import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import os
import secrets

import agent
import attendance
import auth
import chat_adapter
import geofence
import guardrails
import hybrid
import metrics
import migrations
import org
import ratelimit
import rag_engine as engine
import seed_users
import tools
from logging_config import get_logger, setup_logging

setup_logging()
log = get_logger(__name__)

state: dict = {}


# سقف الرفع: القراءة على دفعات تمنع تحميل ملف ضخم في الذاكرة كاملاً
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("بدء تشغيل الخدمة — تحميل قاعدة المتجهات")
    migrations.migrate()            # مخطّط تراكمي — لا إعادة بناء تدميرية
    state["collection"] = engine.get_collection()
    log.info("الخدمة جاهزة — %d قطعة مخزّنة", state["collection"].count())
    yield
    log.info("إيقاف الخدمة")


app = FastAPI(
    title="المساعد المؤسسي",
    description="واجهة REST للإجابة عن الأسئلة من مستندات الشركة وقواعد بياناتها",
    version="1.1.0",
    lifespan=lifespan,
)

# مصادر مسموحة من البيئة. "*" مع allow_credentials يرفضه المتصفح أصلاً،
# ومع الرموز في الترويسة يعني أن أي موقع يستطيع استدعاء الـ API.
# الافتراضي محلي فقط — ولا نطاق إنتاج مكتوب في الكود.
_DEFAULT_ORIGINS = "http://localhost:8501,http://127.0.0.1:8501"
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", _DEFAULT_ORIGINS).split(",") if o.strip()
]

if "*" in ALLOWED_ORIGINS:
    log.warning("CORS مفتوح للجميع — مقبول للتطوير فقط، لا للنشر.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=os.getenv("CORS_ORIGIN_REGEX") or None,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)

@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    """معرّف يربط الطلب بكل سطور سجله وأحداث تدقيقه."""
    rid = request.headers.get("X-Request-ID") or secrets.token_hex(8)
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


@app.exception_handler(ratelimit.RateLimitExceeded)
async def _rate_limited(request: Request, exc: ratelimit.RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": str(exc)},
        headers={"Retry-After": str(exc.retry_after)},
    )


app.include_router(chat_adapter.build_chat_route(lambda: state["collection"]))


_bearer = HTTPBearer(auto_error=False)


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """يستخرج هوية المستخدم من الرمز.

    الحراسة عند مدخل النظام — كل عميل جديد يمرّ بها تلقائياً.
    """
    if creds is None:
        raise HTTPException(status_code=401, detail="مطلوب تسجيل الدخول")
    try:
        return auth.decode_token(creds.credentials)
    except auth.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


# ---------- نماذج البيانات ----------

class LoginRequest(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(min_length=4, max_length=200)


class NewUserRequest(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    employee_name: str = Field(min_length=2, max_length=80)
    employee_id: str = Field(default="", max_length=20)
    role: str = Field(default="employee")
    password: str = Field(default="", max_length=200)


class NewUserResponse(BaseModel):
    username: str
    employee_name: str
    role: str
    password: str
    note: str


class UserInfo(BaseModel):
    username: str
    employee_name: str
    employee_id: str
    role: str


class LoginResponse(BaseModel):
    token: str
    name: str
    role: str
    employee_id: str

class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    user_name: str = Field(default="", max_length=80)


class Source(BaseModel):
    filename: str
    location: str
    distance: float
    text: str


class AskResponse(BaseModel):
    answer: str
    sources: list[Source]


class AgentStep(BaseModel):
    tool: str
    args: dict


class AgentResponse(BaseModel):
    answer: str
    steps: list[AgentStep]
    sources: list[dict]


class HealthResponse(BaseModel):
    status: str
    chunks: int
    documents: int


class StatsResponse(BaseModel):
    questions: int
    total_tokens: int
    avg_ms: int
    max_ms: int
    refused: int
    blocked: int
    errors: int
    uploads: int
    by_mode: dict
    by_tool: dict
    recent: list[dict]



class MeResponse(BaseModel):
    """هوية المستخدم التنظيمية. الحقول الثلاثة الأولى كما كانت."""
    name: str
    role: str
    id: str
    department: str | None = None
    department_id: str | None = None
    job_title: str | None = None
    manager_id: str | None = None
    manager_name: str | None = None
    employment_status: str | None = None
    email: str | None = None
    hire_date: str | None = None


class TeamMember(BaseModel):
    employee_id: str
    name: str
    job_title: str
    department: str | None = None
    department_id: str | None = None
    employment_status: str = "active"
    direct_reports: int = 0          # يميّز رؤساء الفرق


class TeamResponse(BaseModel):
    employee_id: str
    relationship: str = "direct_reports"   # المباشرون فقط — لا الشجرة
    count: int
    members: list[TeamMember]


class DepartmentView(BaseModel):
    department_id: str
    name: str
    manager_id: str | None = None
    manager_name: str | None = None
    is_active: bool = True
    headcount: int | None = None    # None = غير مكشوف لهذا النطاق


class DepartmentsResponse(BaseModel):
    scope: str
    departments: list[DepartmentView]


class OrgNode(BaseModel):
    employee_id: str
    name: str
    job_title: str
    department_id: str | None = None
    employment_status: str = "active"
    reports: list["OrgNode"] = []


class OrgDepartment(BaseModel):
    department_id: str
    name: str
    manager_id: str | None = None
    manager_name: str | None = None
    headcount: int
    roots: list[OrgNode]


class OrgPerson(BaseModel):
    employee_id: str
    name: str


class OrgTotals(BaseModel):
    departments: int
    employees: int
    managers: int
    reporting_links: int


class OrgStructure(BaseModel):
    departments: list[OrgDepartment]
    unassigned: list[OrgNode]
    unplaced: list[OrgPerson]       # بيانات تالفة لا تُفقَد بصمت
    totals: OrgTotals


OrgNode.model_rebuild()


class MyLeave(BaseModel):
    employee_id: str
    employee_name: str
    department: str = ""
    annual_leave: int
    leave_used: int
    pending_days: int
    available: int
    calculation: str
    formula: str


class MyRequest(BaseModel):
    id: int
    leave_type: str = "annual"
    start_date: str
    end_date: str
    days: int
    status: str
    reason: str = ""              # سبب الموظف عند التقديم
    decision_note: str = ""       # ملاحظة المدير عند القرار — حقل منفصل
    rejection_reason: str = ""    # ملاحظة القرار عند الرفض تحديداً
    decided_at: str = ""
    decided_by_name: str = ""
    created_at: str = ""
    can_cancel: bool = False


class PendingRequest(BaseModel):
    id: int
    employee_name: str
    leave_type: str = "annual"
    start_date: str
    end_date: str
    days: int
    reason: str = ""
    created_at: str = ""           # كان يُحسب ويُحذف بالنموذج
    balance_now: int | None = None
    balance_after_approval: int | None = None


class DecisionRecord(BaseModel):
    id: int
    employee_name: str
    leave_type: str = "annual"
    start_date: str
    end_date: str
    days: int
    status: str
    reason: str = ""               # سبب الموظف
    decision_note: str = ""        # ملاحظة المدير — حقل منفصل
    decided_at: str = ""
    decided_by_name: str = ""
    created_at: str = ""


class ReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    note: str = Field(default="", max_length=200)


class DocumentInfo(BaseModel):
    doc_hash: str
    filename: str
    chunks: int
    file_type: str = ""
    uploaded_at: str = ""
    uploaded_by: str = ""


class UploadResult(BaseModel):
    filename: str
    status: str
    chunks: int


# ---------- المصادقة ----------

@app.post("/auth/login", response_model=LoginResponse, tags=["المصادقة"])
def login(req: LoginRequest, request: Request):
    # التحديد بالعنوان: لا هوية بعد. يُبطئ تخمين كلمات المرور.
    ratelimit.login_limiter.check(ratelimit.client_key(request))

    try:
        user = auth.authenticate(req.username, req.password)
    except auth.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    # الدخول الناجح يُحرّر العدّاد — الحد ضد الفشل المتكرر لا الاستخدام
    ratelimit.login_limiter.reset(ratelimit.client_key(request))

    return LoginResponse(
        token=auth.create_token(user),
        name=user["employee_name"],
        role=user["role"],
        employee_id=user["employee_id"],
    )


@app.get("/auth/users", response_model=list[UserInfo], tags=["المصادقة"])
def list_users(user: dict = Depends(current_user)):
    if not auth.is_admin(user):
        raise HTTPException(status_code=403, detail="إدارة الحسابات تتطلب صلاحية مسؤول")
    return [UserInfo(**u) for u in auth.list_users()]


@app.post("/auth/users", response_model=NewUserResponse, status_code=201, tags=["المصادقة"])
def add_user(req: NewUserRequest, user: dict = Depends(current_user)):
    """ينشئ حساباً جديداً — للمدير فقط.

    الحسابات تُنشأ بدعوة لا بتسجيل ذاتي: النظام يحوي بيانات موظفين
    ومستندات شركة، فمن يدخل يجب أن يكون معروفاً مسبقاً.
    """
    if not auth.is_admin(user):
        log.warning("منع إنشاء حساب: %s ليس مديراً", user["name"])
        raise HTTPException(status_code=403, detail="إنشاء الحسابات يتطلب صلاحية مسؤول")

    if req.role not in auth.ROLES:
        raise HTTPException(status_code=400, detail=f"دور غير معروف: {req.role}")

    if auth.user_exists(req.username):
        raise HTTPException(status_code=409, detail="اسم المستخدم مستخدم بالفعل")

    password = req.password.strip() or seed_users.generate_password()
    employee_id = req.employee_id.strip() or f"EMP-{secrets.randbelow(9000) + 1000}"

    try:
        auth.create_user(
            req.username, employee_id, req.employee_name.strip(), password, req.role
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    log.info("أنشأ %s حساباً جديداً: %s (%s)", user["name"], req.username, req.role)

    return NewUserResponse(
        username=req.username.strip().lower(),
        employee_name=req.employee_name.strip(),
        role=req.role,
        password=password,
        note="كلمة المرور تظهر مرة واحدة — انسخها الآن.",
    )


@app.delete("/auth/users/{username}", status_code=204, tags=["المصادقة"])
def remove_user(username: str, user: dict = Depends(current_user)):
    if not auth.is_admin(user):
        raise HTTPException(status_code=403, detail="حذف الحسابات يتطلب صلاحية مسؤول")

    if username.strip().lower() == user["sub"]:
        raise HTTPException(status_code=400, detail="لا يمكنك حذف حسابك")

    if not auth.delete_user(username):
        raise HTTPException(status_code=404, detail="الحساب غير موجود")

    log.warning("حذف %s الحساب: %s", user["name"], username)


@app.get("/auth/me", response_model=MeResponse, tags=["المصادقة"])
def me(user: dict = Depends(current_user)):
    """الهوية من الرمز وحده — لا معامل يختار موظفاً آخر.

    الحقول الثلاثة الأولى كما كانت (الواجهة تقرؤها)؛ الباقي إضافي
    ويبقى فارغاً إن لم يوجد سجل موظف مرتبط بالحساب.
    """
    out = {"name": user["name"], "role": user["role"], "id": user["employee_id"]}

    emp = org.employee_by_id(user.get("employee_id") or "", path=tools.DB_PATH)
    if emp:
        mgr = org.employee_by_id(emp["manager_id"], path=tools.DB_PATH) \
            if emp.get("manager_id") else None
        out.update(
            department=emp.get("department"),
            department_id=emp.get("department_id"),
            job_title=emp.get("job_title"),
            manager_id=emp.get("manager_id"),
            manager_name=mgr["name"] if mgr else None,
            employment_status=emp.get("employment_status") or "active",
            email=emp.get("email"),
            hire_date=emp.get("hire_date"),
        )
    return MeResponse(**out)


# ---------- النظام ----------

@app.get("/health", response_model=HealthResponse, tags=["النظام"])
def health():
    col = state["collection"]
    return HealthResponse(
        status="ok",
        chunks=col.count(),
        documents=len(engine.list_documents(col)),
    )


@app.get("/stats", response_model=StatsResponse, tags=["النظام"])
def stats(user: dict = Depends(current_user)):
    """الإحصاءات تكشف أسئلة المستخدمين — للمدير فقط."""
    if not auth.is_admin(user):
        log.warning("منع وصول للإحصاءات: %s ليس مديراً", user["name"])
        raise HTTPException(status_code=403, detail="الإحصاءات تتطلب صلاحية مسؤول")
    return StatsResponse(**metrics.summary())


# ---------- الأسئلة ----------

@app.post("/ask", response_model=AskResponse, tags=["الأسئلة"])
def ask(req: AskRequest, request: Request, user: dict = Depends(current_user)):
    start = time.perf_counter()
    rid = getattr(request.state, "request_id", "")
    ratelimit.agent_limiter.check(ratelimit.client_key(request, user))

    try:
        guardrails.check_question(req.question)
    except guardrails.GuardrailViolation as e:
        log.warning("رُفض سؤال عند بوابة /ask: %s", e)
        metrics.record(
            kind="question", question=req.question, mode="docs",
            outcome="blocked", duration_ms=_ms(start),
            actor=user, request_id=rid,
        )
        raise HTTPException(status_code=400, detail=str(e))

    try:
        out = engine.ask_rag(state["collection"], req.question)
    except Exception as e:
        log.exception("فشل معالجة السؤال")
        metrics.record(
            kind="question", question=req.question, mode="docs",
            outcome="error", duration_ms=_ms(start),
            actor=user, request_id=rid,
        )
        raise HTTPException(status_code=502, detail=f"فشل استدعاء النموذج: {e}")

    metrics.record(
        kind="question",
        question=req.question,
        mode="docs",
        sources=len(out["sources"]),
        outcome=metrics.classify_outcome(out["answer"], None, len(out["sources"])),
        duration_ms=_ms(start),
    )

    return AskResponse(
        answer=out["answer"],
        sources=[
            Source(
                filename=s["meta"]["source"],
                location=s["meta"].get("location", ""),
                distance=round(s["distance"], 4),
                text=s["text"],
            )
            for s in out["sources"]
        ],
    )


@app.post("/agent", response_model=AgentResponse, tags=["الوكيل"])
def agent_ask(req: AskRequest, request: Request, user: dict = Depends(current_user)):
    start = time.perf_counter()
    rid = getattr(request.state, "request_id", "")
    ratelimit.agent_limiter.check(ratelimit.client_key(request, user))

    try:
        guardrails.check_question(req.question)
    except guardrails.GuardrailViolation as e:
        log.warning("رُفض سؤال عند بوابة /agent: %s", e)
        metrics.record(
            kind="question", question=req.question, mode="agent",
            outcome="blocked", duration_ms=_ms(start),
            actor=user, request_id=rid,
        )
        raise HTTPException(status_code=400, detail=str(e))

    try:
        out = agent.run_agent(
            state["collection"], req.question,
            user_name=user["name"], actor=user,
        )
    except Exception as e:
        log.exception("فشل تشغيل الوكيل")
        metrics.record(
            kind="question", question=req.question, mode="agent",
            outcome="error", duration_ms=_ms(start),
            actor=user, request_id=rid,
        )
        raise HTTPException(status_code=502, detail=f"فشل الوكيل: {e}")

    tools_used = [t["tool"] for t in out["trace"]]
    metrics.record(
        kind="question",
        question=req.question,
        mode="agent",
        tools=tools_used,
        sources=len(out["sources"]),
        tokens=out.get("tokens", 0),
        outcome=metrics.classify_outcome(
            out["answer"], tools_used, len(out["sources"])
        ),
        duration_ms=_ms(start),
    )

    return AgentResponse(
        answer=out["answer"],
        steps=[AgentStep(tool=t["tool"], args=t["args"]) for t in out["trace"]],
        sources=out["sources"],
    )


# ---------- بيانات المستخدم الحالي ----------
#
# نقاط قراءة فقط تغلّف دوال tools الموجودة — لا منطق عمل جديد.
# اللوحة تحتاج أرقاماً حتمية وسريعة، واستدعاء النموذج لعرض رقم
# بطيء ومكلف وغير حتمي.

@app.get("/me/leave", response_model=MyLeave, tags=["بياناتي"])
def my_leave(request: Request, user: dict = Depends(current_user)):
    """رصيد المستخدم المصادَق عليه.

    الموظف يُحدَّد من `employee_id` في الرمز حصراً — لا يقبل
    اسماً ولا معرّفاً من العميل. فلا يوجد معامل يمكن التلاعب به.
    """
    summary = tools.leave_summary_for(user["employee_id"])
    if summary is None:
        raise HTTPException(status_code=404, detail="لا يوجد سجل موظف مرتبط بهذا الحساب")
    return MyLeave(**summary)


@app.get("/me/requests", response_model=list[MyRequest], tags=["بياناتي"])
def my_requests(request: Request, user: dict = Depends(current_user)):
    """طلبات المستخدم المصادَق عليه، الأحدث أولاً.

    "بياناتي" تعني أنا — حتى لو كان المستخدم مديراً. صلاحية
    الإدارة تُمارَس عبر /leave/pending لا عبر هذا المسار.
    """
    return [MyRequest(**r) for r in tools.requests_for_employee(user["employee_id"])]


@app.post("/leave/{request_id}/cancel", tags=["بياناتي"])
def cancel_my_request(
    request_id: int,
    request: Request,
    user: dict = Depends(current_user),
):
    """يلغي طلباً معلّقاً للمستخدم نفسه.

    الملكية تُفحص في `tools.cancel_leave_request` بالمعرّف —
    لا نكرّر منطق الصلاحية هنا.
    """
    result = tools.cancel_leave_request(request_id, actor=user)

    if not result.get("cancelled"):
        reason = result.get("reason", "تعذّر إلغاء الطلب")
        # نميّز: غير موجود · ليس ملكك · حالة نهائية
        if "لا يوجد طلب" in reason:
            status = 404
        elif "موظف آخر" in reason:
            status = 403
        else:
            status = 409
        log.warning("رفض إلغاء الطلب #%s لـ %s: %s", request_id, user["name"], reason)
        raise HTTPException(status_code=status, detail=reason)

    metrics.record(
        kind="cancel",
        question=f"#{request_id}",
        outcome="cancelled",
        actor=user,
        request_id=getattr(request.state, "request_id", ""),
    )

    summary = tools.leave_summary_for(user["employee_id"])
    return {
        "cancelled": True,
        "request_id": request_id,
        "days_returned": result.get("days_returned"),
        "available": summary["available"] if summary else None,
    }


# ---------- البنية التنظيمية ----------
#
# كل نقطة هنا تحسب ما يُكشف من نطاق الفاعل المخزّن (auth.scope_for
# + شجرة org). لا معامل من العميل يوسّع النطاق — والنقاط لا تقبل
# معاملات هوية أصلاً.

@app.get("/me/team", response_model=TeamResponse, tags=["التنظيم"])
def my_team(user: dict = Depends(current_user)):
    """التقارير المباشرة للمستخدم — لا الشجرة كاملة.

    الموظف: قائمة فارغة (نطاقه نفسه).
    """
    me_id = user.get("employee_id") or ""
    members = []
    if auth.scope_for(user) != auth.SCOPE_SELF and me_id:
        members = org.direct_reports_view(me_id, path=tools.DB_PATH)
    return TeamResponse(employee_id=me_id, count=len(members),
                        members=[TeamMember(**m) for m in members])


@app.get("/org/departments", response_model=DepartmentsResponse, tags=["التنظيم"])
def org_departments(user: dict = Depends(current_user)):
    """الأقسام ضمن النطاق، والعدد من الموظفين المصرّح بهم فقط.

    الموظف:  قسمه وحده بلا عدد
    المدير:  الأقسام التي فيها فريقه، والعدد من فريقه فقط
    المسؤول: كل الأقسام بالعدد الفعلي
    """
    scope = auth.scope_for(user)

    def view(d, headcount):
        return DepartmentView(
            department_id=d["department_id"], name=d["name"],
            manager_id=d.get("manager_id") or None,
            manager_name=d.get("manager_name") or None,
            is_active=bool(d.get("is_active", 1)), headcount=headcount,
        )

    if scope == auth.SCOPE_SELF:
        emp = org.employee_by_id(user.get("employee_id") or "", path=tools.DB_PATH)
        depts = []
        if emp and emp.get("department_id"):
            own = [d for d in org.department_views(None, path=tools.DB_PATH)
                   if d["department_id"] == emp["department_id"]]
            depts = [view(d, None) for d in own]
        return DepartmentsResponse(scope=scope, departments=depts)

    # المدير: نفس مجموعة الفريق التي تفرضها الأدوات — مصدر واحد للنطاق
    allowed = tools._team_ids(user) if scope == auth.SCOPE_TEAM else None
    rows = org.department_views(allowed, path=tools.DB_PATH)
    return DepartmentsResponse(scope=scope,
                               departments=[view(d, d["headcount"]) for d in rows])


@app.get("/org/structure", response_model=OrgStructure, tags=["التنظيم"])
def org_structure(user: dict = Depends(current_user)):
    """الهيكل التنظيمي الكامل — للمسؤول وحده."""
    if not auth.is_admin(user):
        raise HTTPException(status_code=403, detail="الهيكل التنظيمي يتطلب صلاحية مسؤول")

    tree = org.organization_tree(path=tools.DB_PATH)
    for d in tree["departments"]:
        d["manager_id"] = d.get("manager_id") or None
        d["manager_name"] = d.get("manager_name") or None
    return OrgStructure(**tree)


# ---------- مراجعة الإجازات ----------

@app.get("/leave/pending", response_model=list[PendingRequest], tags=["الإجازات"])
def pending_requests(user: dict = Depends(current_user)):
    if not auth.is_elevated(user):
        raise HTTPException(status_code=403, detail="المراجعة تتطلب صلاحية مدير")

    result = tools.list_pending_requests(actor=user)
    return [PendingRequest(**dict(r, leave_type="annual")) for r in result["requests"]]


@app.get("/leave/decisions", response_model=list[DecisionRecord], tags=["الإجازات"])
def recent_decisions(
    request: Request,
    limit: int = 10,
    user: dict = Depends(current_user),
):
    """آخر القرارات على طلبات الفريق — للمدير فقط.

    لا نقطة قائمة تعيد الطلبات المقرَّرة لغير صاحبها:
    /leave/pending تقتصر على المعلّقة، و/me/requests على صاحب الرمز.
    """
    if not auth.is_elevated(user):
        log.warning("منع عرض القرارات: %s ليس مديراً", user["name"])
        raise HTTPException(status_code=403, detail="عرض القرارات يتطلب صلاحية مدير")

    result = tools.recent_decisions(limit, actor=user)
    return [DecisionRecord(**r) for r in result["decisions"]]


@app.post("/leave/{request_id}/review", tags=["الإجازات"])
def review_request(
    request_id: int,
    req: ReviewRequest,
    request: Request,
    user: dict = Depends(current_user),
):
    """يعتمد أو يرفض طلباً معلّقاً. الاعتماد يخصم الأيام فعلياً."""
    if not auth.is_elevated(user):
        log.warning("منع مراجعة: %s ليس مديراً", user["name"])
        raise HTTPException(status_code=403, detail="المراجعة تتطلب صلاحية مدير")

    result = tools.review_leave_request(
        request_id, req.decision, req.note, actor=user
    )
    if not result.get("reviewed"):
        raise HTTPException(status_code=400, detail=result.get("reason", "تعذّرت المراجعة"))

    metrics.record(
        kind="review",
        question=f"#{request_id} {req.decision}",
        outcome=result["status"],
        actor=user,
        request_id=getattr(request.state, "request_id", ""),
    )
    return result


# ---------- المستندات ----------

@app.get("/documents", response_model=list[DocumentInfo], tags=["المستندات"])
def list_docs(user: dict = Depends(current_user)):
    """قائمة المستندات مع بيانات وصفية متاحة فعلاً.

    تاريخ الرفع ومن رفعه يُقرآن من سجل الأحداث — لا عمود جديد
    ولا تخزين مكرّر. حجم الملف غير محفوظ في أي مكان فلا يُعرض.
    """
    docs = engine.list_documents(state["collection"])

    # البيانات التكميلية اختيارية — فشلها يُفقد التاريخ لا القائمة
    try:
        history = metrics.upload_history()
    except Exception as e:
        log.warning("تعذّر إثراء قائمة المستندات: %s", e)
        history = {}

    names = {}
    for meta in history.values():
        aid = meta.get("actor_id")
        if aid and aid not in names:
            try:
                emp = tools.employee_by_id(aid)
                names[aid] = emp["name"] if emp else ""
            except Exception:
                names[aid] = ""

    out = []
    for h, info in docs.items():
        filename = info["filename"]
        record = history.get(filename, {})
        out.append(DocumentInfo(
            doc_hash=h,
            filename=filename,
            chunks=info["chunks"],
            file_type=filename.rsplit(".", 1)[-1].lower() if "." in filename else "",
            uploaded_at=record.get("ts", ""),
            uploaded_by=names.get(record.get("actor_id", ""), ""),
        ))

    out.sort(key=lambda d: (d.uploaded_at or "", d.filename), reverse=True)
    return out


@app.post("/documents", response_model=UploadResult, status_code=201, tags=["المستندات"])
async def upload_doc(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
):
    # رفع المستندات يغيّر قاعدة المعرفة التي يجيب منها النظام للجميع.
    # إخفاء القسم في الواجهة ليس حماية — الفحص هنا حيث تُنفَّذ العملية.
    if not auth.is_elevated(user):
        log.warning("منع رفع مستند: %s ليس مديراً", user["name"])
        raise HTTPException(status_code=403, detail="رفع المستندات يتطلب صلاحية مدير")

    ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in engine.SUPPORTED_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"صيغة غير مدعومة: .{ext} — المدعوم: {engine.SUPPORTED_TYPES}",
        )

    # القراءة على دفعات: نتوقف عند التجاوز بدل تحميل الملف كاملاً.
    # `await file.read()` بلا حد يعني أن ملفاً ضخماً يستنزف الذاكرة.
    chunks, size = [], 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            log.warning("ملف مرفوض لكبر حجمه: %s (> %d MB)", file.filename, MAX_UPLOAD_MB)
            raise HTTPException(
                status_code=413,
                detail=f"الملف كبير جداً. الحد الأقصى {MAX_UPLOAD_MB} ميغابايت.",
            )
        chunks.append(chunk)

    content = b"".join(chunks)
    if not content:
        raise HTTPException(status_code=400, detail="الملف فارغ")

    try:
        result = engine.ingest_file(state["collection"], content, file.filename)
    except Exception as e:
        log.exception("فشل رفع الملف %s", file.filename)
        metrics.record(
            kind="upload", question=file.filename, outcome="error",
            actor=user, request_id=getattr(request.state, "request_id", ""),
        )
        raise HTTPException(status_code=500, detail=f"فشل معالجة الملف: {e}")

    if result["status"] == "added":
        hybrid.invalidate()

    metrics.record(
        kind="upload",
        question=file.filename,
        outcome=result["status"],
        sources=result["chunks"],
        actor=user,
        request_id=getattr(request.state, "request_id", ""),
    )

    return UploadResult(**result)


@app.delete(
    "/documents/{doc_hash}",
    status_code=204,
    responses={404: {"description": "المستند غير موجود"}},
    tags=["المستندات"],
)
def delete_doc(doc_hash: str, request: Request, user: dict = Depends(current_user)):
    if not auth.is_elevated(user):
        log.warning("منع حذف مستند: %s ليس مديراً", user["name"])
        raise HTTPException(status_code=403, detail="حذف المستندات يتطلب صلاحية مدير")

    col = state["collection"]
    if not engine.document_exists(col, doc_hash):
        raise HTTPException(status_code=404, detail="المستند غير موجود")
    engine.delete_document(col, doc_hash)
    hybrid.invalidate()
    metrics.record(
        kind="delete", question=doc_hash,
        actor=user, request_id=getattr(request.state, "request_id", ""),
    )


# ===================== الحضور =====================
#
# نماذج الطلب تقبل الإحداثيات والدقّة **فقط**. لا حقل site_id ولا
# inside ولا distance: غيابها من المخطّط يعني أن Pydantic يُسقطها
# قبل أن تصل إلى المنطق، فادّعاء العميل لا يُقرأ أصلاً.
#
# الخصوصية بالحقل لا بالنيّة: نماذج الردّ للمدير والمسؤول لا تُعرّف
# حقول الإحداثيات ولا الدقّة ولا عنوان الطلب ولا المسافة المحسوبة.
# حقل غير معرَّف لا يخرج مهما حوى الصفّ.
#
# كاشف الوجود: السجل غير الموجود والسجل خارج النطاق يعيدان 404
# بالنصّ نفسه حرفياً. و403 عن القدرة فقط (دور لا يكفي، أو مراجعة
# ذاتية) — ورفض الدور ليس كشفاً: الموظف يعلم أنه ليس مديراً.

NO_SUCH_RECORD = "لا يوجد سجل بهذا الرقم"
NO_SUCH_CORRECTION = "لا يوجد طلب تصحيح بهذا الرقم"
NEEDS_MANAGER = "هذه البيانات تتطلب صلاحية مدير"
NEEDS_ADMIN = "إدارة المواقع تتطلب صلاحية مسؤول"


class Reading(BaseModel):
    """قراءة موقع من الجهاز. ثلاثة حقول لا رابع."""
    latitude: float = Field(..., description="خط العرض كما أبلغ به الجهاز")
    longitude: float = Field(..., description="خط الطول كما أبلغ به الجهاز")
    accuracy_m: float = Field(..., description="نصف قطر الدقّة بالمتر")


class CheckResult(BaseModel):
    recorded: bool
    accepted: bool | None = None
    record_id: int | None = None
    verdict: str | None = None
    reason: str | None = None
    site_id: str | None = None
    site_name: str | None = None
    distance_m: float | None = None
    check_in_at: str | None = None
    check_out_at: str | None = None
    since: str | None = None


class MyAttendanceRecord(BaseModel):
    """سجل الموظف عن نفسه — يشمل ما أبلغ به جهازه."""
    record_id: int
    check_in_at: str
    check_out_at: str | None = None
    in_verdict: str
    in_verdict_reason: str | None = None
    out_verdict: str | None = None
    out_verdict_reason: str | None = None
    site_id: str | None = None
    site_name: str | None = None
    duration_minutes: int | None = None
    counts_toward_work: bool
    administrative: bool
    approved_by_correction_id: int | None = None
    in_reported_lat: float | None = None
    in_reported_lon: float | None = None
    in_reported_accuracy_m: float | None = None
    in_computed_distance_m: float | None = None


class TeamAttendanceRecord(BaseModel):
    """سجل يراه المدير — بلا إحداثيات ولا دقّة ولا مسافة ولا IP.

    المدير يحتاج أن يعرف هل حضر وأين ومتى، لا أن يتتبّع موظفه
    بدقّة المتر. والحقول موجودة في القاعدة للتدقيق عند النزاع،
    لا للعرض اليومي.
    """
    record_id: int
    employee_id: str
    employee_name: str | None = None
    check_in_at: str
    check_out_at: str | None = None
    verdict: str
    closure: str | None = None
    site_name: str | None = None
    duration_minutes: int | None = None
    counts_toward_work: bool
    administrative: bool


class Totals(BaseModel):
    verified_minutes: int
    administrative_minutes: int
    total_minutes: int
    unresolved_sessions: int
    unresolved_note: str | None = None


class MyAttendanceResponse(BaseModel):
    employee_id: str
    open_session: MyAttendanceRecord | None = None
    count: int
    records: list[MyAttendanceRecord]
    totals: Totals


class TeamAttendanceResponse(BaseModel):
    scope: str
    count: int
    records: list[TeamAttendanceRecord]
    totals: Totals


class SiteOut(BaseModel):
    site_id: str
    name: str
    latitude: float
    longitude: float
    radius_m: int
    department_id: str | None = None
    is_active: bool


class SitesResponse(BaseModel):
    count: int
    sites: list[SiteOut]


class NewSite(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    latitude: float
    longitude: float
    radius_m: int = 150
    department_id: str | None = None


class SitePatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    latitude: float | None = None
    longitude: float | None = None
    radius_m: int | None = None
    department_id: str | None = None


class NewCorrection(BaseModel):
    kind: str
    requested_change: str = ""
    reason: str = ""
    record_id: int | None = None
    requested_check_in_at: str | None = None
    requested_check_out_at: str | None = None


class CorrectionCreated(BaseModel):
    created: bool
    correction_id: int | None = None
    status: str | None = None
    reason: str | None = None


class CorrectionOut(BaseModel):
    correction_id: int
    employee_id: str
    employee_name: str | None = None
    record_id: int | None = None
    kind: str
    requested_change: str | None = None
    reason: str | None = None
    status: str
    created_at: str | None = None
    requested_check_in_at: str | None = None
    requested_check_out_at: str | None = None


class PendingCorrections(BaseModel):
    scope: str
    count: int
    corrections: list[CorrectionOut]


class ReviewCorrection(BaseModel):
    decision: str
    note: str = ""


class ReviewResult(BaseModel):
    reviewed: bool
    correction_id: int | None = None
    status: str | None = None
    decided_at: str | None = None
    reason: str | None = None


def _site_names(path=None) -> dict:
    return {s["site_id"]: s["name"] for s in attendance.sites(active_only=False)}


def _mine(row: dict, names: dict) -> MyAttendanceRecord:
    return MyAttendanceRecord(
        record_id=row["id"],
        check_in_at=row["check_in_at"],
        check_out_at=row["check_out_at"],
        in_verdict=row["in_verdict"],
        in_verdict_reason=row["in_verdict_reason"],
        out_verdict=row["out_verdict"],
        out_verdict_reason=row["out_verdict_reason"],
        site_id=row["site_id"],
        site_name=names.get(row["site_id"]),
        duration_minutes=attendance.record_minutes(row),
        counts_toward_work=attendance.counts_toward_work(row),
        administrative=row["in_verdict"] == attendance.VERDICT_MANUALLY_ADDED
        or row["out_verdict"] == attendance.VERDICT_CORRECTION_CLOSED,
        approved_by_correction_id=row.get("corrected_by_correction_id"),
        in_reported_lat=row["in_reported_lat"],
        in_reported_lon=row["in_reported_lon"],
        in_reported_accuracy_m=row["in_reported_accuracy_m"],
        in_computed_distance_m=row["in_computed_distance_m"],
    )


def _theirs(row: dict, names: dict, who: dict) -> TeamAttendanceRecord:
    return TeamAttendanceRecord(
        record_id=row["id"],
        employee_id=row["employee_id"],
        employee_name=who.get(row["employee_id"]),
        check_in_at=row["check_in_at"],
        check_out_at=row["check_out_at"],
        verdict=row["in_verdict"],
        closure=row["out_verdict"],
        site_name=names.get(row["site_id"]),
        duration_minutes=attendance.record_minutes(row),
        counts_toward_work=attendance.counts_toward_work(row),
        administrative=row["in_verdict"] == attendance.VERDICT_MANUALLY_ADDED
        or row["out_verdict"] == attendance.VERDICT_CORRECTION_CLOSED,
    )


def _actor_id(user: dict) -> str:
    employee_id = (user.get("employee_id") or "").strip()
    if not employee_id:
        raise HTTPException(status_code=403, detail="حسابك غير مرتبط بسجل موظف")
    return employee_id


def _attendance_guard(request: Request, user: dict) -> None:
    try:
        ratelimit.attendance_limiter.check(ratelimit.client_key(request, user))
    except ratelimit.RateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=str(e),
                            headers={"Retry-After": str(e.retry_after)}) from None


@app.post("/attendance/check-in", response_model=CheckResult, tags=["الحضور"])
def attendance_check_in(body: Reading, request: Request,
                        user: dict = Depends(current_user)):
    """تسجيل حضور. الفاعل يسجّل لنفسه حصراً — لا معرّف موظف في الجسم."""
    employee_id = _actor_id(user)
    _attendance_guard(request, user)

    result = attendance.check_in(
        employee_id, body.latitude, body.longitude, body.accuracy_m,
        request_ip=(request.client.host if request.client else ""),
    )
    metrics.record(kind="check_in", question=result.get("verdict", ""),
                   outcome="accepted" if result.get("accepted") else "refused",
                   actor=user, request_id=getattr(request.state, "request_id", ""))
    return CheckResult(**result)


@app.post("/attendance/check-out", response_model=CheckResult, tags=["الحضور"])
def attendance_check_out(body: Reading, request: Request,
                         user: dict = Depends(current_user)):
    employee_id = _actor_id(user)
    _attendance_guard(request, user)

    result = attendance.check_out(
        employee_id, body.latitude, body.longitude, body.accuracy_m,
        request_ip=(request.client.host if request.client else ""),
    )
    metrics.record(kind="check_out", question=result.get("verdict", ""),
                   outcome="recorded" if result.get("recorded") else "refused",
                   actor=user, request_id=getattr(request.state, "request_id", ""))
    return CheckResult(**result)


@app.get("/me/attendance", response_model=MyAttendanceResponse, tags=["الحضور"])
def my_attendance(request: Request, start: str = "", end: str = "",
                  user: dict = Depends(current_user)):
    """سجل الموظف عن نفسه، ومعه حالة الجلسة المفتوحة.

    الحالة داخل الردّ لا في نقطة مستقلة: الواجهة تحتاجها لترسم زرّ
    «حضور» أو «انصراف»، وجلبها مع السجل نداء واحد — كما تفعل
    /me/leave التي تعيد رصيداً مركّباً.
    """
    employee_id = _actor_id(user)
    try:
        rows = attendance.records_for([employee_id], start=start, end=end)
    except attendance.AttendanceTimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    names = _site_names()
    openrow = attendance.open_session(employee_id)
    return MyAttendanceResponse(
        employee_id=employee_id,
        open_session=_mine(openrow, names) if openrow else None,
        count=len(rows),
        records=[_mine(r, names) for r in rows],
        totals=Totals(**attendance.summarize(rows)),
    )


@app.get("/team/attendance", response_model=TeamAttendanceResponse, tags=["الحضور"])
def team_attendance(request: Request, start: str = "", end: str = "",
                    user: dict = Depends(current_user)):
    if not auth.is_elevated(user):
        raise HTTPException(status_code=403, detail=NEEDS_MANAGER)

    scope_ids = tools.attendance_scope(user)
    try:
        rows = attendance.records_for(scope_ids, start=start, end=end)
    except attendance.AttendanceTimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    who = attendance.employee_names({r["employee_id"] for r in rows})
    return TeamAttendanceResponse(
        scope="org" if scope_ids is None else "team",
        count=len(rows),
        records=[_theirs(r, _site_names(), who) for r in rows],
        totals=Totals(**attendance.summarize(rows)),
    )


@app.get("/sites", response_model=SitesResponse, tags=["الحضور"])
def list_sites(user: dict = Depends(current_user)):
    """المواقع النشطة للجميع، والمعطّلة للمسؤول.

    إحداثيات موقع العمل ليست بياناً شخصياً، والموظف يحتاج أن يعرف
    أين يسجّل.
    """
    rows = attendance.sites(active_only=not auth.is_admin(user))
    return SitesResponse(count=len(rows), sites=[
        SiteOut(site_id=s["site_id"], name=s["name"], latitude=s["latitude"],
                longitude=s["longitude"], radius_m=s["radius_m"],
                department_id=s["department_id"], is_active=bool(s["is_active"]))
        for s in rows])


@app.post("/sites", response_model=SiteOut, status_code=201, tags=["الحضور"])
def create_site(body: NewSite, request: Request, user: dict = Depends(current_user)):
    if not auth.is_admin(user):
        raise HTTPException(status_code=403, detail=NEEDS_ADMIN)
    try:
        s = attendance.create_site(body.name, body.latitude, body.longitude,
                                   body.radius_m, body.department_id)
    except geofence.GeofenceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    metrics.record(kind="site_create", question=body.name, actor=user,
                   request_id=getattr(request.state, "request_id", ""))
    return SiteOut(site_id=s["site_id"], name=s["name"], latitude=s["latitude"],
                   longitude=s["longitude"], radius_m=s["radius_m"],
                   department_id=s["department_id"], is_active=bool(s["is_active"]))


@app.patch("/sites/{site_id}", response_model=SiteOut, tags=["الحضور"])
def patch_site(site_id: str, body: SitePatch, user: dict = Depends(current_user)):
    if not auth.is_admin(user):
        raise HTTPException(status_code=403, detail=NEEDS_ADMIN)
    try:
        s = attendance.update_site(site_id, body.name, body.latitude, body.longitude,
                                   body.radius_m, body.department_id)
    except geofence.GeofenceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    if s is None:
        raise HTTPException(status_code=404, detail="لا يوجد موقع بهذا الرقم")
    return SiteOut(site_id=s["site_id"], name=s["name"], latitude=s["latitude"],
                   longitude=s["longitude"], radius_m=s["radius_m"],
                   department_id=s["department_id"], is_active=bool(s["is_active"]))


@app.post("/sites/{site_id}/deactivate", response_model=SiteOut, tags=["الحضور"])
def deactivate_site(site_id: str, user: dict = Depends(current_user)):
    if not auth.is_admin(user):
        raise HTTPException(status_code=403, detail=NEEDS_ADMIN)
    s = attendance.set_site_active(site_id, False)
    if s is None:
        raise HTTPException(status_code=404, detail="لا يوجد موقع بهذا الرقم")
    return SiteOut(site_id=s["site_id"], name=s["name"], latitude=s["latitude"],
                   longitude=s["longitude"], radius_m=s["radius_m"],
                   department_id=s["department_id"], is_active=bool(s["is_active"]))


@app.post("/attendance/corrections", response_model=CorrectionCreated,
          tags=["الحضور"])
def create_correction(body: NewCorrection, request: Request,
                      user: dict = Depends(current_user)):
    """طلب تصحيح على سجل الفاعل. الملكية تُفحص في الطبقة الأدنى."""
    employee_id = _actor_id(user)

    # السجل خارج ملكيته أو غير موجود: الردّ نفسه، فلا يُكشف وجوده.
    if body.record_id is not None:
        rec = attendance.record_by_id(body.record_id)
        if rec is None or (rec["employee_id"] or "").strip() != employee_id:
            raise HTTPException(status_code=404, detail=NO_SUCH_RECORD)

    result = attendance.create_correction(
        employee_id, body.kind, body.requested_change, body.reason,
        record_id=body.record_id,
        requested_check_in_at=body.requested_check_in_at,
        requested_check_out_at=body.requested_check_out_at,
    )
    if not result["created"]:
        raise HTTPException(status_code=400, detail=result["reason"])
    metrics.record(kind="correction", question=body.kind, actor=user,
                   request_id=getattr(request.state, "request_id", ""))
    return CorrectionCreated(**result)


@app.get("/attendance/corrections/pending", response_model=PendingCorrections,
         tags=["الحضور"])
def pending_corrections(user: dict = Depends(current_user)):
    if not auth.is_elevated(user):
        raise HTTPException(status_code=403, detail=NEEDS_MANAGER)

    scope_ids = tools.attendance_scope(user)
    rows = attendance.pending_corrections(scope_ids)
    return PendingCorrections(
        scope="org" if scope_ids is None else "team",
        count=len(rows),
        corrections=[CorrectionOut(
            correction_id=r["id"], employee_id=r["employee_id"],
            employee_name=r.get("employee_name"), record_id=r["record_id"],
            kind=r["kind"], requested_change=r["requested_change"],
            reason=r["reason"], status=r["status"], created_at=r["created_at"],
            requested_check_in_at=r.get("requested_check_in_at"),
            requested_check_out_at=r.get("requested_check_out_at"),
        ) for r in rows])


@app.post("/attendance/corrections/{correction_id}/review",
          response_model=ReviewResult, tags=["الحضور"])
def review_correction(correction_id: int, body: ReviewCorrection, request: Request,
                      user: dict = Depends(current_user)):
    if not auth.is_elevated(user):
        raise HTTPException(status_code=403, detail=NEEDS_MANAGER)

    reviewer_id = _actor_id(user)
    corr = attendance.correction_by_id(correction_id)

    # غير موجود وخارج النطاق: الرمز والنصّ نفسهما حرفياً.
    if corr is None or not tools._can_view(user, corr["employee_id"]):
        raise HTTPException(status_code=404, detail=NO_SUCH_CORRECTION)

    result = attendance.review_correction(correction_id, body.decision,
                                          reviewer_id, body.note)
    if not result["reviewed"]:
        reason = result.get("reason", "")
        if "مراجعة طلبك" in reason:
            raise HTTPException(status_code=403, detail=reason)
        if "حُسم" in reason:
            raise HTTPException(status_code=409, detail=reason)
        raise HTTPException(status_code=400, detail=reason)

    metrics.record(kind="correction_review", question=str(correction_id),
                   outcome=result["status"], actor=user,
                   request_id=getattr(request.state, "request_id", ""))
    return ReviewResult(**{k: v for k, v in result.items() if k != "reviewed_by"})
