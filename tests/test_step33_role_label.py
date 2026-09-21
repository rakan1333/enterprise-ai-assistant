"""الخطوة 3.3 — وصف الدور الذي يُخبَر به النموذج.

كان agent.py يحوّل كل دور غير "manager" إلى "موظف"، فأُخبر
النموذج أن المسؤول موظف عادي وأجاب على هذا الأساس.

الأمان لم يتأثر: الصلاحية تُفرض في الأدوات من الرمز. الخلل
في ما يعتقده النموذج عن المستخدم — أي في التجربة.
"""

import pytest

import agent


@pytest.fixture
def captured(monkeypatch):
    """نعترض أول استدعاء للنموذج ونعيد جواباً نهائياً بلا أدوات."""
    seen = {}

    def fake_llm(messages):
        seen["system"] = messages[0]["content"]
        return {"role": "assistant", "content": "حسناً."}, 0

    monkeypatch.setattr(agent, "_call_llm", fake_llm)
    return seen


def run(role, captured):
    agent.run_agent(None, "سؤال", actor={"employee_id": "EMP-1", "name": "اسم", "role": role})
    return captured["system"]


def test_admin_is_described_as_admin(captured):
    """المسؤول لا يُوصَف بأنه موظف — الخلل الذي ظهر فعلاً."""
    system = run("admin", captured)
    assert "صلاحيته: مسؤول النظام" in system
    assert "صلاحيته: موظف" not in system


def test_manager_label_unchanged(captured):
    assert "صلاحيته: مدير" in run("manager", captured)


def test_employee_label_unchanged(captured):
    assert "صلاحيته: موظف" in run("employee", captured)


@pytest.mark.parametrize("role", ["Admin", "superadmin", "", None, "root"])
def test_unknown_role_falls_back_to_employee(role, captured):
    """دور غير معروف يُوصَف بأضيق وصف — كما يعامله نموذج الصلاحية."""
    assert "صلاحيته: موظف" in run(role, captured)


def test_no_actor_adds_no_role_line(captured):
    agent.run_agent(None, "سؤال")
    assert "صلاحيته" not in captured["system"]


def test_label_matches_authorization_scope():
    """الوصف يتبع نفس ترتيب الأدوار الذي تفرضه الصلاحية."""
    import auth
    assert set(auth.ROLES) == {"employee", "manager", "admin"}
