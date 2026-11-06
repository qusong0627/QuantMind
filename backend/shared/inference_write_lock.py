"""Cross-process coordination for one owner's model artifacts."""

import hashlib
import json
from contextlib import contextmanager
from functools import cache

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def lock_id(tenant_id, user_id, resource):
    if not str(tenant_id or "").strip() or not str(user_id or "").strip():
        raise ValueError("Inference write locks require both owner dimensions")
    value = json.dumps([str(tenant_id), str(user_id), str(resource)])
    return int.from_bytes(
        hashlib.sha256(value.encode()).digest()[:8], "big", signed=True
    )


@cache
def sessions(process_id):
    from backend.shared.database import SessionLocal

    dsn = SessionLocal.kw["bind"].url.set(drivername="postgresql+psycopg2")
    return sessionmaker(
        create_engine(dsn, pool_pre_ping=True, pool_size=2, max_overflow=4)
    )


@contextmanager
def model_write_lock(tenant_id, user_id, resource):
    import os

    key = lock_id(tenant_id, user_id, resource)
    with sessions(os.getpid())() as db, db.begin():
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        yield
