"""Benefit chunks in pgvector: ingestion and per-customer similarity search.

    python -m caregap.rag.store          # make ingest-benefits

Each customer's Summary of Benefits PDF is split into its numbered sections; a long
section is split further with LangChain's recursive splitter, keeping its heading on every
piece. Chunks are embedded and replace that customer's rows in one transaction, so
re-running is idempotent. Search is always filtered to one customer.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from langchain_text_splitters import RecursiveCharacterTextSplitter
from psycopg.rows import dict_row

from caregap.customers import CustomerConfig
from caregap.rag.documents import read_pdf, sections

#: Sections longer than this are split (characters; every current section fits in one).
CHUNK_SIZE = 900


@dataclass(frozen=True)
class Chunk:
    id: str
    customer: str
    section: str
    title: str
    text: str

    @property
    def citation(self) -> str:
        """The ID the model cites: the part after ``customer:``."""
        return self.id.split(":", 1)[1]


def chunk_document(customer: str, text: str) -> list[Chunk]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=0, separators=["\n", ". ", " "]
    )
    chunks = []
    for s in sections(text):
        for n, piece in enumerate(splitter.split_text(s.text), 1):
            chunks.append(
                Chunk(f"{customer}:{s.id}-{n}", customer, s.id, s.title, f"{s.title}\n{piece}")
            )
    return chunks


def _vector(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.7g}" for v in values) + "]"


class ChunkStore:
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    def replace(
        self, customer: str, chunks: list[Chunk], vectors: list[list[float]], *,
        source: Path, model: str,
    ) -> None:  # fmt: skip
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        with psycopg.connect(self.conninfo) as conn, conn.transaction():
            conn.execute("DELETE FROM benefit_chunks WHERE customer = %s", (customer,))
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO benefit_chunks (id, customer, section, title, text, embedding,"
                    " source, source_sha256, embed_model)"
                    " VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s, %s)",
                    [(c.id, c.customer, c.section, c.title, c.text, _vector(v), str(source),
                      sha, model) for c, v in zip(chunks, vectors, strict=True)],
                )  # fmt: skip

    def search(self, customer: str, vector: list[float], k: int) -> list[tuple[Chunk, float]]:
        """Nearest chunks by cosine distance, within one customer's document only."""
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            rows = conn.execute(
                "SELECT id, customer, section, title, text, embedding <=> %s::vector AS distance"
                " FROM benefit_chunks WHERE customer = %s ORDER BY distance, id LIMIT %s",
                (_vector(vector), customer, k),
            ).fetchall()
        return [
            (Chunk(r["id"], r["customer"], r["section"], r["title"], r["text"]),
             float(r["distance"]))
            for r in rows
        ]  # fmt: skip

    def all_text(self, customer: str) -> str:
        with psycopg.connect(self.conninfo) as conn:
            rows = conn.execute(
                "SELECT text FROM benefit_chunks WHERE customer = %s ORDER BY id", (customer,)
            ).fetchall()
        return "\n".join(r[0] for r in rows)


class BenefitRetriever(BaseRetriever):
    """LangChain retriever over one customer's benefit chunks."""

    store: Any
    embeddings: Embeddings
    customer: str
    k: int = 4

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        hits = self.store.search(self.customer, self.embeddings.embed_query(query), self.k)
        return [
            Document(
                page_content=c.text,
                metadata={"id": c.citation, "section": c.section, "title": c.title, "distance": d},
            )
            for c, d in hits
        ]


def ingest(
    customers: Mapping[str, CustomerConfig], store: ChunkStore, embeddings: Embeddings,
    model: str,
) -> dict[str, int]:  # fmt: skip
    counts = {}
    for cfg in customers.values():
        chunks = chunk_document(cfg.id, read_pdf(cfg.benefits_document))
        vectors = embeddings.embed_documents([c.text for c in chunks])
        store.replace(cfg.id, chunks, vectors, source=cfg.benefits_document, model=model)
        counts[cfg.id] = len(chunks)
    return counts


def main() -> int:
    from caregap.config import get_settings
    from caregap.customers import load_all
    from caregap.llm.embeddings import LangChainEmbeddings
    from caregap.llm.factory import build_embeddings

    settings = get_settings()
    client = build_embeddings(settings)
    counts = ingest(
        load_all(settings.customers_dir),
        ChunkStore(settings.database_url),
        LangChainEmbeddings(client, purpose="ingest-benefits"),
        client.model,
    )
    for customer, n in counts.items():
        print(f"[ingest-benefits] {customer}: {n} chunks ({client.model})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
