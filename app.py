"""واجهة المساعد المؤسسي — تتصل بخدمة REST."""

import streamlit as st

import api_client as api
from logging_config import setup_logging

setup_logging()

st.set_page_config(page_title="المساعد المؤسسي", page_icon="📚", layout="wide")


def render_sources(sources: list[dict]) -> None:
    with st.expander(f"📎 المصادر ({len(sources)})"):
        for i, s in enumerate(sources, start=1):
            st.markdown(
                f"**[مصدر {i}]** `{s['filename']}` — {s['location']}  \n"
                f"<sub>درجة القرب: {s['distance']:.3f}</sub>",
                unsafe_allow_html=True,
            )
            st.text(s["text"])
            st.divider()


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
    st.header("📁 إدارة المستندات")
    st.caption(f"متصل بالخدمة — {status['chunks']} قطعة")

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
            col1, col2 = st.columns([4, 1])
            col1.write(f"📄 {d['filename']}")
            col1.caption(f"{d['chunks']} قطعة")
            if col2.button("🗑️", key=d["doc_hash"]):
                api.delete_document(d["doc_hash"])
                st.rerun()

    st.divider()

    if st.button("🧹 محادثة جديدة"):
        st.session_state.messages = []
        st.rerun()


# ---------- المحادثة ----------

st.title("📚 المساعد المؤسسي الذكي")
st.caption("يجيب من مستنداتك فقط، مع ذكر المصادر.")

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("sources"):
            render_sources(msg["sources"])

if prompt := st.chat_input("اكتب سؤالك عن المستندات..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("جارٍ البحث في المستندات..."):
            try:
                out = api.ask(prompt)
                answer, sources = out["answer"], out["sources"]
            except api.APIError as e:
                answer, sources = f"⚠️ {e}", []
            except Exception as e:
                answer, sources = f"⚠️ تعذّر الاتصال بالخدمة: {e}", []

        st.markdown(answer)

        if sources:
            render_sources(sources)

    st.session_state.messages.append(
        {"role": "assistant", "content": answer, "sources": sources}
    )