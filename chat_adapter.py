"""محوّل بين الواجهة وخادم المساعد.

الواجهة تتوقّع: POST /chat يستقبل قائمة رسائل ويعيد بثّاً بصيغة SSE.
خادمنا يوفّر: run_agent يعيد إجابة كاملة دفعة واحدة.

هذه الوحدة تترجم بين العقدين دون تعديل أيٍّ منهما.
"""

import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import agent
import guardrails
from logging_config import get_logger

log = get_logger(__name__)

router = APIRouter()

# تأخير بسيط بين الكلمات يعطي إحساس الكتابة الحيّة
STREAM_DELAY = 0.02


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    model: str = "balanced"


def _sse(payload: dict | str) -> str:
    """يغلّف الحمولة بصيغة Server-Sent Events."""
    if isinstance(payload, str):
        return f"data: {payload}\n\n"
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _stream(collection, question: str, history: list[dict]):
    """ينفّذ الوكيل ثم يبثّ إجابته كلمةً كلمة."""
    try:
        out = await asyncio.to_thread(agent.run_agent, collection, question, history)
    except Exception as e:
        log.exception("فشل الوكيل داخل المحوّل")
        yield _sse({"delta": f"⚠️ تعذّر إنتاج إجابة: {e}"})
        yield _sse("[DONE]")
        return

    steps = [t["tool"] for t in out.get("trace", [])]
    if steps:
        yield _sse({"event": "tools", "tools": steps})

    sources = [
        {
            "doc": s.get("source", ""),
            "chunk": s.get("location", ""),
            "text": s.get("text", ""),
        }
        for s in out.get("sources", [])
    ]
    if sources:
        yield _sse({"event": "sources", "sources": sources})

    answer = out.get("answer", "")
    for word in answer.split(" "):
        yield _sse({"delta": word + " "})
        await asyncio.sleep(STREAM_DELAY)

    yield _sse("[DONE]")


def build_chat_route(get_collection):
    """ينشئ النقطة مع حقن مصدر المجموعة من التطبيق المضيف."""

    @router.post("/chat", tags=["الوكيل"])
    async def chat(req: ChatRequest):
        if req.messages[-1].role != "user":
            raise HTTPException(status_code=400, detail="آخر رسالة يجب أن تكون من المستخدم")

        question = req.messages[-1].content
        history = [{"role": m.role, "content": m.content} for m in req.messages[:-1]]

        try:
            guardrails.check_question(question)
        except guardrails.GuardrailViolation as e:
            log.warning("رُفض سؤال عند بوابة /chat: %s", e)

            async def refuse():
                yield _sse({"delta": str(e)})
                yield _sse("[DONE]")

            return StreamingResponse(refuse(), media_type="text/event-stream")

        return StreamingResponse(
            _stream(get_collection(), question, history),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
