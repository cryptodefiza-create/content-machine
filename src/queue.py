"""PostgreSQL/SQLite queue with Railway persistence"""
import json
from enum import Enum
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Optional, List

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, Float, text, func
from sqlalchemy.orm import declarative_base, sessionmaker

from .utils import get_project_root, get_env, logger

Base = declarative_base()


class ContentStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    POSTED = "posted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ContentItem(Base):
    __tablename__ = "content_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    content_hash = Column(String(12), unique=True, index=True)
    content_type = Column(String(20), default="trend")
    source_topic = Column(Text)
    source_url = Column(String(500), nullable=True)
    topic_summary = Column(Text)
    run_id = Column(String(32), nullable=True, index=True)
    pipeline_version = Column(String(10), nullable=True)
    quality_score = Column(Float, nullable=True)

    pro_content = Column(Text)
    pro_is_thread = Column(Boolean, default=False)
    pro_thread_parts = Column(Text, nullable=True)
    pro_hashtags = Column(Text, nullable=True)
    pro_image_prompt = Column(Text, nullable=True)

    work_content = Column(Text)
    work_is_thread = Column(Boolean, default=False)
    work_thread_parts = Column(Text, nullable=True)
    work_cashtags = Column(Text, nullable=True)
    work_image_prompt = Column(Text, nullable=True)

    degen_content = Column(Text)
    degen_is_thread = Column(Boolean, default=False)
    degen_thread_parts = Column(Text, nullable=True)
    degen_image_prompt = Column(Text, nullable=True)

    engagement_notes = Column(Text, nullable=True)
    status = Column(String(20), default=ContentStatus.PENDING.value, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    approved_at = Column(DateTime, nullable=True)


class QueueManager:

    def __init__(self, db_url: Optional[str] = None):
        if db_url is None:
            db_url = get_env("DATABASE_URL", "")

        if not db_url or db_url.strip() == "":
            db_path = get_project_root() / "data" / "content.db"
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            db_url = f"sqlite:///{str(db_path)}"
            logger.info(f"Using SQLite: {db_path}")

        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)

        if db_url.startswith("postgresql://"):
            self.engine = create_engine(
                db_url,
                pool_size=5,
                max_overflow=10,
                pool_pre_ping=True
            )
            logger.info("Connected to PostgreSQL")
        else:
            self.engine = create_engine(db_url)

        Base.metadata.create_all(self.engine)
        self._ensure_columns()
        self.Session = sessionmaker(bind=self.engine)

    def _ensure_columns(self):
        """Lightweight migration for new columns."""
        try:
            with self.engine.connect() as conn:
                if self.engine.url.get_backend_name().startswith("sqlite"):
                    rows = conn.execute(text("PRAGMA table_info(content_items)")).fetchall()
                    columns = {row[1] for row in rows}
                else:
                    rows = conn.execute(text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'content_items'"
                    )).fetchall()
                    columns = {row[0] for row in rows}

                migrations = []
                if "run_id" not in columns:
                    migrations.append("ALTER TABLE content_items ADD COLUMN run_id VARCHAR(32)")
                if "pipeline_version" not in columns:
                    migrations.append("ALTER TABLE content_items ADD COLUMN pipeline_version VARCHAR(10)")
                if "quality_score" not in columns:
                    migrations.append("ALTER TABLE content_items ADD COLUMN quality_score FLOAT")

                for stmt in migrations:
                    conn.execute(text(stmt))
        except Exception as e:
            logger.warning(f"Column migration skipped: {e}")

    @contextmanager
    def get_session(self):
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ping(self) -> bool:
        try:
            with self.get_session() as session:
                session.execute(text("SELECT 1"))
                return True
        except Exception as e:
            logger.error(f"Database ping failed: {e}")
            return False

    def add_content(self, content_data: dict) -> ContentItem:
        pro = content_data.get("pro_post") or {}
        work = content_data.get("work_post") or {}
        degen = content_data.get("degen_post") or {}
        visual_prompts = content_data.get("visual_prompts") or {}

        with self.get_session() as session:
            item = ContentItem(
                content_hash=content_data["content_hash"],
                content_type=content_data.get("content_type", "trend"),
                source_topic=content_data["source_topic"],
                source_url=content_data.get("source_url"),
                topic_summary=content_data.get("topic_summary", ""),
                run_id=content_data.get("run_id"),
                pipeline_version=content_data.get("pipeline_version"),
                quality_score=content_data.get("quality_score"),

                pro_content=pro.get("content", ""),
                pro_is_thread=pro.get("is_thread", False),
                pro_thread_parts=json.dumps(pro.get("thread_parts", [])),
                pro_hashtags=json.dumps(pro.get("suggested_hashtags", [])),
                pro_image_prompt=visual_prompts.get("pro"),

                work_content=work.get("content", ""),
                work_is_thread=work.get("is_thread", False),
                work_thread_parts=json.dumps(work.get("thread_parts", [])),
                work_cashtags=json.dumps(work.get("cashtags", [])),
                work_image_prompt=visual_prompts.get("work"),

                degen_content=degen.get("content", ""),
                degen_is_thread=degen.get("is_thread", False),
                degen_thread_parts=json.dumps(degen.get("thread_parts", [])),
                degen_image_prompt=visual_prompts.get("degen"),

                engagement_notes=content_data.get("engagement_notes")
            )
            session.add(item)
            session.flush()
            session.refresh(item)
            session.expunge(item)
            return item

    def content_exists(self, content_hash: str, dedup_hours: int = 48) -> bool:
        with self.get_session() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=dedup_hours)
            return session.query(ContentItem)\
                .filter(ContentItem.content_hash == content_hash)\
                .filter(ContentItem.created_at >= cutoff)\
                .first() is not None

    def get_pending(self, limit: int = 20) -> List[ContentItem]:
        with self.get_session() as session:
            items = session.query(ContentItem)\
                .filter_by(status=ContentStatus.PENDING.value)\
                .order_by(ContentItem.created_at.desc())\
                .limit(limit).all()
            session.expunge_all()
            return items

    def get_by_id(self, item_id: int) -> Optional[ContentItem]:
        with self.get_session() as session:
            item = session.query(ContentItem).filter_by(id=item_id).first()
            if item:
                session.expunge(item)
            return item

    def get_by_run_id(self, run_id: str, limit: int = 20) -> List[ContentItem]:
        with self.get_session() as session:
            items = session.query(ContentItem)\
                .filter_by(run_id=run_id)\
                .order_by(ContentItem.created_at.desc())\
                .limit(limit).all()
            session.expunge_all()
            return items

    def update_status(self, item_id: int, status: str) -> bool:
        with self.get_session() as session:
            item = session.query(ContentItem).filter_by(id=item_id).first()
            if not item:
                return False

            item.status = status
            if status == ContentStatus.APPROVED.value:
                item.approved_at = datetime.now(timezone.utc)
            item.updated_at = datetime.now(timezone.utc)
            return True

    EDITABLE_FIELDS = frozenset({
        "pro_content", "work_content", "degen_content",
        "pro_image_prompt", "work_image_prompt", "degen_image_prompt",
        "engagement_notes",
    })

    def update_content(self, item_id: int, updates: dict) -> bool:
        with self.get_session() as session:
            item = session.query(ContentItem).filter_by(id=item_id).first()
            if not item:
                return False

            for key, value in updates.items():
                if key in self.EDITABLE_FIELDS:
                    setattr(item, key, value)
                else:
                    logger.warning(f"Rejected update to non-editable field: {key}")
            item.updated_at = datetime.now(timezone.utc)
            return True

    def expire_old_pending(self, hours: int = 48) -> int:
        with self.get_session() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            count = session.query(ContentItem)\
                .filter(ContentItem.status == ContentStatus.PENDING.value)\
                .filter(ContentItem.created_at < cutoff)\
                .update({
                    "status": ContentStatus.EXPIRED.value,
                    "updated_at": datetime.now(timezone.utc)
                })
            logger.info(f"Expired {count} old pending items")
            return count

    def get_stats(self) -> dict:
        with self.get_session() as session:
            rows = session.query(
                ContentItem.status, func.count(ContentItem.id)
            ).group_by(ContentItem.status).all()
            counts = {status: count for status, count in rows}
            total = sum(counts.values())
            return {
                "total": total,
                "pending": counts.get(ContentStatus.PENDING.value, 0),
                "approved": counts.get(ContentStatus.APPROVED.value, 0),
                "posted": counts.get(ContentStatus.POSTED.value, 0),
                "expired": counts.get(ContentStatus.EXPIRED.value, 0),
                "rejected": counts.get(ContentStatus.REJECTED.value, 0),
            }
