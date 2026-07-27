from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    genre: Mapped[str] = mapped_column(String(128), default="")
    premise: Mapped[str] = mapped_column(Text, default="")
    story: Mapped[str] = mapped_column(Text, default="")
    story_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    storyboard_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    frames: Mapped[list["StoryboardFrame"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="StoryboardFrame.position"
    )
    jobs: Mapped[list["RenderJob"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class StoryboardFrame(Base):
    __tablename__ = "storyboard_frames"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(Text, default="")
    visual_prompt: Mapped[str] = mapped_column(Text, default="")
    still_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    preview_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    duration_hint_sec: Mapped[float] = mapped_column(Float, default=4.0)
    is_new_shot: Mapped[bool] = mapped_column(Boolean, default=True)

    project: Mapped[Project] = relationship(back_populates="frames")


class RenderJob(Base):
    __tablename__ = "render_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    # pending|running|paused|cancelling|cancelled|failed|completed
    target_length_sec: Mapped[float] = mapped_column(Float, default=30.0)
    format: Mapped[str] = mapped_column(String(32), default="mp4")
    aspect: Mapped[str] = mapped_column(String(16), default="16:9")
    chunk_frames: Mapped[int] = mapped_column(Integer, default=33)
    overlap_frames: Mapped[int] = mapped_column(Integer, default=12)
    width: Mapped[int] = mapped_column(Integer, default=1280)
    height: Mapped[int] = mapped_column(Integer, default=704)
    fps: Mapped[int] = mapped_column(Integer, default=24)
    t2v_workflow: Mapped[str] = mapped_column(String(64), default="wan22_t2v")
    i2v_workflow: Mapped[str] = mapped_column(String(64), default="wan22_i2v")
    prompt_base: Mapped[str] = mapped_column(Text, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    seed: Mapped[int] = mapped_column(Integer, default=42)
    movie_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="jobs")
    shots: Mapped[list["Shot"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="Shot.position"
    )


class Shot(Base):
    __tablename__ = "shots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("render_jobs.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(256), default="")
    prompt_base: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    frame_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    job: Mapped[RenderJob] = relationship(back_populates="shots")
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="shot", cascade="all, delete-orphan", order_by="Chunk.chunk_index"
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shot_id: Mapped[int] = mapped_column(ForeignKey("shots.id", ondelete="CASCADE"))
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    mode: Mapped[str] = mapped_column(String(32), default="new_shot")  # new_shot|continue
    status: Mapped[str] = mapped_column(String(32), default="pending")
    handoff: Mapped[dict] = mapped_column(JSON, default=dict)
    frames_dir: Mapped[str | None] = mapped_column(String(512), nullable=True)
    kept_frames_dir: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_frame_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    comfy_prompt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retries: Mapped[int] = mapped_column(Integer, default=0)

    shot: Mapped[Shot] = relationship(back_populates="chunks")


_engine = None
_SessionLocal = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.media_dir.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
        _engine = create_engine(settings.database_url, connect_args=connect_args)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return _engine


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)


def SessionLocal():
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()
