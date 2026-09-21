"""تحديد معدل بسيط داخل العملية.

بلا تبعية خارجية: نافذة منزلقة في الذاكرة تكفي لخادم واحد.
عند تعدّد النسخ خلف موازن حِمل ستحتاج مخزناً مشتركاً (Redis) —
مسجّل كقيد في README لا كوعد.

الهدف: إبطاء التخمين على تسجيل الدخول، ومنع استنزاف حصة النموذج.
"""

import threading
import time
from collections import defaultdict, deque

from logging_config import get_logger

log = get_logger(__name__)


class RateLimitExceeded(Exception):
    """تجاوز الحد — يحمل الثواني المتبقية."""

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"تجاوزت الحد المسموح. حاول بعد {retry_after} ثانية.")


class SlidingWindow:
    """يسمح بـ limit طلباً خلال window ثانية لكل مفتاح."""

    def __init__(self, limit: int, window: int, name: str = ""):
        self.limit = limit
        self.window = window
        self.name = name
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """يرفع RateLimitExceeded عند التجاوز. لا يستهلك عند الرفض."""
        now = time.monotonic()
        cutoff = now - self.window

        with self._lock:
            q = self._hits[key]
            while q and q[0] < cutoff:
                q.popleft()

            if len(q) >= self.limit:
                retry = max(1, int(q[0] + self.window - now) + 1)
                log.warning("تجاوز حد %s | المفتاح: %s | إعادة بعد %ds",
                            self.name or "طلبات", key, retry)
                raise RateLimitExceeded(retry)

            q.append(now)

            # تنظيف كسول — يمنع نمو الذاكرة بمفاتيح ميتة
            if len(self._hits) > 4096:
                for k in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
                    del self._hits[k]

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def remaining(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key, deque())
            live = sum(1 for t in q if t >= now - self.window)
            return max(0, self.limit - live)


# الحدود: تسجيل الدخول ضيّق (تخمين كلمات المرور)،
# والوكيل أوسع (استخدام مشروع) لكنه يحمي حصة النموذج.
login_limiter = SlidingWindow(limit=8,  window=300, name="تسجيل الدخول")
agent_limiter = SlidingWindow(limit=30, window=60,  name="المساعد")


def client_key(request, user=None) -> str:
    """مفتاح التحديد: هوية المستخدم إن وُجدت، وإلا عنوان العميل.

    الهوية أدق من العنوان — عدة موظفين قد يشتركون في IP واحد
    خلف شبكة الشركة.
    """
    if user:
        ident = user.get("employee_id") or user.get("sub")
        if ident:
            return "user:" + str(ident)

    client = getattr(request, "client", None)
    return "ip:" + (client.host if client else "unknown")
