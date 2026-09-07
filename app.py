"""واجهة المساعد المؤسسي — تتصل بخدمة REST."""

import streamlit as st

import api_client as api
from logging_config import setup_logging

setup_logging()

st.set_page_config(page_title="المساعد المؤسسي", page_icon="📚", layout="wide")

st.markdown("""
<style>
    .stApp { direction: rtl; }

    section[data-testid="stSidebar"] {
        direction: rtl;
        text-align: right;
    }

    .stMarkdown, .stCaption, p, li, label, h1, h2, h3, h4 {
        text-align: right;
    }

    .stChatMessage { direction: rtl; text-align: right; }

    .stTextInput input,
    .stTextArea textarea,
    [data-testid="stChatInput"] textarea {
        direction: rtl;
        text-align: right;
    }

    section[data-testid="stSidebar"] .stButton button { width: 100%; }
    [data-testid="column"] .stButton button { width: auto; min-width: 44px; }

    code, pre, .stJson { direction: ltr; text-align: left; }

    [data-testid="stExpander"] summary { flex-direction: row-reverse; }
</style>
""", unsafe_allow_html=True)


def render_sources(sources: list[dict]) -> None:
    with st.expander(f"📎 المصادر ({len(sources)})"):
        for i, s in enumerate(sources, start=1):
            st.markdown(f"**[مصدر {i}]** `{s['source']}` — {s.get('location', '')}")
            st.text(s["text"])
            st.divider()


def render_steps(steps: list[dict]) -> None:
    with st.expander(f"🔧 الأدوات المستخدمة ({len(steps)})"):
        for i, s in enumerate(steps, start=1):
            st.markdown(f"**{i}.** `{s['tool']}`")
            if s.get("args"):
                st.json(s["args"])


# ---------- فحص الاتصال ----------

try:
    status = api.health()
except Exception:
    st.error(
        "⚠️ تعذّر الاتصال بالخدمة.\n\n"
        "شغّل الخادم أولاً في نافذة منفصلة:\n\n"
        "`uvicorn api:app --reload`"
    )
    st.stop()


# ---------- الشريط الجانبي ----------

with st.sidebar:
    st.header("⚙️ الإعدادات")
    st.caption(f"متصل بالخدمة — {status['chunks']} قطعة")

    mode = st.radio(
        "وضع الإجابة",
        options=["الوكيل", "بحث المستندات"],
        help="الوكيل يختار بين المستندات وقاعدة البيانات. البحث يقتصر على المستندات.",
    )

    st.divider()
    st.header("📁 إدارة المستندات")

    uploaded = st.file_uploader(
        "ارفع ملف",
        type=["pdf", "docx", "xlsx"],
        accept_multiple_files=True,
    )

    if uploaded and st.button("معالجة الملفات", type="primary"):
        for f in uploaded:
            with st.spinner(f"جارٍ معالجة {f.name}..."):
                try:
                    result = api.upload_document(f.name, f.getvalue())
                except api.APIError as e:
                    st.error(f"❌ {f.name}: {e}")
                    continue

            if result["status"] == "added":
                st.success(f"✅ {f.name} — {result['chunks']} قطعة")
            elif result["status"] == "duplicate":
                st.info(f"ℹ️ {f.name} مرفوع مسبقاً")
            else:
                st.warning(f"⚠️ {f.name} لا يحتوي نصاً قابلاً للقراءة")

        st.rerun()

    st.divider()

    docs = api.list_documents()
    st.subheader(f"المستندات المخزّنة ({len(docs)})")

    if not docs:
        st.caption("لا توجد مستندات بعد.")
    else:
        for d in docs:
            col1, col2 = st.columns([5, 1])
            with col1:
                st.markdown(f"📄 **{d['filename']}**")
                st.caption(f"{d['chunks']} قطعة")
            with col2:
                if st.button("🗑️", key=d["doc_hash"], help="حذف"):
                    api.delete_document(d["doc_hash"])
                    st.rerun()

    st.divider()

    if st.button("🧹 محادثة جديدة"):
        st.session_state.messages = []
        st.rerun()


# ---------- المحادثة ----------

st.title("📚 المساعد المؤسسي الذكي")
st.caption("يجيب من مستنداتك وبياناتك، مع إظهار المصدر والأدوات المستخدمة.")

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("steps"):
            render_steps(msg["steps"])
        if msg.get("sources"):
            render_sources(msg["sources"])

if prompt := st.chat_input("اكتب سؤالك..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("جارٍ العمل..."):
            try:
                if mode == "الوكيل":
                    out = api.ask_agent(prompt)
                    steps = out.get("steps", [])
                else:
                    out = api.ask(prompt)
                    steps = []
                answer = out["answer"]
                sources = out.get("sources", [])
            except api.APIError as e:
                answer, sources, steps = f"⚠️ {e}", [], []
            except Exception as e:
                answer, sources, steps = f"⚠️ تعذّر الاتصال: {e}", [], []

        st.markdown(answer)

        if steps:
            render_steps(steps)
        if sources:
            render_sources(sources)

    st.session_state.messages.append(
        {"role": "assistant", "content": answer, "sources": sources, "steps": steps}
    )