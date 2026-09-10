"""اختبارات الحماية من ادّعاء التنفيذ.

الخلفية: النموذج كتب "تم الإلغاء بنجاح" دون استدعاء أي أداة،
والمستخدم صدّق أن العملية تمّت. هذي أخطر من هلوسة المعلومة
لأنها لا تُكتشف بالقراءة.
"""

import pytest

import action_guard as guard

WRITE_TOOLS = {"submit_leave_request", "cancel_leave_request"}


# ---------- ادّعاءات كاذبة يجب أن تُصحَّح ----------

PHANTOM = [
    ("1️⃣ إلغاء الطلب رقم 1\n(تم الإلغاء بنجاح.)", ["list_leave_requests"]),
    ("تم تسجيل طلب الإجازة بنجاح، رقم الطلب 3.", []),
    ("سأقوم بالإلغاء. تم الإلغاء بنجاح.", ["preview_leave_request"]),
    ("أُلغي الطلب وأُنشئ الجديد.", ["list_leave_requests"]),
    ("سجّلت الطلب لك.", ["preview_leave_request"]),
]


@pytest.mark.parametrize("answer,tools", PHANTOM)
def test_phantom_action_is_corrected(answer, tools):
    """الادّعاء بلا أداة كتابة يُستبدل برد صادق."""
    result, corrected = guard.verify(answer, tools, WRITE_TOOLS)
    assert corrected
    assert "لم أنفّذ" in result
    assert "بنجاح" not in result


def test_correction_does_not_leak_original_claim():
    """الرد المصحّح لا يعيد نص الادّعاء — وإلا بقي المستخدم مخدوعاً."""
    result, _ = guard.verify("تم إلغاء الطلب بنجاح", [], WRITE_TOOLS)
    assert "تم إلغاء" not in result


# ---------- ادّعاءات صادقة يجب أن تمرّ ----------

GENUINE = [
    ("تم إلغاء الطلب رقم 1.", ["cancel_leave_request"]),
    ("تم تسجيل الطلب بنجاح.", ["submit_leave_request"]),
    ("أُلغي الطلب وسُجّل الجديد.", ["cancel_leave_request", "submit_leave_request"]),
]


@pytest.mark.parametrize("answer,tools", GENUINE)
def test_real_action_passes(answer, tools):
    """حماية ترفض كل شيء عديمة الفائدة — نقيس الطرفين."""
    result, corrected = guard.verify(answer, tools, WRITE_TOOLS)
    assert not corrected
    assert result == answer


# ---------- ردود لا تدّعي شيئاً ----------

NEUTRAL = [
    ("رصيد الإجازة المتبقي هو 8 أيام.", ["get_leave_balance"]),
    ("هل تؤكد إلغاء الطلب؟", ["list_leave_requests"]),
    ("سأسجّل 4 أيام. رصيدك 8 → 4. أؤكّد؟", ["preview_leave_request"]),
    ("المستندات لا تحتوي على إجابة لهذا السؤال.", ["search_documents"]),
    ("إجمالي بدل الانتقال 72,000 ريال.", ["department_summary"]),
]


@pytest.mark.parametrize("answer,tools", NEUTRAL)
def test_neutral_answers_untouched(answer, tools):
    result, corrected = guard.verify(answer, tools, WRITE_TOOLS)
    assert not corrected
    assert result == answer


def test_confirmation_request_is_not_a_claim():
    """طلب التأكيد يذكر العملية دون ادّعاء تنفيذها."""
    assert not guard.claims_execution("هل تريد أن ألغي الطلب رقم 1؟")


# ---------- حالات حدّية ----------

def test_empty_answer_is_safe():
    result, corrected = guard.verify("", [], WRITE_TOOLS)
    assert not corrected


def test_none_tools_handled():
    result, corrected = guard.verify("تم التسجيل بنجاح", None, WRITE_TOOLS)
    assert corrected


def test_read_tool_does_not_authorize_claim():
    """استدعاء أداة قراءة لا يبرّر ادّعاء الكتابة."""
    _, corrected = guard.verify("تم الإلغاء بنجاح", ["get_employee"], WRITE_TOOLS)
    assert corrected
