"""Pydantic schemas for Community."""

from typing import List, Optional

from pydantic import BaseModel, Field


class PostBase(BaseModel):
    title: str
    content: str
    category: str | None = None
    tags: list[str] | None = Field(default_factory=list)
    media: list[dict] | None = Field(default_factory=list)


class PostCreate(PostBase):
    pass


class PostUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    media: list[dict] | None = None


class CommentBase(BaseModel):
    content: str


class CommentCreateIn(CommentBase):
    parentId: int | None = None
    replyToId: int | None = None


class CommentUpdate(CommentBase):
    pass


class UploadResponse(BaseModel):
    url: str
    thumbnail: str | None = None
    filename: str
    size: int
