from pathlib import Path

import rag_engine as engine
from logging_config import setup_logging

setup_logging()
col = engine.get_collection()

files = ["سياسة_الموارد_البشرية.docx", "بيانات_الموظفين.xlsx"]

for name in files:
    p = Path(name)
    if not p.exists():
        print("مفقود:", name)
        continue
    result = engine.ingest_file(col, p.read_bytes(), name)
    print(result["status"], "|", name, "|", result["chunks"], "قطعة")

print()
print("إجمالي القطع:", col.count())