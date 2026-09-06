import chromadb
from chromadb.utils import embedding_functions

client = chromadb.PersistentClient(path="./vectorstore")
embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
       model_name="intfloat/multilingual-e5-base"
)
col = client.get_collection("company_docs", embedding_function=embed_fn)

print("إجمالي القطع:", col.count())
print()

res = col.query(query_texts=["شروط العمل عن بعد"], n_results=col.count())
for i, (doc, meta, dist) in enumerate(
    zip(res["documents"][0], res["metadatas"][0], res["distances"][0]), start=1
):
    mark = ">>>" if "بعد" in meta.get("location", "") else "   "
    print(f"{mark} {i:2d}. {dist:.3f} | {meta.get('location','')}")