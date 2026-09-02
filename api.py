"""واجهة REST للمساعد المؤسسي."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

import rag_engine as engine
from logging_config import get_logger, setup_logging

setup_logging()
log = get_logger(__name__)

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("بدء تشغيل الخدمة — تحميل قاعدة المتجهات")
    state["collection"] = engine.get_collection()
    log.info("الخدمة جاهزة — %d قطعة مخزّنة", state["collection"].count())
    yield
    log.info("إيقاف الخدمة")


app = FastAPI(
    title="المساعد المؤسسي",
    description="واجهة REST للإجابة عن الأسئلة من مستندات الشركة",
    version="1.0.0",
    lifespan=lifespan,
)


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


class HealthResponse(BaseModel):
    status: str
    chunks: int
    documents: int


# ---------- النقاط ----------

@app.get("/health", response_model=HealthResponse, tags=["النظام"])
def health():
    col = state["collection"]
    return HealthResponse(
        status="ok",
        chunks=col.count(),
        documents=len(engine.list_documents(col)),
    )


@app.post("/ask", response_model=AskResponse, tags=["الأسئلة"])
def ask(req: AskRequest):
    try:
        out = engine.ask_rag(state["collection"], req.question)
    except Exception as e:
        log.exception("فشل معالجة السؤال")
        raise HTTPException(status_code=502, detail=f"فشل استدعاء النموذج: {e}")

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