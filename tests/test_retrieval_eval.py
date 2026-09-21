"""اختبارات مجموعة تقييم الاسترجاع.

نحقن مسترجِعاً اصطناعياً بدل نموذج التضمين: الاختبار يقيس
**منطق التقييم** حتمياً وسريعاً. تشغيل المجموعة على القاعدة
الحقيقية يتم بـ `python eval_retrieval.py`.
"""

import pytest

import eval_retrieval as ev


def chunk(source, text="", distance=0.12, location="قسم"):
    return {"text": text, "meta": {"source": source, "location": location},
            "distance": distance}


# ---------- المقاييس ----------

def test_perfect_hit_at_rank_one():
    case = ev.EVAL_SET[0]
    chunks = [chunk(case["expect_source"], "شروط العمل عن بعد: يومان أسبوعياً")]
    r = ev.evaluate_case(chunks, case, k=5)

    assert r["hit"] and r["rank"] == 1
    assert r["recall_at_k"] == 1.0
    assert r["rr"] == 1.0
    assert r["terms_found"]


def test_miss_when_expected_source_absent():
    case = ev.EVAL_SET[0]
    r = ev.evaluate_case([chunk("ملف_آخر.docx")] * 3, case, k=5)

    assert not r["hit"]
    assert r["recall_at_k"] == 0.0
    assert r["rr"] == 0.0
    assert r["precision_at_k"] == 0.0


def test_reciprocal_rank_reflects_position():
    """المركز الأول يساوي 1.0 والثالث 0.33 — يكشف التراجع التدريجي."""
    case = ev.EVAL_SET[0]
    chunks = [chunk("ملف_آخر.docx"), chunk("ثالث.xlsx"),
              chunk(case["expect_source"], "العمل عن بعد")]
    r = ev.evaluate_case(chunks, case, k=5)

    assert r["rank"] == 3
    assert round(r["rr"], 2) == 0.33


def test_precision_counts_relevant_share():
    case = ev.EVAL_SET[0]
    chunks = [chunk(case["expect_source"], "عن بعد"),
              chunk(case["expect_source"], "عن بعد"),
              chunk("ملف_آخر.docx"), chunk("ملف_آخر.docx")]
    r = ev.evaluate_case(chunks, case, k=4)

    assert r["precision_at_k"] == 0.5


def test_k_truncates_evaluation_window():
    """نتيجة خارج النافذة لا تُحتسب — وإلا خدعنا أنفسنا."""
    case = ev.EVAL_SET[0]
    chunks = [chunk("ملف_آخر.docx")] * 4 + [chunk(case["expect_source"], "عن بعد")]

    assert not ev.evaluate_case(chunks, case, k=3)["hit"]
    assert ev.evaluate_case(chunks, case, k=5)["hit"]


def test_terms_check_catches_wrong_chunk_of_right_file():
    """المستند صحيح والمقطع خاطئ — فخّ يخفي مشكلة تقطيع."""
    case = ev.EVAL_SET[0]
    r = ev.evaluate_case([chunk(case["expect_source"], "نص لا صلة له")], case, k=5)

    assert r["hit"]
    assert not r["terms_found"]


def test_empty_retrieval_is_handled():
    r = ev.evaluate_case([], ev.EVAL_SET[0], k=5)
    assert r["retrieved"] == 0 and not r["hit"] and r["precision_at_k"] == 0.0


# ---------- الملخّص ----------

def test_summary_averages_and_lists_misses():
    results = [
        {"id": "a", "hit": True,  "recall_at_k": 1.0, "precision_at_k": 1.0, "rr": 1.0},
        {"id": "b", "hit": False, "recall_at_k": 0.0, "precision_at_k": 0.0, "rr": 0.0},
    ]
    s = ev.summarize(results)

    assert s["recall_at_k"] == 0.5
    assert s["mrr"] == 0.5
    assert s["misses"] == ["b"]


def test_summary_handles_empty_input():
    assert ev.summarize([])["cases"] == 0


# ---------- التشغيل الكامل بمسترجِع محقون ----------

def _fake_retriever(question, k):
    """يعيد المستند المتوقّع لكل حالة إيجابية، ولا شيء للسلبية."""
    for case in ev.EVAL_SET:
        if case["question"] == question:
            text = " ".join(case["expect_terms"])
            return [chunk(case["expect_source"], text, 0.11)][:k]
    return []


def test_full_run_with_perfect_retriever():
    report = ev.run(k=5, retriever=_fake_retriever)

    assert report["summary"]["recall_at_k"] == 1.0
    assert report["summary"]["mrr"] == 1.0
    assert not report["summary"]["misses"]
    assert all(r["terms_found"] for r in report["positive"])


def test_full_run_with_broken_retriever():
    """مسترجِع يعيد ملفاً خاطئاً دائماً — يجب أن تسقط المقاييس."""
    broken = lambda q, k: [chunk("ملف_خاطئ.docx", "نص")][:k]
    report = ev.run(k=5, retriever=broken)

    assert report["summary"]["recall_at_k"] == 0.0
    assert len(report["summary"]["misses"]) == len(ev.EVAL_SET)


def test_out_of_scope_questions_return_nothing():
    """السلبية تحرس الطرف الآخر: نظام يعيد كل شيء عديم الفائدة."""
    report = ev.run(k=5, retriever=_fake_retriever)

    assert len(report["negative"]) == len(ev.NEGATIVE_SET)
    for r in report["negative"]:
        assert r["retrieved"] == 0
        assert r["min_distance"] == 1.0


def test_negative_set_flags_overconfident_retriever():
    """مسترجِع يجيب على كل شيء — السلبية تكشفه بمسافة قريبة."""
    greedy = lambda q, k: [chunk("أي_ملف.docx", "نص", 0.05)]
    report = ev.run(k=5, retriever=greedy)

    assert all(r["min_distance"] < 0.2 for r in report["negative"])


def test_eval_set_is_well_formed():
    """المجموعة نفسها تُفحص — حالة ناقصة تعطي ثقة زائفة."""
    ids = [c["id"] for c in ev.EVAL_SET]
    assert len(ids) == len(set(ids))

    for c in ev.EVAL_SET:
        assert c["question"].strip()
        assert c["expect_source"].strip()
        assert c["expect_terms"]

    assert len({c["expect_source"] for c in ev.EVAL_SET}) >= 2
