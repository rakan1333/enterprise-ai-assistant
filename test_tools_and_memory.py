"""اختبارات أدوات قاعدة البيانات ومنطق ذاكرة المحادثة."""

import pytest

import agent
import build_db
import tools


@pytest.fixture(scope="module", autouse=True)
def fresh_database(tmp_path_factory):
    """قاعدة بيانات معزولة لكل تشغيل — لا نلمس بيانات التطوير."""
    db = tmp_path_factory.mktemp("db") / "test_company.db"
    build_db.build(db)
    original = tools.DB_PATH
    tools.DB_PATH = db
    yield
    tools.DB_PATH = original


# ---------- البحث عن موظف ----------

def test_partial_name_matches():
    """المستخدمون نادراً ما يكتبون الاسم الكامل."""
    res = tools.get_employee("خالد")
    assert res["found"]
    assert res["employee"]["name"] == "خالد الدوسري"


def test_full_name_matches():
    assert tools.get_employee("خالد الدوسري")["found"]


def test_unknown_name_returns_not_found():
    """لا يرمي استثناءً — يعيد نتيجة يفهمها النموذج ويشرحها للمستخدم."""
    res = tools.get_employee("شخص غير موجود")
    assert res["found"] is False
    assert "message" in res


def test_ambiguous_name_reports_all_matches():
    """التطابق المتعدد يجب أن يُطلب توضيحه لا أن يُخمّن."""
    res = tools.get_employee("ال")
    if res.get("multiple"):
        assert len(res["matches"]) > 1


# ---------- رصيد الإجازة ----------

def test_leave_balance_is_computed_not_stored():
    """الرقم الذي فشل فيه البحث الدلالي تماماً."""
    res = tools.get_leave_balance("خالد الدوسري")
    assert res["remaining"] == 8
    assert res["remaining"] == res["annual_leave"] - res["leave_used"]


def test_leave_balance_for_unused_leave():
    res = tools.get_leave_balance("سارة")
    assert res["remaining"] == 30


def test_leave_balance_propagates_not_found():
    assert tools.get_leave_balance("لا أحد")["found"] is False


# ---------- الأقسام ----------

def test_department_listing_filters_correctly():
    res = tools.list_department_employees("تقنية المعلومات")
    assert res["count"] == 2
    names = [e["name"] for e in res["employees"]]
    assert "أحمد الشمري" in names


def test_unknown_department_returns_empty_not_error():
    assert tools.list_department_employees("قسم وهمي")["count"] == 0


# ---------- الإجماليات ----------

def test_summary_total_is_exact():
    """الرقم الذي أخطأ فيه RAG: 51200 بدل 72000.

    السبب أن الاسترجاع رأى 3 أقسام من 5. الاستعلام يرى الخمسة دائماً.
    """
    assert tools.department_summary()["total_monthly_transport"] == 72000


def test_summary_covers_every_department():
    summary = tools.department_summary()
    assert len(summary["departments"]) == 5
    assert summary["total_headcount"] == 90


def test_summary_total_equals_sum_of_parts():
    """الإجمالي محسوب لا مكتوب — يبقى صحيحاً لو تغيّرت البيانات."""
    summary = tools.department_summary()
    assert summary["total_monthly_transport"] == sum(
        d["monthly_total"] for d in summary["departments"]
    )


# ---------- الآلة الحاسبة ----------

def test_calculator_evaluates_simple_expression():
    assert tools.calculate("800 * 24")["result"] == 19200


def test_calculator_rejects_code_execution():
    """eval بلا قيود ثغرة تنفيذ كود — القائمة البيضاء تمنعها."""
    res = tools.calculate("__import__('os').system('echo hacked')")
    assert "error" in res


def test_calculator_rejects_letters():
    assert "error" in tools.calculate("open('secrets.txt')")


def test_calculator_handles_division_by_zero():
    """لا ينهار — يعيد خطأً يفهمه النموذج."""
    assert "error" in tools.calculate("1 / 0")


# ---------- ذاكرة المحادثة ----------

def test_empty_history_is_handled():
    assert agent._clean_history(None) == []
    assert agent._clean_history([]) == []


def test_history_keeps_user_and_assistant_only():
    """نتائج الأدوات لا تُعاد — قد تكون قديمة وتضخّم السياق."""
    history = [
        {"role": "user", "content": "سؤال"},
        {"role": "assistant", "content": "إجابة"},
        {"role": "tool", "content": "نتيجة أداة"},
        {"role": "system", "content": "تعليمات"},
    ]
    roles = [m["role"] for m in agent._clean_history(history)]
    assert roles == ["user", "assistant"]


def test_history_drops_empty_messages():
    history = [
        {"role": "user", "content": "   "},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "سؤال حقيقي"},
    ]
    assert len(agent._clean_history(history)) == 1


def test_history_is_capped_by_message_count():
    """بلا سقف، التكلفة تنمو مع كل دورة في المحادثة."""
    long_history = [{"role": "user", "content": f"سؤال {i}"} for i in range(50)]
    assert len(agent._clean_history(long_history)) == agent.MAX_HISTORY_MESSAGES


def test_history_keeps_the_most_recent_messages():
    """الأحدث أهم — القديم يُقتطع من البداية."""
    history = [{"role": "user", "content": f"س{i}"} for i in range(20)]
    kept = agent._clean_history(history)
    assert kept[-1]["content"] == "س19"


def test_history_truncates_long_messages():
    history = [{"role": "user", "content": "ح" * 9000}]
    assert len(agent._clean_history(history)[0]["content"]) == agent.MAX_HISTORY_CHARS


def test_history_ignores_malformed_entries():
    """البرمجة الدفاعية — لا نفترض اكتمال ما يصل من العميل."""
    history = [
        {"role": "user"},
        {"content": "بلا دور"},
        {"role": "user", "content": "سليم"},
    ]
    assert len(agent._clean_history(history)) == 1
