"""يستضيف واجهة Lumen داخل Streamlit ويوجّهها لخادمك.

    streamlit run ui_lumen.py

يتطلّب تشغيل الخادم أولاً في نافذة منفصلة:

    uvicorn api:app --reload
"""

import os
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

load_dotenv()

API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")
HTML_PATH = Path(__file__).parent / "lumen.html"

st.set_page_config(
    page_title="المساعد المؤسسي",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# إخفاء عناصر Streamlit ليملأ التصميم الشاشة بالكامل
st.markdown(
    """
    <style>
      #MainMenu, header, footer { visibility: hidden; }
      .block-container { padding: 0 !important; max-width: 100% !important; }
      iframe { border: 0; }
      body { background: #07080b; }
    </style>
    """,
    unsafe_allow_html=True,
)

if not HTML_PATH.exists():
    st.error(f"الملف غير موجود: {HTML_PATH}")
    st.stop()

html = HTML_PATH.read_text(encoding="utf-8")

# حقن عنوان الخادم وتنسيق العربية قبل تحميل الصفحة
INJECT = f"""<head>
<script>window.LUMEN_API_BASE="{API_BASE}";</script>
<style>
  body {{ direction: rtl; }}
  h1, p, textarea, input {{ text-align: right; }}
  textarea, input {{ direction: rtl; }}
  pre, code {{ direction: ltr; text-align: left; }}
</style>
"""

html = html.replace("<head>", INJECT, 1)

components.html(html, height=980, scrolling=False)
