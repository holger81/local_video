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
    # wan | ltx — default video motion backend for this project
    video_backend: Mapped[str] = mapped_column(String(16), default="wan")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    frames: Mapped[list["StoryboardFrame"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="StoryboardFrame.position"
    )
    characters: Mapped[list["Character"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Character.position"
    )
    jobs: Mapped[list["RenderJob"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Character(Base):
    """Ground-truth cast entry used when planning/rendering scenes and images."""

    __tablename__ = "characters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(128), default="")
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    appearance_prompt: Mapped[str] = mapped_column(Text, default="")
    reference_image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    intro_frame_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_detected: Mapped[bool] = mapped_column(Boolean, default=False)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="characters")


class StoryboardFrame(Base):
    __tablename__ = "storyboard_frames"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(Text, default="")
    visual_prompt: Mapped[str] = mapped_column(Text, default="")
    still_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Per-step video keyframes: first → mid → last (before movie / between-stills clips)
    keyframe_first_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    keyframe_mid_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    keyframe_last_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Editable prompts used to render each keyframe (seeded from the beat)
    keyframe_first_prompt: Mapped[str] = mapped_column(Text, default="")
    keyframe_mid_prompt: Mapped[str] = mapped_column(Text, default="")
    keyframe_last_prompt: Mapped[str] = mapped_column(Text, default="")
    # Variable series: [{index, t_sec, role, image_prompt, path}] — first/last + ≤2s middles
    keyframes: Mapped[list] = mapped_column(JSON, default=list)
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
    # wan | ltx — job default; shots may override
    video_backend: Mapped[str] = mapped_column(String(16), default="wan")
    t2v_workflow: Mapped[str] = mapped_column(String(64), default="wan22_t2v")
    i2v_workflow: Mapped[str] = mapped_column(String(64), default="wan22_i2v")
    flf2v_workflow: Mapped[str] = mapped_column(String(64), default="wan22_flf2v")
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
    # None = inherit job.video_backend; otherwise wan|ltx
    video_backend: Mapped[str | None] = mapped_column(String(16), nullable=True)

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
    _migrate_sqlite(engine)


def _migrate_sqlite(engine) -> None:
    """Add columns/tables introduced after initial create_all (SQLite has no ALTER via ORM)."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(storyboard_frames)").fetchall()
        }
        additions = {
            "keyframe_first_path": "ALTER TABLE storyboard_frames ADD COLUMN keyframe_first_path VARCHAR(512)",
            "keyframe_mid_path": "ALTER TABLE storyboard_frames ADD COLUMN keyframe_mid_path VARCHAR(512)",
            "keyframe_last_path": "ALTER TABLE storyboard_frames ADD COLUMN keyframe_last_path VARCHAR(512)",
            "keyframe_first_prompt": "ALTER TABLE storyboard_frames ADD COLUMN keyframe_first_prompt TEXT DEFAULT ''",
            "keyframe_mid_prompt": "ALTER TABLE storyboard_frames ADD COLUMN keyframe_mid_prompt TEXT DEFAULT ''",
            "keyframe_last_prompt": "ALTER TABLE storyboard_frames ADD COLUMN keyframe_last_prompt TEXT DEFAULT ''",
            "keyframes": "ALTER TABLE storyboard_frames ADD COLUMN keyframes JSON",
        }
        for name, ddl in additions.items():
            if name not in cols:
                conn.exec_driver_sql(ddl)

        project_cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(projects)").fetchall()
        }
        if "video_backend" not in project_cols:
            conn.exec_driver_sql(
                "ALTER TABLE projects ADD COLUMN video_backend VARCHAR(16) DEFAULT 'wan'"
            )

        job_cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(render_jobs)").fetchall()
        }
        if "video_backend" not in job_cols:
            conn.exec_driver_sql(
                "ALTER TABLE render_jobs ADD COLUMN video_backend VARCHAR(16) DEFAULT 'wan'"
            )
        if "flf2v_workflow" not in job_cols:
            conn.exec_driver_sql(
                "ALTER TABLE render_jobs ADD COLUMN flf2v_workflow VARCHAR(64) DEFAULT 'wan22_flf2v'"
            )

        shot_cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(shots)").fetchall()
        }
        if "video_backend" not in shot_cols:
            conn.exec_driver_sql(
                "ALTER TABLE shots ADD COLUMN video_backend VARCHAR(16)"
            )

        # create_all usually creates characters; keep an explicit safeguard for older DBs.
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER NOT NULL PRIMARY KEY,
                project_id INTEGER NOT NULL,
                position INTEGER DEFAULT 0,
                name VARCHAR(128) DEFAULT '',
                aliases JSON,
                description TEXT DEFAULT '',
                appearance_prompt TEXT DEFAULT '',
                reference_image_path VARCHAR(512),
                intro_frame_id INTEGER,
                auto_detected BOOLEAN DEFAULT 0,
                approved BOOLEAN DEFAULT 0,
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE
            )
            """
        )


def SessionLocal():
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()
