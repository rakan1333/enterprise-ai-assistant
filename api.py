"""واجهة REST للمساعد المؤسسي."""

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import agent
import chat_adapter
import guardrails
import metrics
import rag_engine as engine
from logging_config import get_logger, setup_logging

setup_logging()
log = get_logger(__name__)

state: dict = {}


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("بدء تشغيل الخدمة — تحميل قاعدة المتجهات")
    state["collection"] = engine.get_collection()
    metrics.init_db()
    log.info("الخدمة جاهزة — %d قطعة مخزّنة", state["collection"].count())
    yield
    log.info("إيقاف الخدمة")


app = FastAPI(
    title="المساعد المؤسسي",
    description="واجهة REST للإجابة عن الأسئلة من مستندات الشركة وقواعد بياناتها",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_adapter.build_chat_route(lambda: state["collection"]))


# ---------- نماذج البيانات ----------

class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


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


class DocumentInfo(BaseModel):
    doc_hash: str
    filename: str
    chunks: int


class UploadResult(BaseModel):
    filename: str
    status: str
    chunks: int


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
def stats():
    return StatsResponse(**metrics.summary())


# ---------- الأسئلة ----------

@app.post("/ask", response_model=AskResponse, tags=["الأسئلة"])
def ask(req: AskRequest):
    start = time.perf_counter()

    try:
        guardrails.check_question(req.question)
    except guardrails.GuardrailViolation as e:
        log.warning("رُفض سؤال عند بوابة /ask: %s", e)
        metrics.record(
            kind="question", question=req.question, mode="docs",
            outcome="blocked", duration_ms=_ms(start),
        )
        raise HTTPException(status_code=400, detail=str(e))

    try:
        out = engine.ask_rag(state["collection"], req.question)
    except Exception as e:
        log.exception("فشل معالجة السؤال")
        metrics.record(
            kind="question", question=req.question, mode="docs",
            outcome="error", duration_ms=_ms(start),
        )
        raise HTTPException(status_code=502, detail=f"فشل استدعاء النموذج: {e}")

    metrics.record(
        kind="question",
        question=req.question,
        mode="docs",
        sources=len(out["sources"]),
        outcome="ok" if out["sources"] else "refused",
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
def agent_ask(req: AskRequest):
    start = time.perf_counter()

    try:
        guardrails.check_question(req.question)
    except guardrails.GuardrailViolation as e:
        log.warning("رُفض سؤال عند بوابة /agent: %s", e)
        metrics.record(
            kind="question", question=req.question, mode="agent",
            outcome="blocked", duration_ms=_ms(start),
        )
        raise HTTPException(status_code=400, detail=str(e))

    try:
        out = agent.run_agent(state["collection"], req.question)
    except Exception as e:
        log.exception("فشل تشغيل الوكيل")
        metrics.record(
            kind="question", question=req.question, mode="agent",
            outcome="error", duration_ms=_ms(start),
        )
        raise HTTPException(status_code=502, detail=f"فشل الوكيل: {e}")

    tools_used = [t["tool"] for t in out["trace"]]
    metrics.record(
        kind="question",
        question=req.question,
        mode="agent",
        tools=tools_used,
        sources=len(out["sources"]),
        outcome="ok" if tools_used else "refused",
        duration_ms=_ms(start),
    )

    return AgentResponse(
        answer=out["answer"],
        steps=[AgentStep(tool=t["tool"], args=t["args"]) for t in out["trace"]],
        sources=out["sources"],
    )


# ---------- المستندات ----------

@app.get("/documents", response_model=list[DocumentInfo], tags=["المستندات"])
def list_docs():
    docs = engine.list_documents(state["collection"])
    return [
        DocumentInfo(doc_hash=h, filename=info["filename"], chunks=info["chunks"])
        for h, info in docs.items()
    ]


@app.post("/documents", response_model=UploadResult, status_code=201, tags=["المستندات"])
async def upload_doc(file: UploadFile = File(...)):
    ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in engine.SUPPORTED_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"صيغة غير مدعومة: .{ext} — المدعوم: {engine.SUPPORTED_TYPES}",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="الملف فارغ")

    try:
        result = engine.ingest_file(state["collection"], content, file.filename)
    except Exception as e:
        log.exception("فشل رفع الملف %s", file.filename)
        metrics.record(kind="upload", question=file.filename, outcome="error")
        raise HTTPException(status_code=500, detail=f"فشل معالجة الملف: {e}")

    metrics.record(
        kind="upload",
        question=file.filename,
        outcome=result["status"],
        sources=result["chunks"],
    )

    return UploadResult(**result)


@app.delete(
    "/documents/{doc_hash}",
    status_code=204,
    responses={404: {"description": "المستند غير موجود"}},
    tags=["المستندات"],
)
def delete_doc(doc_hash: str):
    col = state["collection"]
    if not engine.document_exists(col, doc_hash):
        raise HTTPException(status_code=404, detail="المستند غير موجود")
    engine.delete_document(col, doc_hash)
    metrics.record(kind="delete", question=doc_hash)
