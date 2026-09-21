"""واجهة المساعد المؤسسي — تصميم مخصّص داخل Streamlit.

    streamlit run ui_app.py

تتطلّب تشغيل الخادم أولاً في نافذة منفصلة:

    uvicorn api:app --reload
"""

import os
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

load_dotenv()

API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")
UI_PATH = Path(__file__).parent / "ui.html"

st.set_page_config(
    page_title="المساعد المؤسسي",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# إخفاء عناصر Streamlit ومنع التمرير على مستوى الصفحة.
# الإطار يُمدَّد بالسكربت ليطابق ارتفاع النافذة تماماً — الارتفاع الثابت
# يعني إما فراغاً أسفل الشاشة أو صندوق كتابة خارج حدودها.
st.markdown(
    """
    <style>
      #MainMenu, header, footer { visibility: hidden; height: 0; }
      .block-container { padding: 0 !important; max-width: 100% !important; }
      div[data-testid="stAppViewBlockContainer"] { padding: 0 !important; }
      div[data-testid="stVerticalBlock"] { gap: 0 !important; }
      .stApp { background: #07080b; overflow: hidden; }
      html, body { overflow: hidden !important; }
      iframe { border: 0; display: block; }
    </style>

    <script>
      (function () {
        function fit() {
          const doc = window.parent.document;
          const h = window.parent.innerHeight - 8;
          doc.querySelectorAll('iframe').forEach(function (f) {
            f.style.height = h + 'px';
            f.setAttribute('height', h);
          });
        }
        fit();
        setTimeout(fit, 120);
        setTimeout(fit, 400);
        setTimeout(fit, 900);
        window.parent.addEventListener('resize', fit);
      })();
    </script>
    """,
    unsafe_allow_html=True,
)

if not UI_PATH.exists():
    st.error(f"ملف الواجهة غير موجود: {UI_PATH}")
    st.stop()

html = UI_PATH.read_text(encoding="utf-8")

# حقن عنوان الخادم قبل تشغيل سكربت الواجهة
html = html.replace(
    "<head>",
    f'<head>\n<script>window.API_BASE="{API_BASE}";</script>',
    1,
)

# قيمة أوّلية معقولة — السكربت أعلاه يصحّحها فور التحميل
components.html(html, height=700, scrolling=False)
