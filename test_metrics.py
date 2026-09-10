"""اختبارات القياس وتصنيف النتائج."""

import pytest

import metrics


@pytest.fixture
def db(tmp_path):
    """قاعدة معزولة لكل اختبار — لا نلمس بيانات التطوير."""
    path = tmp_path / "events.db"
    metrics.init_db(path)
    return path


# ---------- التصنيف ----------

REFUSALS = [
    "المستندات المتوفرة لا تحتوي على معلومات حول سياسة التأمين الصحي.",
    "لا توجد مقاطع ذات صلة في المستندات.",
    "لا يوجد موظف بالاسم: سعد",
    "تعذّر الوصول لإجابة نهائية ضمن عدد الخطوات المسموح.",
    "أرجو توضيح ما المقصود بسؤالك.",
    "يرجى تزويدي باسم القسم.",
]

ANSWERS = [
    "رصيد الإجازة المتبقي لخالد الدوسري هو 8 أيام.",
    "إجمالي بدل الانتقال هو 72,000 ريال شهرياً.",
    "ساعات العمل من الثامنة صباحاً حتى الخامسة مساءً.",
]


@pytest.mark.parametrize("text", REFUSALS)
def test_refusal_is_not_counted_as_success(text):
    """استدعاء أداة ليس نجاحاً — البحث قد يُستدعى ولا يجد شيئاً.

    كان المنطق القديم يصنّف هذي "ok" فتبالغ اللوحة في نسبة الإجابة.
    """
    assert metrics.classify_outcome(text, ["search_documents"], 0) == "refused"


@pytest.mark.parametrize("text", ANSWERS)
def test_real_answers_counted_as_success(text):
    assert metrics.classify_outcome(text, ["get_employee"], 0) == "ok"


def test_empty_answer_is_error():
    assert metrics.classify_outcome("", [], 0) == "error"


def test_warning_prefix_is_error():
    assert metrics.classify_outcome("⚠️ تعذّر الاتصال", [], 0) == "error"


def test_no_tools_and_no_sources_is_refused():
    """ردّ عام بلا أي استرجاع ليس إجابة على سؤال."""
    assert metrics.classify_outcome("مرحباً بك!", [], 0) == "refused"


def test_answer_with_sources_is_success():
    assert metrics.classify_outcome("الشروط هي كذا.", ["search_documents"], 3) == "ok"


# ---------- التسجيل والتلخيص ----------

def test_init_is_idempotent(tmp_path):
    """التشغيل المتكرر آمن — الخدمة تقلع مرات كثيرة."""
    path = tmp_path / "x.db"
    metrics.init_db(path)
    metrics.init_db(path)
    assert metrics.summary(path)["questions"] == 0


def test_summary_counts_questions_only(db):
    metrics.record("question", "س1", "agent", tokens=100, path=db)
    metrics.record("upload", "ملف.docx", path=db)
    metrics.record("delete", "abc123", path=db)

    s = metrics.summary(db)
    assert s["questions"] == 1
    assert s["uploads"] == 1


def test_summary_aggregates_tokens_and_timing(db):
    metrics.record("question", "س1", "agent", tokens=800, duration_ms=1000, path=db)
    metrics.record("question", "س2", "agent", tokens=1200, duration_ms=3000, path=db)

    s = metrics.summary(db)
    assert s["total_tokens"] == 2000
    assert s["avg_ms"] == 2000
    assert s["max_ms"] == 3000


def test_summary_splits_outcomes(db):
    metrics.record("question", "س1", "agent", outcome="ok", path=db)
    metrics.record("question", "س2", "agent", outcome="refused", path=db)
    metrics.record("question", "س3", "agent", outcome="blocked", path=db)
    metrics.record("question", "س4", "agent", outcome="error", path=db)

    s = metrics.summary(db)
    assert (s["refused"], s["blocked"], s["errors"]) == (1, 1, 1)


def test_summary_counts_each_tool(db):
    metrics.record("question", "س1", "agent", ["search_documents"], path=db)
    metrics.record("question", "س2", "agent", ["search_documents", "calculate"], path=db)

    by_tool = metrics.summary(db)["by_tool"]
    assert by_tool["search_documents"] == 2
    assert by_tool["calculate"] == 1


def test_long_question_is_truncated(db):
    metrics.record("question", "س" * 900, "agent", path=db)
    assert len(metrics.summary(db)["recent"][0]["question"]) <= 300


def test_recent_is_capped_and_newest_first(db):
    for i in range(15):
        metrics.record("question", f"سؤال {i}", "agent", path=db)

    recent = metrics.summary(db)["recent"]
    assert len(recent) == 10
    assert recent[0]["question"] == "سؤال 14"


def test_record_never_raises_on_bad_path():
    """القياس لا يُسقط ما يقيسه.

    لو فشل تسجيل حدث لأي سبب، يجب ألا يمنع المستخدم من إجابته.
    """
    from pathlib import Path
    metrics.record("question", "س", path=Path("/nonexistent/dir/x.db"))


def test_reset_clears_events(db):
    metrics.record("question", "س", "agent", path=db)
    metrics.reset(db)
    assert metrics.summary(db)["questions"] == 0
