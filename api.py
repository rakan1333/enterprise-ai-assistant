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

    
class DocumentInfo(BaseModel):
    doc_hash: str
    filename: str
    chunks: int


class UploadResult(BaseModel):
    filename: str
    status: str
    chunks: int


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
        raise HTTPException(status_code=500, detail=f"فشل معالجة الملف: {e}")

    return UploadResult(**result)


@app.delete("/documents/{doc_hash}", status_code=204, tags=["المستندات"])
def delete_doc(doc_hash: str):
    col = state["collection"]
    if not engine.document_exists(col, doc_hash):
        raise HTTPException(status_code=404, detail="المستند غير موجود")
    engine.delete_document(col, doc_hash) 
    