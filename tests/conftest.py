"""تجهيزات مشتركة لاختبارات طبقة HTTP."""

import pytest


@pytest.fixture(scope="module")
def api_client(tmp_path_factory):
    """عميل اختبار بقاعدة معزولة وحسابات معروفة.

    نستبدل مسارات القواعد قبل استيراد api — الوحدات تلتقطها عند التحميل.
    """
    from fastapi.testclient import TestClient

    import auth
    import build_db
    import metrics
    import rag_engine
    import tools

    db = tmp_path_factory.mktemp("api") / "api_test.db"
    build_db.build(db)

    tools.DB_PATH = db
    auth.DB_PATH = db
    metrics.DB_PATH = db
    auth.init_db(db)
    metrics.init_db(db)

    # حسابان يتشاركان الاسم الأول — لاختبار الملكية بالمعرّف
    auth.create_user("khalid", "EMP-003", "خالد الدوسري", "Employee@123", "employee", db)
    auth.create_user("noura", "EMP-002", "نورة العتيبي", "Manager@123", "manager", db)
    auth.create_user("ahmed", "EMP-001", "أحمد الشمري", "Second@123", "employee", db)
    # مسؤول للاختبار: الصلاحية من الدور لا من الاسم
    auth.create_user("sysadmin", "EMP-100", "مسؤول النظام", "Admin@12345", "admin", db)

    # نستبدل طبقة الاسترجاع بكعب: اختبارات الصلاحيات لا تحتاج
    # نموذج تضمين ولا قاعدة متجهات، وتحميلهما يبطّئ بلا فائدة.
    class _StubCollection:
        def count(self):
            return 0

    _stub = _StubCollection()
    rag_engine.get_collection = lambda: _stub
    rag_engine.list_documents = lambda col: {}
    rag_engine.document_exists = lambda col, h: False
    rag_engine.ingest_file = lambda col, content, name: {
        "status": "added", "filename": name, "chunks": 1,
    }

    # النموذج الثلاثي يمنح المدير نطاق فريقه. مديرة بلا تقارير
    # لا ترى شيئاً — فنُسند لها فريقاً ليبقى ما تؤكّده الاختبارات
    # (العزل بين الموظفين، ووصول المدير) قابلاً للقياس.
    import org
    org.DB_PATH = db
    org.assign_manager("EMP-003", "EMP-002", path=db)
    org.assign_manager("EMP-001", "EMP-002", path=db)

    import api

    api.state["collection"] = _stub

    with TestClient(api.app) as client:
        client._db = db
        yield client


def _token(client, username, password):
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def employee(api_client):
    """خالد الدوسري — موظف."""
    return {"Authorization": "Bearer " + _token(api_client, "khalid", "Employee@123")}


@pytest.fixture(scope="module")
def other_employee(api_client):
    """أحمد الشمري — موظف آخر."""
    return {"Authorization": "Bearer " + _token(api_client, "ahmed", "Second@123")}


@pytest.fixture(scope="module")
def manager(api_client):
    """نورة العتيبي — مديرة."""
    return {"Authorization": "Bearer " + _token(api_client, "noura", "Manager@123")}


@pytest.fixture
def docx_upload():
    """ملف Word صغير صالح — لاختبار الرفع."""
    import io

    import docx

    d = docx.Document()
    d.add_paragraph("سياسة تجريبية لاختبار الصلاحيات.")
    buf = io.BytesIO()
    d.save(buf)
    return {"file": ("policy.docx", buf.getvalue(),
                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}


@pytest.fixture(scope="module")
def admin(api_client):
    """مسؤول النظام — نطاق المؤسسة."""
    return {"Authorization": "Bearer " + _token(api_client, "sysadmin", "Admin@12345")}
