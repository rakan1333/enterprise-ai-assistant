"""طبقة الحماية — تفحص المدخلات والمخرجات."""

import re

from logging_config import get_logger

log = get_logger(__name__)

MAX_QUESTION_LENGTH = 500

INJECTION_PATTERNS = [
    r"تجاهل\s+(كل\s+)?(التعليمات|ما\s+سبق)",
    r"ignore\s+(all\s+)?(previous\s+)?instructions",
    r"disregard\s+.{0,20}instructions",
    r"you\s+are\s+now\s+",
    r"أنت\s+الآن\s+",
    r"new\s+instructions?\s*:",
    r"تعليمات\s+جديدة\s*:",
    r"system\s*prompt",
    r"reveal\s+.{0,20}(prompt|instructions)",
    r"اكشف\s+.{0,20}(التعليمات|النظام)",
]

SENSITIVE_PATTERNS = [
    r"\b(كل|جميع)\s+(الموظفين|الرواتب|البيانات)\b",
    r"\ball\s+(employees|salaries|records)\b",
    r"\b(احذف|امسح|drop|delete|truncate)\b",
]


class GuardrailViolation(Exception):
    """يُرمى عند رفض المدخل أو المخرج."""


def check_question(question: str) -> None:
    """يفحص سؤال المستخدم قبل تمريره للوكيل."""
    q = question.strip()

    if len(q) < 3:
        raise GuardrailViolation("السؤال قصير جداً.")

    if len(q) > MAX_QUESTION_LENGTH:
        raise GuardrailViolation(
            f"السؤال طويل جداً (الحد {MAX_QUESTION_LENGTH} حرف)."
        )

    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, q, re.IGNORECASE):
            log.warning("رُفض سؤال — نمط حقن محتمل: %s", q[:100])
            raise GuardrailViolation(
                "تعذّر معالجة هذا السؤال. أعد صياغته بشكل مباشر."
            )

    for pattern in SENSITIVE_PATTERNS:
        if re.search(pattern, q, re.IGNORECASE):
            log.warning("رُفض سؤال — طلب واسع النطاق: %s", q[:100])
            raise GuardrailViolation(
                "هذا الطلب يتجاوز نطاق الاستعلامات المسموحة. "
                "اسأل عن موظف أو قسم محدد."
            )


def sanitize_context(text: str) -> str:
    """ينظّف نص المستندات من محاولات الحقن قبل إرساله للنموذج."""
    cleaned = text
    for pattern in INJECTION_PATTERNS:
        cleaned = re.sub(pattern, "[محتوى محذوف]", cleaned, flags=re.IGNORECASE)
    if cleaned != text:
        log.warning("نُظّف مقطع من محتوى مشبوه")
    return cleaned


def check_tool_args(tool_name: str, args: dict) -> None:
    """يفحص معاملات الأداة قبل تنفيذها."""
    for key, value in args.items():
        if not isinstance(value, str):
            continue
        if len(value) > 200:
            raise GuardrailViolation(f"معامل طويل جداً للأداة {tool_name}")
        if re.search(r"[;'\"]|--|\bunion\b|\bselect\b", value, re.IGNORECASE):
            log.warning("معامل مشبوه للأداة %s: %s", tool_name, value[:100])
            raise GuardrailViolation(f"معامل غير مقبول للأداة {tool_name}")