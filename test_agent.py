import rag_engine as engine
from agent import run_agent
from logging_config import setup_logging

setup_logging()
col = engine.get_collection()

questions = [
    "كم رصيد الإجازة المتبقي لخالد الدوسري؟",
    "كم إجمالي بدل الانتقال الشهري لكل الأقسام؟",
    "ما شروط العمل عن بعد؟",
    "ما سياسة التأمين الصحي؟",
]

for q in questions:
    print("=" * 60)
    print("السؤال:", q)
    out = run_agent(col, q)
    print("الأدوات:", [t["tool"] for t in out["trace"]])
    print("الإجابة:", out["answer"])
    print()