"""PostgreSQL/SQLite queue with Railway persistence support"""
import json
from enum import Enum
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager
from typing import Optional, List

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .utils import get_project_root, get_env, logger

Base = declarative_base()


class ContentStatus(str, Enum):
    """Valid content statuses"""
    PENDING = "pending"
    APPROVED = "approved"
    POSTED = "posted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ContentItem(Base):
    __tablename__ = "content_items"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Source
    content_hash = Column(String(12), unique=True, index=True)
    content_type = Column(String(20), default="trend")
    source_topic = Column(Text)
    source_url = Column(String(500), nullable=True)
    topic_summary = Column(Text)

    # PRO
    pro_content = Column(Text)
    pro_is_thread = Column(Boolean, default=False)
    pro_thread_parts = Column(Text, nullable=True)
    pro_hashtags = Column(Text, nullable=True)
    pro_image_prompt = Column(Text, nullable=True)

    # WORK
    work_content = Column(Text)
    work_is_thread = Column(Boolean, default=False)
    work_thread_parts = Column(Text, nullable=True)
    work_cashtags = Column(Text, nullable=True)
    work_image_prompt = Column(Text, nullable=True)

    # DEGEN
    degen_content = Column(Text)
    degen_is_thread = Column(Boolean, default=False)
    degen_thread_parts = Column(Text, nullable=True)
    degen_image_prompt = Column(Text, nullable=True)

    engagement_notes = Column(Text, nullable=True)

    # Status
    status = Column(String(20), default=ContentStatus.PENDING.value, index=True)

    # Timestamps (indexed for query performance)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    approved_at = Column(DateTime, nullable=True)


class QueueManager:
    """
    Database queue manager.

    Railway: PostgreSQL (auto-set DATABASE_URL)
    Local: SQLite fallback
    """

    def __init__(self, db_url: Optional[str] = None):
        if db_url is None:
            db_url = get_env("DATABASE_URL", "")

        # Handle empty string (from .env with no value)
        if not db_url or db_url.strip() == "":
            db_path = get_project_root() / "data" / "content.db"
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            db_url = f"sqlite:///{str(db_path)}"
            logger.info(f"Using SQLite: {db_path}")

        # Railway Postgres URL fix (they use postgres:// but SQLAlchemy needs postgresql://)
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
        self.Session = sessionmaker(bind=self.engine)

    @contextmanager
    def get_session(self):
        """Context manager for database sessions"""
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
        """Check database connectivity for health checks"""
        try:
            with self.get_session() as session:
                session.execute(text("SELECT 1"))
                return True
        except Exception as e:
            logger.error(f"Database ping failed: {e}")
            return False

    def add_content(self, content_data: dict) -> ContentItem:
        """Add new content to queue"""
        with self.get_session() as session:
            item = ContentItem(
                content_hash=content_data["content_hash"],
                content_type=content_data.get("content_type", "trend"),
                source_topic=content_data["source_topic"],
                source_url=content_data.get("source_url"),
                topic_summary=content_data.get("topic_summary", ""),

                pro_content=content_data["pro_post"]["content"],
                pro_is_thread=content_data["pro_post"].get("is_thread", False),
                pro_thread_parts=json.dumps(content_data["pro_post"].get("thread_parts", [])),
                pro_hashtags=json.dumps(content_data["pro_post"].get("suggested_hashtags", [])),
                pro_image_prompt=content_data["visual_prompts"].get("pro"),

                work_content=content_data["work_post"]["content"],
                work_is_thread=content_data["work_post"].get("is_thread", False),
                work_thread_parts=json.dumps(content_data["work_post"].get("thread_parts", [])),
                work_cashtags=json.dumps(content_data["work_post"].get("cashtags", [])),
                work_image_prompt=content_data["visual_prompts"].get("work"),

                degen_content=content_data["degen_post"]["content"],
                degen_is_thread=content_data["degen_post"].get("is_thread", False),
                degen_thread_parts=json.dumps(content_data["degen_post"].get("thread_parts", [])),
                degen_image_prompt=content_data["visual_prompts"].get("degen"),

                engagement_notes=content_data.get("engagement_notes")
            )
            session.add(item)
            session.flush()
            session.refresh(item)
            session.expunge(item)
            return item

    def content_exists(self, content_hash: str) -> bool:
        """Check for duplicates"""
        with self.get_session() as session:
            return session.query(ContentItem).filter_by(content_hash=content_hash).first() is not None

    def get_pending(self, limit: int = 20) -> List[ContentItem]:
        """Get pending items ordered by newest first"""
        with self.get_session() as session:
            items = session.query(ContentItem)\
                .filter_by(status=ContentStatus.PENDING.value)\
                .order_by(ContentItem.created_at.desc())\
                .limit(limit).all()
            session.expunge_all()
            return items

    def get_pending_count(self) -> int:
        """Get count of pending items (for notifications)"""
        with self.get_session() as session:
            return session.query(ContentItem)\
                .filter_by(status=ContentStatus.PENDING.value)\
                .count()

    def get_by_id(self, item_id: int) -> Optional[ContentItem]:
        """Get item by ID"""
        with self.get_session() as session:
            item = session.query(ContentItem).filter_by(id=item_id).first()
            if item:
                session.expunge(item)
            return item

    def update_status(self, item_id: int, status: str) -> bool:
        """Update status. Returns True if updated, False if not found."""
        with self.get_session() as session:
            item = session.query(ContentItem).filter_by(id=item_id).first()
            if not item:
                logger.warning(f"Attempted to update non-existent item: {item_id}")
                return False

            item.status = status
            if status == ContentStatus.APPROVED.value:
                item.approved_at = datetime.utcnow()
            item.updated_at = datetime.utcnow()
            return True

    def update_content(self, item_id: int, updates: dict) -> bool:
        """Update content fields. Returns True if updated, False if not found."""
        with self.get_session() as session:
            item = session.query(ContentItem).filter_by(id=item_id).first()
            if not item:
                logger.warning(f"Attempted to update non-existent item: {item_id}")
                return False

            for key, value in updates.items():
                if hasattr(item, key):
                    setattr(item, key, value)
            item.updated_at = datetime.utcnow()
            return True

    def expire_old_pending(self, hours: int = 48) -> int:
        """Mark old pending items as expired. Returns count expired."""
        with self.get_session() as session:
            cutoff = datetime.utcnow() - timedelta(hours=hours)
            count = session.query(ContentItem)\
                .filter(ContentItem.status == ContentStatus.PENDING.value)\
                .filter(ContentItem.created_at < cutoff)\
                .update({
                    "status": ContentStatus.EXPIRED.value,
                    "updated_at": datetime.utcnow()
                })
            logger.info(f"Expired {count} old pending items")
            return count

    def get_stats(self) -> dict:
        """Get queue statistics"""
        with self.get_session() as session:
            return {
                "total": session.query(ContentItem).count(),
                "pending": session.query(ContentItem).filter_by(status=ContentStatus.PENDING.value).count(),
                "approved": session.query(ContentItem).filter_by(status=ContentStatus.APPROVED.value).count(),
                "posted": session.query(ContentItem).filter_by(status=ContentStatus.POSTED.value).count(),
                "expired": session.query(ContentItem).filter_by(status=ContentStatus.EXPIRED.value).count(),
                "rejected": session.query(ContentItem).filter_by(status=ContentStatus.REJECTED.value).count()
            }
