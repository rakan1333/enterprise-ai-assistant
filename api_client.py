"""عميل HTTP للتواصل مع خدمة المساعد."""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
TIMEOUT = 120


class APIError(Exception):
    pass


def _handle(r: httpx.Response) -> dict | list | None:
    if r.status_code == 204:
        return None
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise APIError(f"[{r.status_code}] {detail}")
    return r.json()


def health() -> dict:
    return _handle(httpx.get(f"{BASE_URL}/health", timeout=10))


def ask(question: str) -> dict:
    return _handle(
        httpx.post(f"{BASE_URL}/ask", json={"question": question}, timeout=TIMEOUT)
    )


def list_documents() -> list[dict]:
    return _handle(httpx.get(f"{BASE_URL}/documents", timeout=30))


def upload_document(filename: str, content: bytes) -> dict:
    return _handle(
        httpx.post(
            f"{BASE_URL}/documents",
            files={"file": (filename, content)},
            timeout=TIMEOUT,
        )
    )


def delete_document(doc_hash: str) -> None:
    _handle(httpx.delete(f"{BASE_URL}/documents/{doc_hash}", timeout=30))