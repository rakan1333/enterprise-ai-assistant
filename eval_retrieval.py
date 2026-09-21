"""مجموعة تقييم صغيرة وحتمية للاسترجاع.

لماذا: النظام يملك 390 اختباراً تحرس الكود، ولا واحد يحرس **جودة
البحث**. تغيير في التقطيع أو نموذج التضمين قد يُفسد الاسترجاع بصمت —
وهذا حدث فعلاً: نموذج ترجمة استُخدم للبحث، فبقيت كل الاختبارات خضراء
بينما الإجابة الصحيحة في المركز الثامن.

المقاييس:
  Recall@K    — هل ظهر المستند المتوقّع ضمن أول K؟
  Precision@K — كم من نتائج أول K ذات صلة؟
  MRR         — كم كان ترتيب أول نتيجة صحيحة؟
  السلبية     — هل يعيد سؤال بلا إجابة نتائجَ فارغة أو بعيدة؟

التشغيل:
    python eval_retrieval.py              # على القاعدة الحالية
    pytest tests/test_retrieval_eval.py   # بمجموعة اصطناعية
"""

import argparse
import json
from pathlib import Path

DEFAULT_K = 5


# ---------- مجموعة التقييم ----------
# كل عنصر: سؤال + المستند المتوقّع + مقطع دالّ.
# لا إجابات مخزّنة — نقيس الاسترجاع لا التوليد.

EVAL_SET = [
    {
        "id": "remote-work",
        "question": "ما شروط العمل عن بعد؟",
        "expect_source": "سياسة_الموارد_البشرية.docx",
        "expect_terms": ["عن بعد"],
    },
    {
        "id": "annual-leave",
        "question": "كم يوماً الإجازة السنوية؟",
        "expect_source": "سياسة_الموارد_البشرية.docx",
        "expect_terms": ["إجاز"],
    },
    {
        "id": "working-hours",
        "question": "ما ساعات العمل الرسمية؟",
        "expect_source": "سياسة_الموارد_البشرية.docx",
        "expect_terms": ["ساعات", "العمل"],
    },
    {
        "id": "employee-by-name",
        "question": "كم رصيد إجازة خالد الدوسري؟",
        "expect_source": "بيانات_الموظفين.xlsx",
        "expect_terms": ["الدوسري"],
    },
    {
        "id": "transport-allowance",
        "question": "كم بدل الانتقال؟",
        "expect_source": "بيانات_الموظفين.xlsx",
        "expect_terms": ["انتقال"],
    },
]

# أسئلة خارج نطاق المستندات — يجب ألا تُسترجَع بثقة
NEGATIVE_SET = [
    {"id": "health-insurance", "question": "ما تفاصيل بوليصة التأمين الصحي؟"},
    {"id": "stock-options",    "question": "ما شروط أسهم الموظفين؟"},
    {"id": "unrelated",        "question": "ما عاصمة اليابان؟"},
]


# ---------- المقاييس ----------

def _source_of(chunk) -> str:
    meta = chunk.get("meta") or {}
    return meta.get("source", "")


def evaluate_case(chunks, case, k=DEFAULT_K):
    """يقيس حالة واحدة على أول k نتيجة."""
    top = chunks[:k]
    sources = [_source_of(c) for c in top]
    expected = case["expect_source"]

    hit_rank = next((i + 1 for i, s in enumerate(sources) if s == expected), 0)
    relevant = sum(1 for s in sources if s == expected)

    terms_ok = False
    if hit_rank:
        text = " ".join(c.get("text", "") for c in top if _source_of(c) == expected)
        terms_ok = all(t in text for t in case.get("expect_terms", []))

    return {
        "id": case["id"],
        "retrieved": len(top),
        "hit": bool(hit_rank),
        "rank": hit_rank,
        "recall_at_k": 1.0 if hit_rank else 0.0,
        "precision_at_k": (relevant / len(top)) if top else 0.0,
        "rr": (1.0 / hit_rank) if hit_rank else 0.0,
        "terms_found": terms_ok,
        "top_sources": sources,
    }


def summarize(results):
    n = len(results) or 1
    return {
        "cases": len(results),
        "recall_at_k": round(sum(r["recall_at_k"] for r in results) / n, 3),
        "precision_at_k": round(sum(r["precision_at_k"] for r in results) / n, 3),
        "mrr": round(sum(r["rr"] for r in results) / n, 3),
        "misses": [r["id"] for r in results if not r["hit"]],
    }


# ---------- التشغيل على النظام الحقيقي ----------

def run(k=DEFAULT_K, retriever=None):
    """يشغّل التقييم. retriever(question, k) -> list[chunk] للحقن في الاختبارات."""
    if retriever is None:
        import hybrid
        import rag_engine as engine

        collection = engine.get_collection()
        retriever = lambda q, top: hybrid.hybrid_search(collection, q, top_k=top)

    positive = [evaluate_case(retriever(c["question"], k), c, k) for c in EVAL_SET]

    negative = []
    for c in NEGATIVE_SET:
        chunks = retriever(c["question"], k)
        negative.append({
            "id": c["id"],
            "retrieved": len(chunks),
            # مسافة أدنى عالية تعني أن النظام لم يجد شيئاً قريباً
            "min_distance": round(min((x.get("distance", 1.0) for x in chunks), default=1.0), 4),
        })

    return {"k": k, "positive": positive, "negative": negative, "summary": summarize(positive)}


def main():
    ap = argparse.ArgumentParser(description="تقييم جودة الاسترجاع")
    ap.add_argument("-k", type=int, default=DEFAULT_K, help="عدد النتائج المقيَّمة")
    ap.add_argument("--json", action="store_true", help="مخرَج JSON")
    args = ap.parse_args()

    report = run(args.k)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    s = report["summary"]
    print(f"\nتقييم الاسترجاع عند K={report['k']}\n" + "─" * 58)
    for r in report["positive"]:
        mark = "✓" if r["hit"] else "✗"
        rank = f"المركز {r['rank']}" if r["rank"] else "غير موجود"
        print(f"  {mark} {r['id']:<22} {rank:<14} دقّة {r['precision_at_k']:.2f}")

    print("─" * 58)
    print(f"  Recall@{report['k']}    {s['recall_at_k']}")
    print(f"  Precision@{report['k']} {s['precision_at_k']}")
    print(f"  MRR           {s['mrr']}")
    if s["misses"]:
        print(f"  ⚠ لم تُسترجَع: {', '.join(s['misses'])}")

    print("\nأسئلة خارج النطاق (يجب ألا تُسترجَع بثقة)")
    for r in report["negative"]:
        print(f"  {r['id']:<22} نتائج {r['retrieved']} · أقرب مسافة {r['min_distance']}")
    print()


if __name__ == "__main__":
    main()
