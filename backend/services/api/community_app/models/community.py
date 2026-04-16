"""Community Data Models."""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from backend.services.api.models.base import Base


class PostRecord(Base):
    __tablename__ = "community_posts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(64), nullable=False, index=True)
    author_id = Column(String(64), nullable=False, index=True)
    title = Column(String(256), nullable=False)
    content = Column(Text, nullable=False)
    category = Column(String(64), index=True)
    tags = Column(JSON, default=[])
    media = Column(JSON, default=[])
    excerpt = Column(Text)
    views = Column(Integer, default=0)
    likes = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    collections = Column(Integer, default=0)
    pinned = Column(Boolean, default=False)
    featured = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    last_comment_at = Column(DateTime)

    __table_args__ = (
        Index("idx_post_tenant_category", "tenant_id", "category"),
    )


class CommentRecord(Base):
    __tablename__ = "community_comments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(64), nullable=False, index=True)
    post_id = Column(BigInteger, nullable=False, index=True)
    author_id = Column(String(64), nullable=False, index=True)
    content = Column(Text, nullable=False)
    parent_id = Column(BigInteger, index=True)
    reply_to_id = Column(BigInteger)
    likes = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class InteractionRecord(Base):
    __tablename__ = "community_interactions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(64), nullable=False, index=True)
    post_id = Column(BigInteger, index=True)
    comment_id = Column(BigInteger, index=True)
    type = Column(String(32), nullable=False)  # 'like', 'collect'
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "post_id", "comment_id", "type",
            name="uq_community_interactions"
        ),
    )


class AuthorFollowRecord(Base):
    __tablename__ = "community_author_follows"
    # Note: follows.py uses raw SQL, but we define the model here for completeness and future orm usage
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(64), nullable=False, index=True)
    follower_user_id = Column(String(64), nullable=False, index=True)
    author_user_id = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "follower_user_id", "author_user_id",
            name="uq_community_author_follows_model"
        ),
    )
