from datetime import datetime
from sqlalchemy import String, Integer, Text, DateTime, JSON, ForeignKey, UniqueConstraint, BigInteger
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass

class Book(Base):
    __tablename__ = "books"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(512))
    genre: Mapped[str] = mapped_column(String(256))
    language: Mapped[str] = mapped_column(String(32), default="fr")
    spec: Mapped[dict] = mapped_column(JSON)
    outline: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    bible: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    chapters: Mapped[list["Chapter"]] = relationship(back_populates="book", cascade="all, delete-orphan")
    trace_steps: Mapped[list["TraceStep"]] = relationship(back_populates="book", cascade="all, delete-orphan")

class Chapter(Base):
    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("book_id", "chapter_number", name="uq_book_chapter_number"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), ForeignKey("books.id"), index=True)
    chapter_number: Mapped[int] = mapped_column(Integer, index=True)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    draft_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    book: Mapped["Book"] = relationship(back_populates="chapters")

class TraceStep(Base):
    __tablename__ = "trace_steps"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), ForeignKey("books.id"), index=True)

    agent: Mapped[str] = mapped_column(String(128), index=True)
    in_event_type: Mapped[str] = mapped_column(String(256), index=True)
    in_event: Mapped[dict] = mapped_column(JSON)

    summary: Mapped[str] = mapped_column(String(1024), default="")
    out_events: Mapped[dict] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="started")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    book: Mapped["Book"] = relationship(back_populates="trace_steps")
    llm_calls: Mapped[list["LLMCall"]] = relationship(back_populates="trace_step", cascade="all, delete-orphan")

class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_step_id: Mapped[str] = mapped_column(String(64), ForeignKey("trace_steps.id"), index=True)

    agent: Mapped[str] = mapped_column(String(128), index=True)
    model: Mapped[str] = mapped_column(String(128))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(32), default="ok", index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    request: Mapped[dict] = mapped_column(JSON)
    response: Mapped[dict] = mapped_column(JSON)
    output_parsed: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    trace_step: Mapped["TraceStep"] = relationship(back_populates="llm_calls")

