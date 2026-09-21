"""محوّل البثّ: صيغة SSE · تمرير الهوية · تسجيل الأحداث.

خلفية: `chat_adapter.py` لم يكن مغطّى رغم أنه يحمل حراسة نقطة
المحادثة وتحديد معدلها وتسجيل أحداثها.
"""

import json

import pytest

import chat_adapter


# ---------- صيغة SSE ----------

def test_sse_wraps_dict_as_json():
    out = chat_adapter._sse({"delta": "نص"})
    assert out.startswith("data: ") and out.endswith("\n\n")
    assert json.loads(out[6:].strip())["delta"] == "نص"


def test_sse_keeps_arabic_readable():
    """ensure_ascii=False — وإلا تتحول العربية إلى \\u0623..."""
    assert "مرحبا" in chat_adapter._sse({"delta": "مرحبا"})


def test_sse_passes_raw_sentinel():
    assert chat_adapter._sse("[DONE]") == "data: [DONE]\n\n"


# ---------- البثّ ----------

class FakeRequest:
    def __init__(self, rid="req-1"):
        self.state = type("S", (), {"request_id": rid})()
        self.client = type("C", (), {"host": "127.0.0.1"})()


async def _collect(gen):
    return [chunk async for chunk in gen]


def _parse(chunks):
    events = []
    for c in chunks:
        raw = c[6:].strip()
        if raw == "[DONE]":
            events.append({"done": True}); continue
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError:
            pass
    return events


@pytest.fixture
def stub_agent(monkeypatch):
    """وكيل مزيّف — لا شبكة ولا نموذج."""
    calls = {}

    def fake_run(collection, question, history=None, user_name="", actor=None):
        calls["question"] = question
        calls["history"] = history
        calls["actor"] = actor
        calls["user_name"] = user_name
        return {
            "answer": "الإجابة جاهزة",
            "trace": [{"tool": "search_documents", "args": {}}],
            "sources": [{"source": "policy.docx", "location": "قسم 1", "text": "نص"}],
            "tokens": 500,
        }

    monkeypatch.setattr(chat_adapter.agent, "run_agent", fake_run)
    monkeypatch.setattr(chat_adapter, "STREAM_DELAY", 0)
    return calls


@pytest.fixture
def captured_events(monkeypatch):
    recorded = []
    monkeypatch.setattr(chat_adapter.metrics, "record",
                        lambda **kw: recorded.append(kw))
    return recorded


@pytest.mark.asyncio
async def test_stream_emits_tools_sources_and_done(stub_agent, captured_events):
    actor = {"employee_id": "EMP-003", "role": "employee", "name": "خالد"}
    events = _parse(await _collect(
        chat_adapter._stream(None, "سؤال", [], actor, "req-1")))

    kinds = [e.get("event") for e in events]
    assert "tools" in kinds and "sources" in kinds
    assert events[-1].get("done")


@pytest.mark.asyncio
async def test_stream_reassembles_full_answer(stub_agent, captured_events):
    actor = {"employee_id": "EMP-003", "role": "employee"}
    events = _parse(await _collect(
        chat_adapter._stream(None, "سؤال", [], actor, "r")))
    text = "".join(e["delta"] for e in events if "delta" in e)
    assert text.strip() == "الإجابة جاهزة"


@pytest.mark.asyncio
async def test_stream_passes_actor_to_agent(stub_agent, captured_events):
    """الهوية تصل للوكيل — الأدوات تفحص الصلاحية بها."""
    actor = {"employee_id": "EMP-003", "role": "employee", "name": "خالد الدوسري"}
    await _collect(chat_adapter._stream(None, "سؤال", [], actor, "r"))
    assert stub_agent["actor"] == actor
    assert stub_agent["user_name"] == "خالد الدوسري"


@pytest.mark.asyncio
async def test_stream_forwards_history(stub_agent, captured_events):
    history = [{"role": "user", "content": "سابق"},
               {"role": "assistant", "content": "ردّ"}]
    await _collect(chat_adapter._stream(None, "سؤال", history, {"role": "employee"}, "r"))
    assert stub_agent["history"] == history


@pytest.mark.asyncio
async def test_stream_records_audit_context(stub_agent, captured_events):
    actor = {"employee_id": "EMP-003", "role": "employee"}
    await _collect(chat_adapter._stream(None, "سؤال", [], actor, "req-77"))

    ev = captured_events[0]
    assert ev["actor"] == actor
    assert ev["request_id"] == "req-77"
    assert ev["tokens"] == 500
    assert ev["tools"] == ["search_documents"]


@pytest.mark.asyncio
async def test_stream_survives_agent_failure(monkeypatch, captured_events):
    """فشل الوكيل يُنتج رسالة ويُسجَّل — لا انهيار في منتصف البثّ."""
    def boom(*a, **k):
        raise RuntimeError("انقطع الاتصال بالنموذج")

    monkeypatch.setattr(chat_adapter.agent, "run_agent", boom)
    events = _parse(await _collect(
        chat_adapter._stream(None, "سؤال", [], {"role": "employee"}, "r")))

    assert events[-1].get("done")
    assert any("تعذّر" in e.get("delta", "") for e in events)
    assert captured_events[0]["outcome"] == "error"


@pytest.mark.asyncio
async def test_stream_marks_refusal_when_no_tools(monkeypatch, captured_events):
    monkeypatch.setattr(chat_adapter, "STREAM_DELAY", 0)
    monkeypatch.setattr(chat_adapter.agent, "run_agent",
                        lambda *a, **k: {"answer": "لا تحتوي المستندات على إجابة.",
                                         "trace": [], "sources": [], "tokens": 10})
    await _collect(chat_adapter._stream(None, "سؤال", [], {"role": "employee"}, "r"))
    assert captured_events[0]["outcome"] == "refused"


# ---------- عقد الطلب ----------

def test_request_requires_at_least_one_message():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        chat_adapter.ChatRequest(messages=[])


def test_request_accepts_valid_shape():
    req = chat_adapter.ChatRequest(
        messages=[{"role": "user", "content": "سؤال"}])
    assert req.messages[-1].role == "user"
