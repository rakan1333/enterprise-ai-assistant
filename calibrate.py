import chromadb
from chromadb.utils import embedding_functions

client = chromadb.PersistentClient(path="./vectorstore")
embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
    model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
col = client.get_collection("company_docs", embedding_function=embed_fn)

tests = [
    ("ما شروط العمل عن بعد؟", True),
    ("كم يوماً إجازة أمومة؟", True),
    ("ما هي ساعات العمل؟", True),
    ("كم بدل الانتقال الشهري؟", True),
    ("كم يوم تدريب سنوياً؟", True),
    ("ما سياسة التأمين الصحي؟", False),
    ("كم راتب المدير التنفيذي؟", False),
    ("ما هي سياسة السفر للخارج؟", False),
]

ok, bad = [], []
for q, has_answer in tests:
    d = col.query(query_texts=[q], n_results=1)["distances"][0][0]
    (ok if has_answer else bad).append(d)
    print(("صح " if has_answer else "خطأ"), round(d, 3), "|", q)

print()
print("اسوا سؤال له اجابة :", round(max(ok), 3))
print("افضل سؤال بلا اجابة:", round(min(bad), 3))
print("الفجوة              :", round(min(bad) - max(ok), 3))
print("العتبة المقترحة     :", round((max(ok) + min(bad)) / 2, 3))