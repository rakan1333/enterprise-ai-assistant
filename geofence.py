"""حساب السياج الجغرافي — دوال خالصة بلا قاعدة بيانات وبلا هوية.

لماذا منفصلة؟ لأن هذه هي النقطة التي يقرّر فيها الخادم، ولا يُسمح
لأي مسار آخر أن يقرّر بدلاً منها. فصلها عن القاعدة يجعلها قابلة
للاختبار بالكامل: الحدود والإحداثيات الشاذة والتداخل تُفحص كدوال
لا كسيناريوهات قاعدة بيانات.

حدّ الثقة — يُقرأ قبل أي تعديل هنا
-----------------------------------
الجهاز يبلّغ: خط العرض، خط الطول، ونصف قطر الدقّة.
الخادم يحسب: المسافة والحكم.

ما يمنعه هذا التصميم: أن يعلن العميل «أنا داخل النطاق». الإعلان
لا يُقرأ أصلاً — لا يوجد معامل اسمه inside ولا site_id موثوق.

ما لا يمنعه: أن يبلّغ العميل بإحداثيات كاذبة. أدوات المطوّر
تتجاوز الموقع بنقرتين، وتطبيقات المحاكاة تفعل المثل على الجوال.
هذا سجلّ قابل للتدقيق لا نظام إثبات. من ينوي الغشّ سيغشّ؛ ما
نضمنه أن أثره مسجَّل بما ادّعاه وما حسبناه، فيُراجَع لاحقاً.
"""

import math

# نصف قطر الأرض المتوسط بالمتر (IUGG). الخطأ على مسافات السياج
# (مئات الأمتار) أقل من المتر — أدقّ بكثير من دقّة أي هاتف.
EARTH_RADIUS_M = 6371008.8

# أقصى نصف قطر دقّة نقبله. قراءة بدقّة 500 متر لا تُثبت وجوداً
# داخل سياج نصف قطره 150 متراً — لا تُثبت شيئاً أصلاً.
#
# حدّه: من يزوّر الموقع يزوّر معه accuracy: 5. هذا يرفض القراءة
# العاجزة، لا القراءة الكاذبة.
MAX_ACCURACY_M = 200.0

# أنصاف الأقطار المقبولة لموقع عمل.
MIN_RADIUS_M = 20
MAX_RADIUS_M = 20_000

VERDICT_INSIDE = "inside"
VERDICT_OUTSIDE = "outside"
VERDICT_REJECTED = "rejected"      # القراءة نفسها غير صالحة للحكم


class GeofenceError(ValueError):
    """مدخل غير صالح — يُستخدم عند إنشاء موقع لا عند تقييم قراءة."""


