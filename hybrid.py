"""بحث هجين — يدمج البحث الدلالي مع بحث الكلمات المفتاحية.

المشكلة: نماذج embedding تمثّل المعنى، والأسماء والمعرّفات بلا معنى دلالي.
"خالد الدوسري" و"أحمد الشمري" متجاوران في فضاء المتجهات لأن كليهما اسم شخص.

الحل: نضيف BM25 الذي يطابق الحروف، ثم ندمج القائمتين بـ RRF.
نقاط قوة كل طريقة هي نقاط ضعف الأخرى.
"""

import re
import threading

from rank_bm25 import BM25Okapi

from logging_config import get_logger

log = get_logger(__name__)

# ثابت RRF — 60 هي القيمة المعتادة في الأدبيات.
# الأصغر يشدّد على المراكز الأولى، والأكبر يسوّي بين المراكز.
RRF_K = 60

# كم نسحب من كل طريقة قبل الدمج. أوسع من الناتج النهائي
# ليجد الدمج فرصاً للاتفاق بين الطريقتين.
CANDIDATES = 20

_TASHKEEL = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u0640]")

# علامات الترقيم العربية تقع داخل نطاق يونيكود العربي، فلا يستبعدها \w.
# بدون استثنائها صراحةً تلتصق بالكلمة: "الدوسري؟" لا يطابق "الدوسري".
_AR_PUNCT = "\u060C\u061B\u061F\u066A\u066B\u066C\u066D\u06D4\u060D\u060E\u060F"
_NON_WORD = re.compile(rf"[^\w\u0600-\u06FF]+|[{_AR_PUNCT}]+")


def normalize_arabic(text: str) -> str:
    """يوحّد الأشكال المتغيّرة في العربية.

    BM25 يطابق الحروف حرفياً، فـ"الإجازة" و"الاجازة" كلمتان مختلفتان عنده.
    بدون هذا التطبيع يفشل البحث في نصف الحالات العربية.
    """
    t = _TASHKEEL.sub("", text)
    t = re.sub("[إأآا]", "ا", t)
    t = re.sub("[ىي]", "ي", t)
    t = t.replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return t


def tokenize(text: str) -> list[str]:
    """يقطّع النص إلى كلمات مطبَّعة."""
    t = normalize_arabic(text.lower())
    return [w for w in _NON_WORD.split(t) if len(w) > 1]


class BM25Index:
    """فهرس كلمات مفتاحية يُبنى مرة ويُعاد استخدامه.

    يُبطَل عند تغيّر عدد المقاطع — رفع مستند أو حذفه.
    """

    def __init__(self):
        self._bm25 = None
        self._ids: list[str] = []
        self._docs: list[str] = []
        self._metas: list[dict] = []
        self._count = -1
        self._lock = threading.Lock()

    def _rebuild(self, collection) -> None:
        data = collection.get(include=["documents", "metadatas"])
        self._ids = data["ids"]
        self._docs = data["documents"]
        self._metas = data["metadatas"]
        corpus = [tokenize(d) for d in self._docs]
        self._bm25 = BM25Okapi(corpus) if corpus else None
        self._count = len(self._ids)
        log.info("بُني فهرس BM25 — %d مقطع", self._count)

    def ensure(self, collection) -> None:
        """يبني الفهرس إن كان قديماً. آمن للاستدعاء المتكرر."""
        with self._lock:
            if self._bm25 is None or collection.count() != self._count:
                self._rebuild(collection)

    def search(self, collection, query: str, top_k: int) -> list[tuple[str, float]]:
        """يعيد (معرّف المقطع، الدرجة) مرتّبة تنازلياً."""
        self.ensure(collection)
        if self._bm25 is None:
            return []

        tokens = tokenize(query)
        if not tokens:
            return []

        scores = self._bm25.get_scores(tokens)
        ranked = sorted(zip(self._ids, scores), key=lambda x: -x[1])
        return [(doc_id, s) for doc_id, s in ranked[:top_k] if s > 0]

    def get(self, doc_id: str) -> tuple[str, dict] | None:
        try:
            i = self._ids.index(doc_id)
        except ValueError:
            return None
        return self._docs[i], self._metas[i]


_index = BM25Index()


def reciprocal_rank_fusion(
    rankings: list[list[str]], k: int = RRF_K
) -> list[tuple[str, float]]:
    """يدمج قوائم مرتّبة بالترتيب لا بالدرجة.

    البحث الدلالي يعطي مسافات (0.14) وBM25 يعطي درجات (7.3) —
    مقاييس لا تُجمع. RRF يتجاهلها ويستخدم الترتيب فقط:

        score = Σ 1 / (k + rank)

    فلا يحتاج معايرة، ولا تسحق درجةٌ شاذّةٌ الطريقةَ الأخرى،
    ويتقدّم المقطع الذي تتفق عليه الطريقتان.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


def hybrid_search(collection, query: str, top_k: int = 6) -> list[dict]:
    """يبحث دلالياً وبالكلمات المفتاحية، ثم يدمج بـ RRF."""
    if collection.count() == 0:
        log.warning("القاعدة فارغة — لا يمكن البحث")
        return []

    # الطريقة الأولى: دلالي
    n = min(CANDIDATES, collection.count())
    res = collection.query(query_texts=[query], n_results=n)
    semantic_ids = res["ids"][0]
    distances = dict(zip(semantic_ids, res["distances"][0]))
    lookup = {
        doc_id: (doc, meta)
        for doc_id, doc, meta in zip(semantic_ids, res["documents"][0], res["metadatas"][0])
    }

    # الطريقة الثانية: كلمات مفتاحية
    bm25_hits = _index.search(collection, query, CANDIDATES)
    bm25_ids = [doc_id for doc_id, _ in bm25_hits]
    bm25_scores = dict(bm25_hits)

    # الدمج
    fused = reciprocal_rank_fusion([semantic_ids, bm25_ids])

    out = []
    for doc_id, score in fused[:top_k]:
        if doc_id in lookup:
            doc, meta = lookup[doc_id]
        else:
            found = _index.get(doc_id)
            if found is None:
                continue
            doc, meta = found

        out.append({
            "text": doc,
            "meta": meta,
            "distance": distances.get(doc_id, 1.0),
            "rrf_score": round(score, 5),
            "matched": (
                "both" if doc_id in distances and doc_id in bm25_scores
                else "semantic" if doc_id in distances
                else "keyword"
            ),
        })

    log.info(
        "بحث هجين: %d دلالي + %d كلمات → %d نتيجة | اتفاق: %d",
        len(semantic_ids), len(bm25_ids), len(out),
        sum(1 for c in out if c["matched"] == "both"),
    )
    return out


def invalidate() -> None:
    """يُبطل الفهرس صراحةً — يُستدعى بعد رفع أو حذف مستند."""
    global _index
    _index = BM25Index()
    log.info("أُبطل فهرس BM25")
