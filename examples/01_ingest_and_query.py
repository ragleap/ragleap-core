"""
Example: Ingest a document and ask a question about it.

Prerequisites:
  - RagLeap Core running locally (docker compose up --build -d)
  - A .txt, .pdf, or .docx file to test with
  - Your API key in the RAGLEAP_API_KEY environment variable (the installer put it in your .env file).
    Set RAGLEAP_URL too if the server is not at http://localhost:8000

Usage:
    python examples/01_ingest_and_query.py path/to/document.txt "Your question here"
"""
import os
import sys

import requests

API_URL = os.environ.get("RAGLEAP_URL", "http://localhost:8000").rstrip("/")
HEADERS = {"x-api-key": os.environ["RAGLEAP_API_KEY"]} if os.environ.get("RAGLEAP_API_KEY") else {}


def _check(response) -> None:
    """Stop with a clear message when the server wants a key we did not send."""
    if response.status_code == 401:
        sys.exit("The server needs your API key. Set RAGLEAP_API_KEY (it is in your .env file) and run again.")
    response.raise_for_status()


def ingest_document(filepath: str) -> dict:
    """Upload a document to RagLeap Core for ingestion."""
    with open(filepath, "rb") as f:
        response = requests.post(f"{API_URL}/upload", files={"file": f}, headers=HEADERS)
    _check(response)
    return response.json()


def ask_question(question: str) -> dict:
    """Ask a question and get a cited answer from ingested documents."""
    response = requests.post(f"{API_URL}/chat", params={"question": question}, headers=HEADERS)
    _check(response)
    return response.json()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python examples/01_ingest_and_query.py <filepath> <question>")
        sys.exit(1)

    filepath, question = sys.argv[1], sys.argv[2]

    print(f"Ingesting {filepath}...")
    result = ingest_document(filepath)
    print(f"✅ Ingested: {result['chunks_stored']} chunks stored (document_id={result['document_id']})\n")

    print(f"Asking: {question}")
    answer = ask_question(question)
    print(f"\n Answer: {answer['answer']}")
    print(f" Sources: {answer['sources']}")
