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
UI_HEIGHT = 900

st.set_page_config(
    page_title="المساعد المؤسسي",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# إخفاء عناصر Streamlit ليملأ التصميم الشاشة
st.markdown(
    """
    <style>
      #MainMenu, header, footer { visibility: hidden; height: 0; }
      .block-container { padding: 0 !important; max-width: 100% !important; }
      iframe { border: 0; }
      .stApp { background: #07080b; }
      div[data-testid="stAppViewBlockContainer"] { padding: 0 !important; }
    </style>
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

components.html(html, height=UI_HEIGHT, scrolling=False)
