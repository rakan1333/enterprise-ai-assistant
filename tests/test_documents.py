"""اختبارات معالجة المستندات — لا تلمس الشبكة ولا النماذج."""

import io

import docx
import openpyxl
import pytest

import rag_engine as engine


# ---------- البصمة ----------

def test_hash_is_deterministic():
    """نفس المحتوى يعطي نفس البصمة دائماً — أساس منع التكرار."""
    data = "محتوى تجريبي".encode("utf-8")
    assert engine.file_hash(data) == engine.file_hash(data)


def test_hash_differs_for_different_content():
    assert engine.file_hash(b"alpha") != engine.file_hash(b"beta")


def test_hash_ignores_filename():
    """البصمة من المحتوى لا الاسم — رفع نفس الملف باسمين يُكشف."""
    same = b"identical bytes"
    assert engine.file_hash(same) == engine.file_hash(same)


# ---------- التقطيع ----------

def test_chunks_respect_size_limit():
    long_text = "جملة قصيرة. " * 200
    chunks = engine.chunk_sections([(1, "قسم: اختبار", long_text)])
    assert chunks, "يجب أن ينتج التقطيع قطعاً"
    for _, _, text in chunks:
        assert len(text) <= engine.CHUNK_SIZE + 50


def test_chunks_carry_location_forward():
    """كل قطعة تحتفظ بموقعها — بدونه لا يمكن الاستشهاد بدقة."""
    chunks = engine.chunk_sections([(3, "قسم: الإجازات", "نص " * 400)])
    assert all(loc == "قسم: الإجازات" for _, loc, _ in chunks)
    assert all(num == 3 for num, _, _ in chunks)


def test_empty_sections_are_dropped():
    chunks = engine.chunk_sections([(1, "قسم: فارغ", "   "), (2, "قسم: مفيد", "محتوى حقيقي")])
    assert len(chunks) == 1
    assert chunks[0][1] == "قسم: مفيد"


def test_short_text_yields_single_chunk():
    chunks = engine.chunk_sections([(1, "قسم: قصير", "جملة واحدة فقط.")])
    assert len(chunks) == 1


# ---------- Word ----------

def _make_docx(paragraphs, table_rows=None) -> bytes:
    d = docx.Document()
    for text, style in paragraphs:
        d.add_paragraph(text, style=style)
    if table_rows:
        t = d.add_table(rows=len(table_rows), cols=len(table_rows[0]))
        for i, row in enumerate(table_rows):
            for j, cell in enumerate(row):
                t.cell(i, j).text = cell
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_docx_splits_by_heading():
    """التقسيم حسب العناوين هو ما يجعل الاستشهاد قابلاً للتحقق."""
    raw = _make_docx([
        ("أولاً: ساعات العمل", "Heading 1"),
        ("من الثامنة حتى الخامسة.", None),
        ("ثانياً: الإجازات", "Heading 1"),
        ("ثلاثون يوماً سنوياً.", None),
    ])
    sections = engine.extract_docx(raw)
    locations = [loc for _, loc, _ in sections]
    assert "قسم: أولاً: ساعات العمل" in locations
    assert "قسم: ثانياً: الإجازات" in locations


def test_docx_locations_are_distinct():
    """مواقع متطابقة تعني استشهاداً لا يمكن التحقق منه."""
    raw = _make_docx([
        ("القسم الأول", "Heading 1"), ("محتوى أول", None),
        ("القسم الثاني", "Heading 1"), ("محتوى ثانٍ", None),
    ])
    locations = [loc for _, loc, _ in engine.extract_docx(raw)]
    assert len(locations) == len(set(locations))


def test_docx_captures_tables():
    """جداول Word لا تظهر في paragraphs — تجاهلها يفقد المحتوى بصمت."""
    raw = _make_docx(
        [("سياسة الإجازات", "Heading 1"), ("تفاصيل أدناه.", None)],
        table_rows=[["نوع الإجازة", "الأيام"], ["أمومة", "70"]],
    )
    joined = " ".join(text for _, _, text in engine.extract_docx(raw))
    assert "أمومة" in joined
    assert "70" in joined


# ---------- Excel ----------

def _make_xlsx(rows, extra=None, sheet_title="بيانات") -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title
    for r in rows:
        ws.append(r)
    if extra:
        for cell, value in extra.items():
            ws[cell] = value
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_one_chunk_per_row():
    """صف واحد = قطعة واحدة — يطابق حبيبات السؤال."""
    raw = _make_xlsx([
        ["الاسم", "القسم", "الرصيد"],
        ["أحمد", "تقنية", 18],
        ["نورة", "موارد", 25],
        ["خالد", "مالية", 8],
    ])
    sections = engine.extract_xlsx(raw)
    assert len(sections) == 3


def test_xlsx_repeats_headers_in_each_row():
    """الرقم وحده بلا معنى — يجب أن يقترن باسم عموده."""
    raw = _make_xlsx([["الاسم", "الرصيد"], ["خالد", 8]])
    text = engine.extract_xlsx(raw)[0][2]
    assert "الاسم: خالد" in text
    assert "الرصيد: 8" in text


def test_xlsx_stops_at_gap_in_header_row():
    """إحصاءات بجانب الجدول لا يجب أن تتسرّب إلى الصفوف."""
    raw = _make_xlsx(
        [["الاسم", "الرصيد"], ["خالد", 8]],
        extra={"D1": "ملاحظة جانبية", "D2": "قيمة دخيلة"},
    )
    text = engine.extract_xlsx(raw)[0][2]
    assert "دخيلة" not in text


def test_xlsx_location_includes_row_number():
    raw = _make_xlsx([["الاسم"], ["خالد"], ["نورة"]])
    locations = [loc for _, loc, _ in engine.extract_xlsx(raw)]
    assert "صف 2" in locations[0]
    assert "صف 3" in locations[1]


def test_xlsx_skips_empty_sheet():
    assert engine.extract_xlsx(_make_xlsx([])) == []


# ---------- التوجيه حسب الصيغة ----------

def test_unsupported_extension_raises():
    with pytest.raises(ValueError, match="غير مدعومة"):
        engine.extract_any(b"anything", "notes.txt")


def test_extension_matching_is_case_insensitive():
    raw = _make_xlsx([["الاسم"], ["خالد"]])
    assert engine.extract_any(raw, "DATA.XLSX")
