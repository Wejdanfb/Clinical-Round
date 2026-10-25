"""Retention policy and cleanup.

  * A COMPLETED round - with all its observations and photos - is
    permanently deleted COMPLETED_ROUND_RETENTION_DAYS (default 7) after
    you mark it completed. You can also delete any round immediately.
  * A round still IN PROGRESS with no changes for INACTIVE_ROUND_DAYS
    (default 30) is permanently deleted.
  * Failed sign-in records (rate limiting) are deleted after 24 hours.
  * Generated Word reports are never stored: they are built in memory
    and sent straight to your browser.

Cleanup runs inside the web app at start-up and at most every
CLEANUP_INTERVAL_MINUTES (default 10) when requests arrive, and on demand
with `python manage.py purge-expired`. Photos are removed in the same
transaction as their round (ON DELETE CASCADE).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select

from models import AuthAttempt, Observation, Photo, Round, utcnow

_lock = threading.Lock()
_last_run = 0.0


def purge_expired(db, settings, now: datetime | None = None) -> dict:
    now = now or utcnow()
    inactive_cutoff = now - timedelta(days=settings.inactive_round_days)
    expired = db.execute(select(Round.id).where(or_(
        and_(Round.status == "completed", Round.purge_after.is_not(None), Round.purge_after <= now),
        and_(Round.status == "open", Round.last_activity_at <= inactive_cutoff),
    ))).scalars().all()

    photos = 0
    if expired:
        photos = db.execute(select(func.count(Photo.id)).join(Observation)
                            .where(Observation.round_id.in_(expired))).scalar_one()
        db.execute(delete(Round).where(Round.id.in_(expired)))
    attempts = db.execute(delete(AuthAttempt).where(AuthAttempt.at <= now - timedelta(hours=24))).rowcount
    db.commit()
    return {"rounds_deleted": len(expired), "photos_deleted": int(photos or 0),
            "signin_records_deleted": int(attempts or 0)}


def maybe_purge(session_factory, settings, logger=None):
    """Run purge_expired at most once per interval in this process."""
    global _last_run
    interval = max(1, settings.cleanup_interval_minutes) * 60
    if time.monotonic() - _last_run < interval or not _lock.acquire(blocking=False):
        return None
    try:
        _last_run = time.monotonic()
        db = session_factory()
        try:
            result = purge_expired(db, settings)
            if logger and (result["rounds_deleted"] or result["signin_records_deleted"]):
                logger.info("Retention cleanup: %s", result)
            return result
        except Exception:     # cleanup must never break a request
            db.rollback()
            if logger:
                logger.exception("Retention cleanup failed")
            return None
        finally:
            db.close()
    finally:
        _lock.release()


def reset_timer():
    global _last_run
    _last_run = 0.0
