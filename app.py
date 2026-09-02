"""واجهة المساعد المؤسسي."""

import streamlit as st

import rag_engine as engine

st.set_page_config(page_title="المساعد المؤسسي", page_icon="📚", layout="wide")


@st.cache_resource
def load_collection():
    return engine.get_collection()


collection = load_collection()


def render_sources(sources: list[dict]) -> None:
    with st.expander(f"📎 المصادر ({len(sources)})"):
        for i, s in enumerate(sources, start=1):
            meta = s["meta"]
            st.markdown(
                f"**[مصدر {i}]** `{meta['source']}` — {meta.get('location', '')}  \n"
                f"<sub>درجة القرب: {s['distance']:.3f}</sub>",
                unsafe_allow_html=True,
            )
            st.text(s["text"])
            st.divider()


# ---------- الشريط الجانبي ----------

with st.sidebar:
    st.header("📁 إدارة المستندات")

    uploaded = st.file_uploader(
        "ارفع ملف",
        type=engine.SUPPORTED_TYPES,
        accept_multiple_files=True,
    )

    if uploaded and st.button("معالجة الملفات", type="primary"):
        for f in uploaded:
            with st.spinner(f"جارٍ معالجة {f.name}..."):
                try:
                    result = engine.ingest_file(collection, f.getvalue(), f.name)
                except Exception as e:
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

    docs = engine.list_documents(collection)
    st.subheader(f"المستندات المخزّنة ({len(docs)})")

    if not docs:
        st.caption("لا توجد مستندات بعد.")
    else:
        for doc_hash, info in docs.items():
            col1, col2 = st.columns([4, 1])
            col1.write(f"📄 {info['filename']}")
            col1.caption(f"{info['chunks']} قطعة")
            if col2.button("🗑️", key=doc_hash):
                engine.delete_document(collection, doc_hash)
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
                out = engine.ask_rag(collection, prompt)
                answer, sources = out["answer"], out["sources"]
            except Exception as e:
                answer, sources = f"⚠️ حدث خطأ: {e}", []

        st.markdown(answer)

        if sources:
            render_sources(sources)

    st.session_state.messages.append(
        {"role": "assistant", "content": answer, "sources": sources}
    )