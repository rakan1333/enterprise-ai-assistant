"""السياج الجغرافي — دوال خالصة، فتُختبر كدوال بلا قاعدة بيانات.

مبدأ حاكم: كل مدخل شاذّ له مخرج معرَّف. لا واحد منها يرمي
استثناءً غير متوقَّع، ولا واحد منها يختار موقعاً بصمت.
"""

import math

import pytest

import geofence as g

# الرياض — نقطة مرجعية، وخط عرضها 24° يجعل فرق haversine عن
# الحساب المستوي ملموساً، وهو المقصود من اختياره.
RIYADH = (24.7136, 46.6753)


def site(sid, lat, lon, radius=150, name=""):
    return {"site_id": sid, "name": name or sid,
            "latitude": lat, "longitude": lon, "radius_m": radius}


# ---------- المسافة ----------

def test_distance_to_self_is_zero():
    assert g.distance_m(*RIYADH, *RIYADH) == pytest.approx(0, abs=1e-6)


def test_distance_is_symmetric():
    a, b = RIYADH, (24.7200, 46.6800)
    assert g.distance_m(*a, *b) == pytest.approx(g.distance_m(*b, *a))


def test_one_degree_of_latitude_is_about_111km():
    """مرجع معروف: درجة عرض ≈ 111.2 كم في كل مكان."""
    d = g.distance_m(24.0, 46.0, 25.0, 46.0)
    assert 110_500 < d < 111_500, d


def test_longitude_shrinks_with_latitude():
    """جوهر اختيار haversine: درجة الطول تضيق كلما ابتعدنا شمالاً.

    عند 24° تساوي نحو cos(24°) = 0.91 من عرضها عند الاستواء.
    الحساب المستوي يتجاهل ذلك فيوسّع السياج شرقاً وغرباً.
    """
    at_equator = g.distance_m(0.0, 46.0, 0.0, 47.0)
    at_riyadh = g.distance_m(24.0, 46.0, 24.0, 47.0)
    assert at_riyadh == pytest.approx(at_equator * math.cos(math.radians(24)), rel=0.01)


def test_distance_rejects_invalid_coordinates():
    for bad in [(91.0, 46.0), (-91.0, 46.0), (24.0, 181.0), (24.0, -181.0)]:
        with pytest.raises(g.GeofenceError):
            g.distance_m(*bad, *RIYADH)


def test_distance_rejects_nan_and_text():
    for bad in [(float("nan"), 46.0), (float("inf"), 46.0), ("شمال", 46.0), (None, 46.0)]:
        with pytest.raises(g.GeofenceError):
            g.distance_m(*bad, *RIYADH)


# ---------- صلاحية الإحداثيات ----------

@pytest.mark.parametrize("lat,lon", [(0, 0), (90, 180), (-90, -180), (24.7136, 46.6753)])
def test_valid_coordinates_accepts_the_whole_globe(lat, lon):
    assert g.valid_coordinates(lat, lon)


@pytest.mark.parametrize("lat,lon", [
    (90.1, 0), (-90.1, 0), (0, 180.1), (0, -180.1),
    ("x", 0), (None, 0), (float("nan"), 0), (float("inf"), 0),
])
def test_valid_coordinates_rejects_the_rest(lat, lon):
    assert not g.valid_coordinates(lat, lon)


@pytest.mark.parametrize("r", [g.MIN_RADIUS_M, 150, g.MAX_RADIUS_M])
def test_valid_radius_accepts_the_range(r):
    assert g.valid_radius(r)


@pytest.mark.parametrize("r", [0, -1, g.MIN_RADIUS_M - 1, g.MAX_RADIUS_M + 1,
                               None, "واسع", float("nan")])
def test_valid_radius_rejects_outside(r):
    assert not g.valid_radius(r)


# ---------- فحص القراءة ----------

def test_good_reading_passes():
    assert g.check_reading(*RIYADH, 12.0) == ""


