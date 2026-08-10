"""GuidelineChunk model — curated, versioned clinical-guideline corpus for RAG (P3-01).

Each chunk retains stable citation metadata (source, section_id, page_range) so every
management option can be traced to its origin. Vectors live in Qdrant in production; the
chunk text + a lexical fallback index live here so retrieval and citation work without the
vector store (deterministic, offline-capable degradation).
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import JSONBType
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

_SOURCES = ("icmr", "who", "nice")


class GuidelineChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A semantically coherent, citable segment of a clinical guideline."""

    __tablename__ = "guideline_chunks"
    __table_args__ = (
        CheckConstraint(
            f"source IN ({', '.join(repr(s) for s in _SOURCES)})",
            name="ck_guideline_chunks_source",
        ),
        UniqueConstraint(
            "corpus_version", "source", "section_id", name="uq_guideline_chunks_section"
        ),
    )

    # No single-column index: uq_guideline_chunks_section leads with corpus_version, which is
    # how every retrieval filters. See migration 0009.
    corpus_version: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    document_title: Mapped[str] = mapped_column(Text, nullable=False)
    section_id: Mapped[str] = mapped_column(String(100), nullable=False)
    heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    page_range: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Conditions/keywords this chunk pertains to — drives lexical fallback retrieval.
    keywords: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    # Optional dense embedding (present when ingested with sentence-transformers).
    embedding: Mapped[list | None] = mapped_column(JSONBType, nullable=True)
    token_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
