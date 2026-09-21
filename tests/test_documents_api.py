"""نقاط المستندات: الصلاحية · البيانات الوصفية · حالات الفشل.

إخفاء زر الرفع في الواجهة ليس حماية — كل فحص هنا يستدعي
الـ API مباشرة كما يفعل أي عميل يعرف العنوان.
"""

import io

import docx
import pytest


def make_docx(text="سياسة تجريبية للاختبار."):
    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return {"file": ("policy.docx", buf.getvalue(),
                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}


# ---------- الصلاحية ----------

def test_employee_cannot_upload(api_client, employee):
    r = api_client.post("/documents", files=make_docx(), headers=employee)
    assert r.status_code == 403


def test_employee_cannot_delete(api_client, employee):
    assert api_client.delete("/documents/anything", headers=employee).status_code == 403


def test_employee_can_list(api_client, employee):
    """القراءة مسموحة — الحماية على التعديل لا الاطلاع."""
    r = api_client.get("/documents", headers=employee)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_manager_can_upload(api_client, manager):
    r = api_client.post("/documents", files=make_docx(), headers=manager)
    assert r.status_code == 201


def test_manager_delete_passes_authorization(api_client, manager):
    """404 يعني تجاوز فحص الدور ووصل لفحص الوجود."""
    assert api_client.delete("/documents/no-such-hash", headers=manager).status_code == 404


@pytest.mark.parametrize("method,path", [
    ("get", "/documents"),
    ("delete", "/documents/x"),
])
def test_unauthenticated_is_rejected(api_client, method, path):
    assert getattr(api_client, method)(path).status_code == 401


def test_unauthenticated_upload_is_rejected(api_client):
    assert api_client.post("/documents", files=make_docx()).status_code == 401


# ---------- الهوية لا تُؤخذ من الطلب ----------

def test_role_cannot_be_escalated_via_payload(api_client, employee):
    """حقول في الجسم لا ترفع الصلاحية — الدور من الرمز وحده."""
    r = api_client.post("/documents",
                        files=make_docx(),
                        data={"role": "manager", "user": "noura"},
                        headers=employee)
    assert r.status_code == 403


def test_hash_traversal_does_not_bypass_role(api_client, employee):
    for bad in ["../secret", "%2e%2e%2fsecret", "a/../../b"]:
        assert api_client.delete("/documents/" + bad, headers=employee).status_code in (403, 404)


# ---------- البيانات الوصفية ----------

def test_document_fields_are_present(api_client, manager):
    api_client.post("/documents", files=make_docx(), headers=manager)
    docs = api_client.get("/documents", headers=manager).json()
    if not docs:
        pytest.skip("لا مستندات في هذي البيئة")

    d = docs[0]
    for field in ("doc_hash", "filename", "chunks", "file_type", "uploaded_at", "uploaded_by"):
        assert field in d, field


def test_file_type_is_derived_from_name(api_client, manager):
    api_client.post("/documents", files=make_docx(), headers=manager)
    docs = api_client.get("/documents", headers=manager).json()
    if not docs:
        pytest.skip("لا مستندات")
    assert all(d["file_type"] == d["filename"].rsplit(".", 1)[-1].lower()
               for d in docs if "." in d["filename"])


def test_listing_never_leaks_internals(api_client, employee):
    """لا مسارات ولا معرّفات متجهات ولا تفاصيل تنفيذ."""
    body = api_client.get("/documents", headers=employee).text
    for leak in ["/mnt/", "C:\\\\", "vectorstore", "chroma", "embedding", "password_hash"]:
        assert leak.lower() not in body.lower(), leak


# ---------- حالات الرفع ----------

def test_unsupported_type_is_rejected(api_client, manager):
    r = api_client.post("/documents",
                        files={"file": ("notes.txt", b"plain text", "text/plain")},
                        headers=manager)
    assert r.status_code == 415


def test_empty_file_is_rejected(api_client, manager):
    r = api_client.post("/documents",
                        files={"file": ("empty.docx", b"", "application/octet-stream")},
                        headers=manager)
    assert r.status_code == 400


def test_oversized_file_is_rejected(api_client, manager):
    import api
    big = b"x" * (api.MAX_UPLOAD_BYTES + 2048)
    r = api_client.post("/documents",
                        files={"file": ("huge.docx", big, "application/octet-stream")},
                        headers=manager)
    assert r.status_code == 413


def test_upload_error_message_is_safe(api_client, manager):
    """رسالة الخطأ لا تكشف مساراً ولا أثر تنفيذ."""
    r = api_client.post("/documents",
                        files={"file": ("x.txt", b"text", "text/plain")},
                        headers=manager)
    detail = r.json().get("detail", "")
    assert "Traceback" not in detail
    assert "/" not in detail or "المدعوم" in detail


# ---------- سجل الرفع ----------

def test_upload_history_reads_existing_events(api_client, manager):
    """تاريخ الرفع من سجل الأحداث — لا جدول جديد."""
    import metrics
    api_client.post("/documents", files=make_docx(), headers=manager)
    history = metrics.upload_history(api_client._db)
    assert isinstance(history, dict)


def test_upload_history_survives_bad_path():
    import metrics
    from pathlib import Path
    assert metrics.upload_history(Path("/nonexistent/x.db")) == {}
