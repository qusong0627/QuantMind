"""Pydantic schemas for Community."""

from typing import List, Optional

from pydantic import BaseModel, Field


class PostBase(BaseModel):
    title: str
    content: str
    category: Optional[str] = None
    tags: Optional[List[str]] = Field(default_factory=list)
    media: Optional[List[dict]] = Field(default_factory=list)


class PostCreate(PostBase):
    pass


class PostUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    media: Optional[List[dict]] = None


class CommentBase(BaseModel):
    content: str


class CommentCreateIn(CommentBase):
    parentId: Optional[int] = None
    replyToId: Optional[int] = None


class CommentUpdate(CommentBase):
    pass


class UploadResponse(BaseModel):
    url: str
    thumbnail: Optional[str] = None
    filename: str
    size: int