def test_missing_accuracy_is_rejected():
    """قراءة بلا دقّة لا تُقيَّم — قبولها يعني قبول أي إحداثيات."""
    assert "دقّة" in g.check_reading(*RIYADH, None)


def test_negative_accuracy_is_rejected():
    assert g.check_reading(*RIYADH, -5) != ""


def test_coarse_accuracy_is_rejected():
    assert "كافية" in g.check_reading(*RIYADH, g.MAX_ACCURACY_M + 1)


def test_accuracy_exactly_at_the_cap_passes():
    assert g.check_reading(*RIYADH, g.MAX_ACCURACY_M) == ""


def test_invalid_coordinates_reported_before_accuracy():
    assert g.check_reading(999, 999, 5) == "إحداثيات غير صالحة"


# ---------- الحكم ----------

def test_point_at_the_centre_is_inside():
    r = g.evaluate(*RIYADH, 10, [site("S1", *RIYADH)])
    assert r["verdict"] == g.VERDICT_INSIDE
    assert r["site_id"] == "S1"
    assert r["distance_m"] == pytest.approx(0, abs=1)


def test_point_well_outside_is_outside():
    far = (24.8000, 46.6753)          # نحو 9.6 كم شمالاً
    r = g.evaluate(*far, 10, [site("S1", *RIYADH, radius=150)])
    assert r["verdict"] == g.VERDICT_OUTSIDE
    assert r["distance_m"] > 9000


def test_boundary_is_inclusive():
    """النقطة على الحدّ تماماً داخل النطاق — قرار صريح لا صدفة."""
    s = site("S1", *RIYADH, radius=150)
    # نحرّك خط العرض بما يعطي 150 متراً بالضبط
    delta = 150.0 / (math.pi / 180 * g.EARTH_RADIUS_M)
    r = g.evaluate(RIYADH[0] + delta, RIYADH[1], 5, [s])
    assert r["distance_m"] == pytest.approx(150, abs=0.5)
    assert r["verdict"] == g.VERDICT_INSIDE


def test_just_outside_the_boundary_is_outside():
    s = site("S1", *RIYADH, radius=150)
    delta = 160.0 / (math.pi / 180 * g.EARTH_RADIUS_M)
    r = g.evaluate(RIYADH[0] + delta, RIYADH[1], 5, [s])
    assert r["verdict"] == g.VERDICT_OUTSIDE


def test_bad_reading_is_rejected_not_outside():
    """الرفض غير الخروج: الأول عيب في القراءة، الثاني موقع الموظف."""
    r = g.evaluate(*RIYADH, 5000, [site("S1", *RIYADH)])
    assert r["verdict"] == g.VERDICT_REJECTED
    assert r["site_id"] is None


def test_invalid_coordinates_are_rejected():
    r = g.evaluate(999, 999, 5, [site("S1", *RIYADH)])
    assert r["verdict"] == g.VERDICT_REJECTED


def test_no_sites_configured_is_reported_distinctly():
    """خلل في الإعداد لا في الموظف — السبب يفرّق بينهما."""
    r = g.evaluate(*RIYADH, 10, [])
    assert r["verdict"] == g.VERDICT_OUTSIDE
    assert "لا توجد مواقع" in r["reason"]
    assert r["site_id"] is None


def test_sites_with_broken_data_are_skipped_not_fatal():
    broken = [
        {"site_id": "BAD1", "name": "x", "latitude": 999, "longitude": 0, "radius_m": 150},
        {"site_id": "BAD2", "name": "y", "latitude": 24.7, "longitude": 46.6, "radius_m": 0},
        site("GOOD", *RIYADH),
    ]
    r = g.evaluate(*RIYADH, 10, broken)
    assert r["verdict"] == g.VERDICT_INSIDE
    assert r["site_id"] == "GOOD"


