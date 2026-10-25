"""Database models (SQLAlchemy 2.x) - single-user version.

PostgreSQL on Render, SQLite for local use. The database holds only the
rounds you are working on (text + photos) so nothing is lost on a page
refresh or when switching devices. Rounds are deleted automatically
according to the retention policy in retention.py; generated Word reports
are never stored.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import List, Optional

from sqlalchemy import (Boolean, Date, DateTime, ForeignKey, Integer, LargeBinary, String, Text,
                        create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


TS = DateTime(timezone=True)


class Round(Base):
    __tablename__ = "rounds"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)   # open | completed

    facility: Mapped[str] = mapped_column(Text, default="")
    department: Mapped[str] = mapped_column(Text, default="")
    round_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    nurses: Mapped[str] = mapped_column(Text, default="")
    patients: Mapped[str] = mapped_column(Text, default="")
    support_team: Mapped[str] = mapped_column(Text, default="")
    hospital_action: Mapped[str] = mapped_column(Text, default="")
    repeat_header: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    last_activity_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(TS, nullable=True)
    purge_after: Mapped[Optional[datetime]] = mapped_column(TS, nullable=True, index=True)

    observations: Mapped[List["Observation"]] = relationship(
        back_populates="round", cascade="all, delete-orphan", passive_deletes=True,
        order_by="(Observation.position, Observation.created_at)")


class Observation(Base):
    __tablename__ = "observations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    round_id: Mapped[str] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    text: Mapped[str] = mapped_column(Text, default="")
    recommendations: Mapped[str] = mapped_column(Text, default="")
    repeated: Mapped[str] = mapped_column(String(3), default="")          # yes | no
    corrective_plan: Mapped[str] = mapped_column(Text, default="")
    documentation: Mapped[str] = mapped_column(Text, default="")
    wide: Mapped[bool] = mapped_column(Boolean, default=False)

    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    round: Mapped[Round] = relationship(back_populates="observations")
    photos: Mapped[List["Photo"]] = relationship(
        back_populates="observation", cascade="all, delete-orphan", passive_deletes=True,
        order_by="(Photo.position, Photo.created_at)")


class Photo(Base):
    __tablename__ = "photos"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    observation_id: Mapped[str] = mapped_column(ForeignKey("observations.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    filename: Mapped[str] = mapped_column(String(255), default="photo")
    ext: Mapped[str] = mapped_column(String(8))                 # jpeg | png
    width_px: Mapped[int] = mapped_column(Integer)
    height_px: Mapped[int] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary)            # cleaned, metadata-free image
    thumb: Mapped[bytes] = mapped_column(LargeBinary)           # small JPEG for the table
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    observation: Mapped[Observation] = relationship(back_populates="photos")


class AuthAttempt(Base):
    """Failed sign-in attempts (rate limiting). Only a keyed hash of the
    client address is stored; records are deleted after 24 hours."""
    __tablename__ = "auth_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_hash: Mapped[str] = mapped_column(String(64), index=True)
    at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)


def make_engine(url: str):
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs.update(pool_size=3, max_overflow=3, pool_recycle=300)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _rec):   # enforce ON DELETE CASCADE in SQLite
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine):
    Base.metadata.create_all(engine)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite returns naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
