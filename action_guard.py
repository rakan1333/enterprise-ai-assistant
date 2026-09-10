"""حماية من ادّعاء التنفيذ (phantom action).

النموذج قد يكتب "تم الإلغاء بنجاح" دون استدعاء أي أداة.
هذي أخطر من هلوسة المعلومة: المستخدم يصدّق أن العملية تمّت،
ولا يكتشف الحقيقة إلا متأخراً.

الحماية هنا برمجية لا سلوكية — نقارن ما ادّعاه الرد
بما نُفّذ فعلاً، ولا نعتمد على طاعة النموذج للتعليمات.
"""

import re

from logging_config import get_logger

log = get_logger(__name__)

# عبارات تدّعي إتمام عملية. تُطابَق كنمط لا كنص حرفي
# لأن النموذج يصوغها بأشكال متعددة.
_CLAIM_PATTERNS = [
    r"تم\s+(ال)?(إلغاء|الغاء|تسجيل|التسجيل|إنشاء|الإنشاء|حذف|الحذف)",
    r"(أُلغي|الغي|سُجّل|سجل|أُنشئ|انشئ)\s+(ال)?طلب",
    r"(ألغيت|سجّلت|سجلت|أنشأت|انشأت)\s+(ال)?طلب",
    r"successfully\s+(cancel|submit|creat|delet)",
    r"بنجاح",
]

_CLAIM_RE = re.compile("|".join(_CLAIM_PATTERNS), re.IGNORECASE)

_CORRECTION = (
    "لم أنفّذ أي عملية بعد.\n\n"
    "لتنفيذ الطلب أحتاج تأكيداً صريحاً يذكر العملية المطلوبة، مثل:\n"
    "«ألغِ الطلب رقم 1» أو «سجّل الطلب».\n\n"
    "لن تُنفَّذ أي عملية إلا بعد ذلك."
)


def claims_execution(answer: str) -> bool:
    """هل يدّعي الرد أن عملية تمّت؟"""
    return bool(_CLAIM_RE.search(answer or ""))


def verify(answer: str, tools_used: list[str], write_tools: set[str]) -> tuple[str, bool]:
    """يتحقق أن ادّعاء التنفيذ مسنود باستدعاء أداة كتابة فعلي.

    يعيد (الرد، هل صُحّح).
    """
    if not claims_execution(answer):
        return answer, False

    executed = [t for t in (tools_used or []) if t in write_tools]
    if executed:
        return answer, False

    log.error(
        "ادّعاء تنفيذ بلا أداة | الأدوات المستدعاة: %s | الرد: %s",
        tools_used, (answer or "")[:180],
    )
    return _CORRECTION, True