def _finite(value) -> float | None:
    """يحوّل إلى float ويرفض NaN و inf والنصوص غير الرقمية."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def valid_coordinates(lat, lon) -> bool:
    """هل النقطة على سطح الأرض فعلاً؟"""
    la, lo = _finite(lat), _finite(lon)
    if la is None or lo is None:
        return False
    return -90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0


def valid_radius(radius_m) -> bool:
    r = _finite(radius_m)
    return r is not None and MIN_RADIUS_M <= r <= MAX_RADIUS_M


def distance_m(lat1, lon1, lat2, lon2) -> float:
    """المسافة بين نقطتين على سطح كروي — haversine.

    نستخدم haversine لا الصيغة المستوية لأن الأخيرة تخطئ مع خط
    العرض: درجة الطول تضيق كلما ابتعدنا عن خط الاستواء، وفي
    السعودية (~24°) تساوي 0.91 من عرضها عند الاستواء. تجاهل ذلك
    يوسّع السياج شرقاً وغرباً بنحو 10%.

    يرمي GeofenceError على إحداثيات غير صالحة — المستدعي يتحقّق
    أولاً أو يستخدم evaluate التي تتحمّل.
    """
    if not (valid_coordinates(lat1, lon1) and valid_coordinates(lat2, lon2)):
        raise GeofenceError("إحداثيات غير صالحة")

    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp = p2 - p1
    dl = math.radians(float(lon2) - float(lon1))

    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def check_reading(lat, lon, accuracy_m) -> str:
    """يفحص صلاحية القراءة نفسها. يعيد سبب الرفض أو نصاً فارغاً.

    الدقّة إلزامية: قراءة بلا دقّة لا يمكن تقدير قيمتها، وقبولها
    يفتح الباب لإرسال إحداثيات بلا أي التزام.
    """
    if not valid_coordinates(lat, lon):
        return "إحداثيات غير صالحة"

    acc = _finite(accuracy_m)
    if acc is None:
        return "تعذّر تحديد دقّة الموقع"
    if acc < 0:
        return "دقّة الموقع غير صالحة"
    if acc > MAX_ACCURACY_M:
        return f"دقّة الموقع غير كافية ({round(acc)} متر)"
    return ""


def evaluate(lat, lon, accuracy_m, sites) -> dict:
    """يحكم على قراءة موقع مقابل قائمة مواقع العمل.

    sites: قواميس فيها site_id و latitude و longitude و radius_m.
    المستدعي يمرّر المواقع النشطة وحدها — هذه الدالة لا تعرف
    القاعدة ولا تصفّي بالحالة.

    يعيد دائماً قاموساً مكتملاً. لا يرمي استثناءً على قراءة سيئة:
    القراءة السيئة حكمٌ لا عطل.

    عند التداخل يُختار الأقرب إلى المركز، وعند التساوي يُختار
    الأصغر معرّفاً — حتميّ لا عشوائي، فإعادة الحساب تعطي النتيجة
    نفسها عند مراجعة نزاع.
    """
    out = {
        "verdict": VERDICT_REJECTED,
        "reason": "",
        "site_id": None,
        "site_name": None,
        "distance_m": None,
        "accuracy_m": _finite(accuracy_m),
        "nearest": [],
    }

    bad = check_reading(lat, lon, accuracy_m)
    if bad:
        out["reason"] = bad
        return out

    usable = []
    for s in sites or []:
        # صفّ تالف أو None وسط القائمة لا يُسقط الحكم كله: المستدعي
        # يمرّر ما قرأه من القاعدة، ولا نفترض سلامة كل صفّ فيها.
        if not isinstance(s, dict) or not s.get("site_id"):
            continue
        if not valid_coordinates(s.get("latitude"), s.get("longitude")):
            continue
        if not valid_radius(s.get("radius_m")):
            continue
        usable.append(s)

    if not usable:
        # ليست قراءة فاسدة — لا يوجد ما نقيس عليه. نميّزها بالسبب
        # لأنها خلل في الإعداد لا في الموظف.
        out["verdict"] = VERDICT_OUTSIDE
        out["reason"] = "لا توجد مواقع عمل معرّفة"
        return out

    measured = sorted(
        (
            {
                "site_id": s["site_id"],
                "site_name": s.get("name") or "",
                "distance_m": round(distance_m(lat, lon, s["latitude"], s["longitude"]), 1),
                "radius_m": float(s["radius_m"]),
            }
            for s in usable
        ),
        key=lambda m: (m["distance_m"], str(m["site_id"])),
    )

    out["nearest"] = measured[:3]

    inside = [m for m in measured if m["distance_m"] <= m["radius_m"]]
    chosen = inside[0] if inside else measured[0]

    out["site_id"] = chosen["site_id"]
    out["site_name"] = chosen["site_name"]
    out["distance_m"] = chosen["distance_m"]

    if inside:
        out["verdict"] = VERDICT_INSIDE
        out["reason"] = ""
    else:
        out["verdict"] = VERDICT_OUTSIDE
        out["reason"] = (
            f"أقرب موقع «{chosen['site_name']}» يبعد "
            f"{round(chosen['distance_m'])} متراً، والنطاق "
            f"{round(chosen['radius_m'])} متراً"
        )
    return out