def test_all_sites_broken_behaves_like_none():
    broken = [{"site_id": "BAD", "name": "x", "latitude": "شمال",
               "longitude": 0, "radius_m": 150}]
    r = g.evaluate(*RIYADH, 10, broken)
    assert r["verdict"] == g.VERDICT_OUTSIDE
    assert "لا توجد مواقع" in r["reason"]


# ---------- التداخل ----------

def test_overlapping_fences_pick_the_nearest_centre():
    near = site("NEAR", 24.7137, 46.6753, radius=500)
    far = site("FAR", 24.7200, 46.6753, radius=2000)
    r = g.evaluate(*RIYADH, 10, [far, near])
    assert r["verdict"] == g.VERDICT_INSIDE
    assert r["site_id"] == "NEAR"


def test_overlap_choice_is_order_independent():
    a = site("A", 24.7137, 46.6753, radius=500)
    b = site("B", 24.7200, 46.6753, radius=2000)
    assert g.evaluate(*RIYADH, 10, [a, b])["site_id"] == \
           g.evaluate(*RIYADH, 10, [b, a])["site_id"]


def test_exact_tie_breaks_on_site_id_deterministically():
    """موقعان بالمسافة نفسها: الأصغر معرّفاً. حتميّ ليُعاد الحساب عند النزاع."""
    s1 = site("AAA", *RIYADH, radius=300)
    s2 = site("BBB", *RIYADH, radius=300)
    assert g.evaluate(*RIYADH, 10, [s2, s1])["site_id"] == "AAA"
    assert g.evaluate(*RIYADH, 10, [s1, s2])["site_id"] == "AAA"


def test_outside_all_reports_the_nearest_one():
    a = site("A", 24.8000, 46.6753, radius=100, name="موقع أ")
    b = site("B", 25.5000, 46.6753, radius=100, name="موقع ب")
    r = g.evaluate(*RIYADH, 10, [b, a])
    assert r["verdict"] == g.VERDICT_OUTSIDE
    assert r["site_id"] == "A"
    assert "موقع أ" in r["reason"]


def test_nearest_list_is_capped_and_sorted():
    many = [site(f"S{i}", 24.7136 + i / 100, 46.6753) for i in range(10)]
    r = g.evaluate(*RIYADH, 10, many)
    assert len(r["nearest"]) == 3
    assert [n["distance_m"] for n in r["nearest"]] == \
           sorted(n["distance_m"] for n in r["nearest"])


# ---------- شكل الردّ ----------

@pytest.mark.parametrize("args", [
    (RIYADH[0], RIYADH[1], 10, [site("S", *RIYADH)]),
    (RIYADH[0], RIYADH[1], 9999, [site("S", *RIYADH)]),
    (999, 999, 10, []),
    (RIYADH[0], RIYADH[1], None, None),
])
def test_evaluate_always_returns_the_same_shape(args):
    """المستدعي يكتب في قاعدة بيانات — لا يحتمل مفتاحاً ناقصاً."""
    r = g.evaluate(*args)
    assert set(r) == {"verdict", "reason", "site_id", "site_name",
                      "distance_m", "accuracy_m", "nearest"}
    assert r["verdict"] in (g.VERDICT_INSIDE, g.VERDICT_OUTSIDE, g.VERDICT_REJECTED)


def test_evaluate_never_raises_on_junk():
    for junk in [None, "nowhere", 42, [{"site_id": "X"}], [None]]:
        try:
            g.evaluate(*RIYADH, 10, junk if isinstance(junk, list) else [])
        except Exception as e:                      # noqa: BLE001
            pytest.fail(f"رمى استثناءً على {junk!r}: {e}")


def test_reported_accuracy_is_echoed_back():
    """نحفظ ما أبلغ به الجهاز — مادة المراجعة عند النزاع."""
    assert g.evaluate(*RIYADH, 37.5, [site("S", *RIYADH)])["accuracy_m"] == 37.5
