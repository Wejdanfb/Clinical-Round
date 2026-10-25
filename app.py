"""
Clinical Round - single-user clinical round report generator.

  * One user (you). When hosted, the app is protected by one password
    (APP_PASSWORD environment variable). Run locally without APP_PASSWORD,
    it opens directly.
  * Create a round, manage its observations in one table (text, photos,
    recommendations, repeated?, corrective plan, documentation), review,
    edit and generate the Word report from the official blank template.
  * Round data and photos are kept in the database (PostgreSQL on Render)
    only while you work on them - see retention.py. Word reports are built
    in memory and never stored. Nothing is written to the server's disk.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import logging
import os
import secrets
from datetime import date, timedelta

from flask import Flask, Request, abort, g, jsonify, render_template, request, send_file, session
from PIL import Image
from sqlalchemy import func, select, text
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

import retention
from config import ConfigError, Settings, load_settings
from models import (AuthAttempt, Observation, Photo, Round, as_utc, init_db, make_engine,
                    make_session_factory, utcnow)
from report_builder import (Observation as RObservation, Photo as RPhoto, ReportData, ReportInputError,
                            TemplateError, build_report, clean_text, load_template_map, process_image,
                            safe_filename)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, "assets", "clinical_round_template.docx")
LOGIN_LIMIT = 10                       # failed sign-ins per client per 15 minutes
ATTEMPT_WINDOW = timedelta(minutes=15)

log = logging.getLogger("clinical_round")


class InMemoryRequest(Request):
    """Uploaded photos stay in RAM; never spooled to a temp file on disk."""

    def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
        return io.BytesIO()


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or load_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    app = Flask(__name__)
    app.request_class = InMemoryRequest
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(
        SECRET_KEY=settings.secret_key,
        MAX_CONTENT_LENGTH=(settings.max_photo_mb + 1) * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=2 * 1024 * 1024,
        SESSION_COOKIE_NAME="cr_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.secure_cookies,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=settings.session_hours),
        JSON_AS_ASCII=False,
    )
    app.settings = settings
    auth_required = bool(settings.app_password)

    engine = make_engine(settings.database_url)
    init_db(engine)
    Session = make_session_factory(engine)
    app.engine, app.Session = engine, Session

    with open(TEMPLATE_PATH, "rb") as fh:
        app.template_bytes = fh.read()
    load_template_map(app.template_bytes)          # fail fast if the template is unusable

    retention.reset_timer()
    retention.maybe_purge(Session, settings, log)

    # ------------------------------------------------------------ helpers
    def db():
        if "db" not in g:
            g.db = Session()
        return g.db

    @app.teardown_appcontext
    def _close_db(_exc):
        s = g.pop("db", None)
        if s is not None:
            s.close()

    def client_hash() -> str:
        ip = request.remote_addr or "unknown"
        return hmac.new(settings.secret_key.encode(), ip.encode(), hashlib.sha256).hexdigest()

    def login_sig() -> str:
        # changes when APP_PASSWORD or SECRET_KEY changes -> old sessions end
        return hmac.new(settings.secret_key.encode(), ("user:" + settings.app_password).encode(),
                        hashlib.sha256).hexdigest()[:32]

    def signed_in() -> bool:
        if not auth_required:
            return True
        return hmac.compare_digest(session.get("sig", ""), login_sig())

    def body() -> dict:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ApiError("Invalid request.")
        return data

    def txt(data, key, label, single_line=False, max_len=None) -> str:
        v = clean_text(data.get(key, ""))
        lim = max_len or settings.max_text_chars
        if len(v) > lim:
            raise ApiError(f"“{label}” is too long (max {lim:,} characters).")
        return " ".join(v.split()) if single_line else v

    def iso(dt):
        dt = as_utc(dt)
        return dt.isoformat() if dt else None

    def delete_due(r: Round):
        if r.status == "completed":
            return iso(r.purge_after)
        return iso(as_utc(r.last_activity_at) + timedelta(days=settings.inactive_round_days))

    def touch(r: Round):
        r.last_activity_at = utcnow()

    def get_round(rid: str) -> Round:
        r = db().get(Round, rid)
        if r is None:
            raise ApiError("This round no longer exists (it may have been deleted).", 404)
        return r

    def require_open(r: Round):
        if r.status != "open":
            raise ApiError("This round is marked completed. Reopen it to make changes.", 409)

    def get_obs(oid: str) -> Observation:
        o = db().get(Observation, oid)
        if o is None:
            raise ApiError("This observation no longer exists.", 404)
        require_open(o.round)
        return o

    def round_summary(r: Round) -> dict:
        n_obs = db().execute(select(func.count(Observation.id)).where(Observation.round_id == r.id)).scalar_one()
        n_ph = db().execute(select(func.count(Photo.id)).join(Observation)
                            .where(Observation.round_id == r.id)).scalar_one()
        return {"id": r.id, "status": r.status, "facility": r.facility, "department": r.department,
                "round_date": r.round_date.isoformat() if r.round_date else "",
                "observations": n_obs, "photos": n_ph, "created_at": iso(r.created_at),
                "last_activity_at": iso(r.last_activity_at), "completed_at": iso(r.completed_at),
                "delete_due": delete_due(r)}

    def round_state(r: Round) -> dict:
        return {
            "round": {
                "id": r.id, "status": r.status, "facility": r.facility, "department": r.department,
                "round_date": r.round_date.isoformat() if r.round_date else "", "nurses": r.nurses,
                "patients": r.patients, "supportTeam": r.support_team, "hospitalAction": r.hospital_action,
                "repeatHeader": r.repeat_header, "created_at": iso(r.created_at),
                "completed_at": iso(r.completed_at), "delete_due": delete_due(r),
                "filename": safe_filename(r.department, r.round_date),
            },
            "observations": [{
                "id": o.id, "no": i, "text": o.text, "recommendations": o.recommendations,
                "repeated": o.repeated, "correctivePlan": o.corrective_plan,
                "documentation": o.documentation, "wide": o.wide, "version": o.version,
                "photos": [{"id": p.id, "filename": p.filename, "w": p.width_px, "h": p.height_px}
                           for p in o.photos],
            } for i, o in enumerate(r.observations, start=1)],
            "retention": {"completed_days": settings.completed_retention_days,
                          "inactive_days": settings.inactive_round_days},
        }

    def apply_round_fields(r: Round, data: dict):
        r.facility = txt(data, "facility", "Facility Name", True, 300)
        r.department = txt(data, "department", "Department / Unit", True, 300)
        raw = str(data.get("round_date") or "").strip()
        missing = [n for n, v in (("Facility Name", r.facility), ("Department / Unit", r.department),
                                  ("Clinical Round Date", raw)) if not v]
        if missing:
            raise ApiError("Please fill in: " + ", ".join(missing) + ".")
        try:
            r.round_date = date.fromisoformat(raw)
        except ValueError:
            raise ApiError("The Clinical Round Date is not a valid date.")
        r.nurses = txt(data, "nurses", "Number of Nursing Staff", True, 100)
        r.patients = txt(data, "patients", "Number of Patients", True, 100)
        r.support_team = txt(data, "supportTeam", "Administrative Round Support Team", True, 1000)
        r.hospital_action = txt(data, "hospitalAction", "Hospital Nursing Administration Action")
        r.repeat_header = bool(data.get("repeatHeader"))

    def apply_obs_fields(o: Observation, data: dict):
        o.text = txt(data, "text", "Observation")
        if not o.text.strip():
            raise ApiError("The observation text is required.")
        rep = str(data.get("repeated") or "").strip().lower()
        if rep not in ("yes", "no"):
            raise ApiError("Choose Yes or No for “Is the observation repeated?”.")
        o.repeated = rep
        o.recommendations = txt(data, "recommendations", "Recommendations")
        o.corrective_plan = txt(data, "correctivePlan", "Corrective Action Plan")
        o.documentation = txt(data, "documentation", "Documentation / Follow-up")
        o.wide = bool(data.get("wide")) and not o.photos

    def make_thumb(data: bytes) -> bytes:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((360, 360), Image.LANCZOS)
            out = io.BytesIO()
            im.save(out, "JPEG", quality=78, optimize=True)
            return out.getvalue()

    def changed(o: Observation):
        o.version += 1
        o.updated_at = utcnow()
        touch(o.round)

    # ------------------------------------------------------------ hooks
    PUBLIC = {"/", "/healthz", "/api/session", "/api/login", "/api/logout"}

    @app.before_request
    def _before():
        session.permanent = True
        if request.path.startswith("/static/"):
            return
        retention.maybe_purge(Session, settings, log)
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
            sent = request.headers.get("X-CSRF-Token", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                raise ApiError("Your session has expired. Please reload the page.", 403)
        if request.path.startswith("/api/") and request.path not in PUBLIC and not signed_in():
            raise ApiError("Please sign in.", 401)

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if settings.production:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    @app.errorhandler(ApiError)
    def _api_error(e: ApiError):
        if g.get("db") is not None:
            g.db.rollback()
        return jsonify(error=e.message), e.status

    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(_e):
        return jsonify(error=f"The photo is too large (limit {settings.max_photo_mb} MB)."), 413

    @app.errorhandler(HTTPException)
    def _http(e):
        if request.path.startswith("/api/"):
            return jsonify(error=e.description or "Request error."), e.code or 400
        return e

    @app.errorhandler(Exception)
    def _unhandled(_e):
        log.exception("Unhandled error")
        if g.get("db") is not None:
            g.db.rollback()
        return jsonify(error="An unexpected error occurred. Please try again."), 500

    # ------------------------------------------------------------ pages
    @app.get("/")
    def index():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return render_template("index.html", max_photo_mb=settings.max_photo_mb,
                               max_photos=settings.max_photos_per_observation)

    @app.get("/healthz")
    def healthz():
        try:
            db().execute(text("SELECT 1"))
            return jsonify(status="ok")
        except Exception:
            log.exception("Health check failed")
            return jsonify(status="database unavailable"), 503

    # ------------------------------------------------------------ sign-in
    @app.get("/api/session")
    def session_info():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return jsonify(signed_in=signed_in(), auth_required=auth_required, csrf=session["csrf"])

    @app.post("/api/login")
    def login():
        if not auth_required:
            return jsonify(ok=True)
        since = utcnow() - ATTEMPT_WINDOW
        fails = db().execute(select(func.count(AuthAttempt.id)).where(
            AuthAttempt.client_hash == client_hash(), AuthAttempt.at >= since)).scalar_one()
        if fails >= LOGIN_LIMIT:
            raise ApiError("Too many failed attempts. Please wait 15 minutes and try again.", 429)
        pw = str(body().get("password", ""))
        ok = hmac.compare_digest(hashlib.sha256(pw.encode()).digest(),
                                 hashlib.sha256(settings.app_password.encode()).digest())
        if not ok:
            db().add(AuthAttempt(client_hash=client_hash()))
            db().commit()
            raise ApiError("Incorrect password.", 401)
        csrf = session.get("csrf") or secrets.token_urlsafe(32)
        session.clear()
        session.update(sig=login_sig(), csrf=csrf)
        return jsonify(ok=True)

    @app.post("/api/logout")
    def logout():
        session.clear()
        session["csrf"] = secrets.token_urlsafe(32)
        return jsonify(ok=True, csrf=session["csrf"])

    # ------------------------------------------------------------ rounds
    @app.get("/api/rounds")
    def list_rounds():
        rows = db().execute(select(Round).order_by(Round.status, Round.created_at.desc())).scalars().all()
        return jsonify(rounds=[round_summary(r) for r in rows],
                       retention={"completed_days": settings.completed_retention_days,
                                  "inactive_days": settings.inactive_round_days})

    @app.post("/api/rounds")
    def create_round():
        r = Round()
        apply_round_fields(r, body())
        db().add(r)
        db().commit()
        return jsonify(round_state(r)), 201

    @app.get("/api/rounds/<rid>")
    def read_round(rid):
        return jsonify(round_state(get_round(rid)))

    @app.patch("/api/rounds/<rid>")
    def update_round(rid):
        r = get_round(rid)
        require_open(r)
        apply_round_fields(r, body())
        touch(r)
        db().commit()
        return jsonify(round_state(r))

    @app.post("/api/rounds/<rid>/complete")
    def complete_round(rid):
        r = get_round(rid)
        if r.status != "completed":
            r.status = "completed"
            r.completed_at = utcnow()
            r.purge_after = r.completed_at + timedelta(days=settings.completed_retention_days)
            db().commit()
        return jsonify(round_state(r))

    @app.post("/api/rounds/<rid>/reopen")
    def reopen_round(rid):
        r = get_round(rid)
        if r.status != "open":
            r.status, r.completed_at, r.purge_after = "open", None, None
            touch(r)
            db().commit()
        return jsonify(round_state(r))

    @app.delete("/api/rounds/<rid>")
    def delete_round(rid):
        db().delete(get_round(rid))
        db().commit()
        return jsonify(ok=True)

    @app.post("/api/rounds/<rid>/reorder")
    def reorder(rid):
        r = get_round(rid)
        require_open(r)
        order = body().get("order")
        current = {o.id: o for o in r.observations}
        if not isinstance(order, list) or sorted(order) != sorted(current):
            raise ApiError("The observation list changed. Please reload.", 409)
        for i, oid in enumerate(order, start=1):
            current[oid].position = i
        touch(r)
        db().commit()
        db().refresh(r)
        return jsonify(round_state(r))

    # ------------------------------------------------------------ observations
    @app.post("/api/rounds/<rid>/observations")
    def add_observation(rid):
        r = get_round(rid)
        require_open(r)
        if len(r.observations) >= settings.max_observations_per_round:
            raise ApiError("This round has reached the maximum number of observations.", 409)
        max_pos = max([o.position for o in r.observations] or [0])
        o = Observation(round_id=r.id, position=max_pos + 1)
        o.photos = []
        apply_obs_fields(o, body())
        db().add(o)
        touch(r)
        db().commit()
        db().refresh(r)
        return jsonify(id=o.id, version=o.version, state=round_state(r)), 201

    @app.patch("/api/observations/<oid>")
    def update_observation(oid):
        o = get_obs(oid)
        data = body()
        if int(data.get("version", -1)) != o.version:
            raise ApiError("This observation was changed in another window or device. "
                           "Your text is still in the form - copy it, reload, and apply it again.", 409)
        apply_obs_fields(o, data)
        changed(o)
        db().commit()
        db().refresh(o.round)
        return jsonify(version=o.version, state=round_state(o.round))

    @app.delete("/api/observations/<oid>")
    def delete_observation(oid):
        o = get_obs(oid)
        r = o.round
        db().delete(o)
        touch(r)
        db().commit()
        db().refresh(r)
        return jsonify(state=round_state(r))

    # ------------------------------------------------------------ photos
    @app.post("/api/observations/<oid>/photos")
    def add_photo(oid):
        o = get_obs(oid)
        if len(o.photos) >= settings.max_photos_per_observation:
            raise ApiError(f"At most {settings.max_photos_per_observation} photos per observation.", 409)
        fs = request.files.get("photo")
        if fs is None:
            raise ApiError("No photo was received.")
        name = os.path.basename(fs.filename or "photo")[:200]
        ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
        if ext not in ("jpg", "jpeg", "png"):
            raise ApiError(f"“{name}” must be a .jpg, .jpeg or .png file.")
        raw = fs.read()
        fs.close()
        try:
            ph = process_image(raw, name)
        except ReportInputError as e:
            raise ApiError(str(e))
        finally:
            del raw
        p = Photo(observation_id=o.id, position=max([x.position for x in o.photos] or [0]) + 1,
                  filename=name, ext=ph.ext, width_px=ph.width_px, height_px=ph.height_px,
                  size_bytes=len(ph.data), data=ph.data, thumb=make_thumb(ph.data))
        db().add(p)
        o.wide = False
        changed(o)
        db().commit()
        return jsonify(id=p.id, version=o.version), 201

    @app.delete("/api/photos/<pid>")
    def delete_photo(pid):
        p = db().get(Photo, pid)
        if p is None:
            raise ApiError("Photo not found.", 404)
        o = get_obs(p.observation_id)
        db().delete(p)
        changed(o)
        db().commit()
        return jsonify(ok=True, version=o.version)

    @app.post("/api/observations/<oid>/photos/order")
    def order_photos(oid):
        o = get_obs(oid)
        order = body().get("order")
        cur = {p.id: p for p in o.photos}
        if not isinstance(order, list) or sorted(order) != sorted(cur):
            raise ApiError("The photo list changed. Please reload.", 409)
        for i, pid in enumerate(order, start=1):
            cur[pid].position = i
        changed(o)
        db().commit()
        return jsonify(ok=True, version=o.version)

    @app.get("/api/photos/<pid>")
    def get_photo(pid):
        p = db().get(Photo, pid)
        if p is None:
            abort(404)
        thumb = request.args.get("v") == "thumb"
        mime = "image/jpeg" if thumb or p.ext == "jpeg" else "image/png"
        resp = send_file(io.BytesIO(p.thumb if thumb else p.data), mimetype=mime, max_age=0)
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    # ------------------------------------------------------------ Word report
    @app.get("/api/rounds/<rid>/report.docx")
    def report(rid):
        r = get_round(rid)
        if not r.observations:
            raise ApiError("Add at least one observation before generating the report.")
        rd = ReportData(facility=r.facility, department=r.department, round_date=r.round_date,
                        nurses=r.nurses, patients=r.patients, support_team=r.support_team,
                        hospital_action=r.hospital_action, repeat_header=r.repeat_header)
        for o in r.observations:
            rd.observations.append(RObservation(
                text=o.text, recommendations=o.recommendations, repeated=o.repeated,
                corrective_plan=o.corrective_plan, documentation=o.documentation, wide=o.wide,
                photos=[RPhoto(p.data, p.ext, p.width_px, p.height_px) for p in o.photos]))
        try:
            data = build_report(app.template_bytes, rd)
        except ReportInputError as e:
            raise ApiError(str(e))
        except TemplateError as e:
            log.error("Template error: %s", e)
            raise ApiError("The Word template could not be used: " + str(e), 500)
        finally:
            for ob in rd.observations:
                ob.photos.clear()
        return send_file(io.BytesIO(data), as_attachment=True,
                         download_name=safe_filename(r.department, r.round_date),
                         mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                         max_age=0)

    return app


# Gunicorn entry point:  gunicorn app:app
try:
    app = create_app()
except ConfigError as exc:   # clear message instead of a traceback on Render
    raise SystemExit(f"Configuration error: {exc}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print(f"\n  Clinical Round running at http://127.0.0.1:{port}  (Ctrl+C to stop)\n")
    app.run(host="127.0.0.1", port=port, debug=False)
