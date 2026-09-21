"""البحث الهجين: التطبيع العربي · التقطيع · دمج RRF.

خلفية: `hybrid.py` لم يكن مغطّى بأي اختبار رغم أنه يحدد
ما يراه النموذج. خطأ في التطبيع يُسقط نصف المطابقات بصمت.
"""

import pytest

import hybrid


# ---------- التطبيع ----------

@pytest.mark.parametrize("raw,expected", [
    ("الإجازة", "الاجازه"),
    ("الاجازة", "الاجازه"),
    ("الأجازة", "الاجازه"),
    ("أحمد",   "احمد"),
    ("إبراهيم", "ابراهيم"),
    ("مُوَظَّف", "موظف"),
    ("عربيّة",  "عربيه"),
])
def test_normalization_unifies_variants(raw, expected):
    assert hybrid.normalize_arabic(raw) == expected


def test_variants_collapse_to_same_form():
    """BM25 يطابق الحروف — بلا تطابق الأشكال يفشل نصف البحث."""
    forms = ["الإجازة", "الاجازة", "الأجازة", "الاجازه"]
    assert len({hybrid.normalize_arabic(f) for f in forms}) == 1


def test_normalization_leaves_plain_text_alone():
    assert hybrid.normalize_arabic("محاسب") == "محاسب"


# ---------- التقطيع ----------

def test_arabic_question_mark_is_stripped():
    """علامات الترقيم العربية داخل نطاق يونيكود العربي، فلا يستبعدها \\w.

    بدون استثنائها صراحةً تلتصق: "الدوسري؟" لا يطابق "الدوسري".
    """
    assert "الدوسري" in hybrid.tokenize("كم رصيد خالد الدوسري؟")
    assert "الدوسري؟" not in hybrid.tokenize("كم رصيد خالد الدوسري؟")


@pytest.mark.parametrize("punct", ["،", "؛", "؟", "٪"])
def test_all_arabic_punctuation_stripped(punct):
    assert hybrid.tokenize("كلمة" + punct) == ["كلمه"]


def test_question_matches_document_tokens():
    """الاختبار الذي يهم: هل يلتقي السؤال بالمقطع؟"""
    q = set(hybrid.tokenize("كم رصيد الإجازة لخالد الدوسري؟"))
    d = set(hybrid.tokenize("[أرصدة الإجازات] اسم الموظف: خالد الدوسري، المتبقي: 8"))
    assert "الدوسري" in q & d


def test_single_characters_dropped():
    assert "و" not in hybrid.tokenize("و الموظف")


def test_identifiers_split_on_hyphen():
    """EMP-003 يصير ['emp','003'] — والرقم هو المميّز."""
    t = hybrid.tokenize("EMP-003")
    assert "emp" in t and "003" in t


def test_empty_input_is_safe():
    assert hybrid.tokenize("") == []
    assert hybrid.tokenize("؟ ، ؛") == []


# ---------- دمج RRF ----------

def test_rrf_rewards_agreement():
    """المقطع الذي تتفق عليه الطريقتان يتقدّم على المتفوّق في واحدة."""
    both = hybrid.reciprocal_rank_fusion([["x"], ["x"]])
    one = hybrid.reciprocal_rank_fusion([["y"], ["z"]])
    assert both[0][1] > one[0][1] * 1.9


def test_rrf_preserves_rank_order():
    fused = hybrid.reciprocal_rank_fusion([["a", "b", "c"]])
    assert [d for d, _ in fused] == ["a", "b", "c"]


def test_rrf_rescues_keyword_only_match():
    """قيمة الهجين: يرفع ما وجدته طريقة واحدة فقط.

    معرّف مثل EMP-003 بلا معنى دلالي — الدلالي يضعه أخيراً.
    """
    semantic = ["d4", "d5", "d3", "d2", "d1"]
    keyword = ["d1", "d2"]
    order = [d for d, _ in hybrid.reciprocal_rank_fusion([semantic, keyword])]
    assert order[0] == "d1"
    assert order.index("d1") < semantic.index("d1")


def test_rrf_merges_disjoint_lists():
    fused = hybrid.reciprocal_rank_fusion([["a"], ["b"]])
    assert {d for d, _ in fused} == {"a", "b"}


def test_rrf_handles_empty_input():
    assert hybrid.reciprocal_rank_fusion([]) == []
    assert hybrid.reciprocal_rank_fusion([[], []]) == []


def test_rrf_constant_dampens_top_rank_dominance():
    """k كبير يقلّل أفضلية المركز الأول — سلوك RRF المقصود."""
    small = hybrid.reciprocal_rank_fusion([["a", "b"]], k=1)
    large = hybrid.reciprocal_rank_fusion([["a", "b"]], k=1000)
    gap = lambda f: f[0][1] - f[1][1]
    assert gap(small) > gap(large)


# ---------- الفهرس ----------

class FakeCollection:
    """مجموعة صغيرة بلا Chroma ولا نموذج تضمين."""

    def __init__(self, docs):
        self._ids = [f"d{i}" for i in range(len(docs))]
        self._docs = docs

    def count(self):
        return len(self._docs)

    def get(self, include=None):
        return {
            "ids": self._ids,
            "documents": self._docs,
            "metadatas": [{"source": "f.docx", "location": f"قسم {i}"}
                          for i in range(len(self._docs))],
        }


DOCS = [
    "[أرصدة الإجازات] الرقم الوظيفي: EMP-003، اسم الموظف: خالد الدوسري، المتبقي: 8",
    "[أرصدة الإجازات] الرقم الوظيفي: EMP-001، اسم الموظف: أحمد الشمري، المتبقي: 18",
    "قسم ثالثاً: العمل عن بعد — يحق للموظف العمل عن بعد يومين أسبوعياً",
    "قسم ثانياً: الإجازات — الإجازة السنوية ثلاثون يوماً لكل موظف",
]


def test_index_finds_exact_identifier():
    col = FakeCollection(DOCS)
    hybrid.invalidate()
    hits = hybrid._index.search(col, "EMP-003", 5)
    assert hits and hits[0][0] == "d0"


def test_index_finds_name():
    col = FakeCollection(DOCS)
    hybrid.invalidate()
    ids = [i for i, _ in hybrid._index.search(col, "خالد الدوسري", 5)]
    assert "d0" in ids


def test_index_rebuilds_when_collection_changes():
    """المستند المحذوف يجب ألا يبقى في الفهرس."""
    col = FakeCollection(DOCS)
    hybrid.invalidate()
    hybrid._index.ensure(col)
    assert hybrid._index._count == 4

    hybrid._index.ensure(FakeCollection(DOCS[:2]))
    assert hybrid._index._count == 2


def test_invalidate_forces_rebuild():
    col = FakeCollection(DOCS)
    hybrid.invalidate()
    hybrid._index.ensure(col)
    hybrid.invalidate()
    assert hybrid._index._bm25 is None


def test_empty_query_returns_nothing():
    col = FakeCollection(DOCS)
    hybrid.invalidate()
    assert hybrid._index.search(col, "؟", 5) == []


def test_empty_collection_is_safe():
    hybrid.invalidate()
    assert hybrid.hybrid_search(FakeCollection([]), "أي سؤال") == []
