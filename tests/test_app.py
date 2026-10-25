"""
Automated tests for Clinical Round (single-user).

Run:   python tests/make_fixtures.py          (synthetic photos, once)
       pytest -q tests/                       (SQLite)
       TEST_DATABASE_URL=postgresql://user:pass@localhost/db pytest -q tests/   (PostgreSQL)
"""
import io
import os
import sys
import tempfile
import zipfile
from datetime import timedelta

import pytest
from lxml import etree
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-123456")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "import.db"))

import config  # noqa: E402
import retention  # noqa: E402
from app import create_app  # noqa: E402
from models import AuthAttempt, Photo, Round, utcnow  # noqa: E402

FX = os.path.join(ROOT, "tests", "fixtures")
TEMPLATE = os.path.join(ROOT, "assets", "clinical_round_template.docx")
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
PASSWORD = "correct-horse-battery"


def fx(name):
    with open(os.path.join(FX, name), "rb") as f:
        return f.read()


@pytest.fixture()
def app(tmp_path):
    url = os.environ.get("TEST_DATABASE_URL") or "sqlite:///" + str(tmp_path / "t.db")
    s = config.Settings({"DATABASE_URL": url, "SECRET_KEY": "k" * 40, "APP_PASSWORD": PASSWORD})
    a = create_app(s)
    # clean slate for PostgreSQL runs
    with a.Session() as db:
        for tbl in ("photos", "observations", "rounds", "auth_attempts"):
            db.execute(__import__("sqlalchemy").text(f"DELETE FROM {tbl}"))
        db.commit()
    yield a
    a.engine.dispose()


class Client:
    def __init__(self, app):
        self.c = app.test_client()
        self.csrf = self.c.get("/api/session").get_json()["csrf"]

    def h(self):
        return {"X-CSRF-Token": self.csrf}

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, json=None, **kw):
        return self.c.post(url, json=json, headers=self.h(), **kw)

    def patch(self, url, json=None):
        return self.c.patch(url, json=json, headers=self.h())

    def delete(self, url):
        return self.c.delete(url, headers=self.h())

    def upload(self, oid, name, data=None):
        return self.c.post(f"/api/observations/{oid}/photos", headers=self.h(),
                           data={"photo": (io.BytesIO(data if data is not None else fx(name)), name)},
                           content_type="multipart/form-data")

    def login(self, pw=PASSWORD):
        return self.post("/api/login", {"password": pw})


@pytest.fixture()
def client(app):
    c = Client(app)
    assert c.login().status_code == 200
    return c


META = {"facility": "مستشفى الاختبار", "department": "الجراحة 2A", "round_date": "2026-10-21",
        "nurses": "6", "patients": "42", "supportTeam": "عضو أول، Test Member", "hospitalAction": ""}


def new_round(c, **over):
    r = c.post("/api/rounds", dict(META, **over))
    assert r.status_code == 201, r.get_json()
    return r.get_json()["round"]["id"]


def add_obs(c, rid, text="ملاحظة", rep="no", **kw):
    r = c.post(f"/api/rounds/{rid}/observations", dict(text=text, repeated=rep, **kw))
    assert r.status_code == 201, r.get_json()
    return r.get_json()


# ======================================================== configuration / secrets
def test_production_requires_secrets():
    base = {"RENDER": "true"}
    with pytest.raises(config.ConfigError, match="DATABASE_URL"):
        config.Settings(base)
    with pytest.raises(config.ConfigError, match="SECRET_KEY"):
        config.Settings(dict(base, DATABASE_URL="postgres://u:p@h/d"))
    with pytest.raises(config.ConfigError, match="APP_PASSWORD"):
        config.Settings(dict(base, DATABASE_URL="postgres://u:p@h/d", SECRET_KEY="x" * 40))
    with pytest.raises(config.ConfigError, match="SQLite"):
        config.Settings(dict(base, DATABASE_URL="sqlite:///x.db", SECRET_KEY="x" * 40, APP_PASSWORD="y" * 12))
    s = config.Settings(dict(base, DATABASE_URL="postgres://u:p@h/d", SECRET_KEY="x" * 40, APP_PASSWORD="y" * 12))
    assert s.database_url.startswith("postgresql+psycopg://") and s.secure_cookies


def test_no_secrets_in_repository():
    import subprocess
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert ".env" not in tracked
    for f in tracked:
        if f.endswith((".py", ".yaml", ".yml", ".md", ".txt", ".js", ".html", ".example")):
            body = open(os.path.join(ROOT, f), encoding="utf-8").read()
            assert PASSWORD not in body or f.startswith("tests/"), f


# ======================================================== authentication / security
def test_sign_in_required_and_rate_limited(app):
    c = Client(app)
    assert c.get("/api/rounds").status_code == 401
    assert c.post("/api/rounds", META).status_code == 401
    assert c.login("wrong").status_code == 401
    for _ in range(9):
        c.login("wrong")
    r = c.login(PASSWORD)
    assert r.status_code == 429, "11th attempt must be blocked even with the right password"
    with app.Session() as db:   # 15 minutes later
        for a in db.query(AuthAttempt).all():
            a.at = utcnow() - timedelta(minutes=16)
        db.commit()
    assert c.login(PASSWORD).status_code == 200
    assert c.get("/api/rounds").status_code == 200
    c.post("/api/logout")
    c.csrf = c.get("/api/session").get_json()["csrf"]
    assert c.get("/api/rounds").status_code == 401


def test_csrf_token_required(client):
    r = client.c.post("/api/rounds", json=META)          # no header
    assert r.status_code == 403
    r = client.c.post("/api/rounds", json=META, headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403


def test_photos_require_sign_in(app, client):
    rid = new_round(client)
    oid = add_obs(client, rid)["id"]
    pid = client.upload(oid, "portrait_1.jpg").get_json()["id"]
    anon = app.test_client()
    assert anon.get(f"/api/photos/{pid}").status_code == 401
    assert anon.get(f"/api/rounds/{rid}/report.docx").status_code == 401


def test_security_headers(client):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "no-store" in r.headers["Cache-Control"]
    assert client.get("/healthz").get_json() == {"status": "ok"}


def test_local_mode_without_password(tmp_path):
    s = config.Settings({"DATABASE_URL": "sqlite:///" + str(tmp_path / "l.db"), "SECRET_KEY": "k" * 40})
    a = create_app(s)
    c = Client(a)
    assert c.get("/api/session").get_json()["auth_required"] is False
    assert c.get("/api/rounds").status_code == 200


# ======================================================== rounds & observations
def test_round_validation_and_update(client):
    r = client.post("/api/rounds", {"facility": "", "department": "", "round_date": ""})
    assert r.status_code == 400 and "Facility Name" in r.get_json()["error"]
    r = client.post("/api/rounds", dict(META, round_date="21/10/2026"))
    assert r.status_code == 400
    rid = new_round(client)
    st = client.patch(f"/api/rounds/{rid}", dict(META, patients="50", hospitalAction="خطة")).get_json()
    assert st["round"]["patients"] == "50" and st["round"]["hospitalAction"] == "خطة"
    assert st["round"]["filename"] == "Clinical_Round_2A_2026-10-21.docx"


def test_observation_crud_order_and_conflict(client):
    rid = new_round(client)
    a = add_obs(client, rid, "أولى")["id"]
    b = add_obs(client, rid, "ثانية", "yes")["id"]
    c = add_obs(client, rid, "ثالثة")["id"]
    assert client.post(f"/api/rounds/{rid}/observations", {"text": "", "repeated": "no"}).status_code == 400
    assert client.post(f"/api/rounds/{rid}/observations", {"text": "x"}).status_code == 400
    st = client.post(f"/api/rounds/{rid}/reorder", {"order": [c, a, b]}).get_json()
    assert [o["text"] for o in st["observations"]] == ["ثالثة", "أولى", "ثانية"]
    assert client.post(f"/api/rounds/{rid}/reorder", {"order": [a, b]}).status_code == 409
    v = [o for o in st["observations"] if o["id"] == a][0]["version"]
    ok = client.patch(f"/api/observations/{a}", {"text": "أولى معدلة", "repeated": "yes", "version": v})
    assert ok.status_code == 200
    stale = client.patch(f"/api/observations/{a}", {"text": "قديم", "repeated": "no", "version": v})
    assert stale.status_code == 409, "edit from an older version must not overwrite newer text"
    st = client.delete(f"/api/observations/{b}").get_json()["state"]
    assert [o["text"] for o in st["observations"]] == ["ثالثة", "أولى معدلة"]


# ======================================================== photos
def test_photo_upload_validation_and_storage(app, client):
    rid = new_round(client)
    oid = add_obs(client, rid)["id"]
    assert client.upload(oid, "rotated_exif6.jpg").status_code == 201
    assert client.upload(oid, "screenshot.png").status_code == 201
    for bad in ("not_an_image.jpg", "truncated.jpg"):
        r = client.upload(oid, bad)
        assert r.status_code == 400 and "photo" in r.get_json()["error"].lower() or "image" in r.get_json()["error"].lower()
    assert client.upload(oid, "document.gif").status_code == 400
    big = client.upload(oid, "huge.jpg", b"\xff\xd8\xff" + os.urandom(17 * 1024 * 1024))
    assert big.status_code == 413
    st = client.get(f"/api/rounds/{rid}").get_json()
    photos = st["observations"][0]["photos"]
    assert len(photos) == 2
    full = Image.open(io.BytesIO(client.get(f"/api/photos/{photos[0]['id']}").data))
    assert full.height > full.width, "EXIF orientation applied"
    assert len(full.getexif()) == 0, "metadata removed"
    thumb = client.get(f"/api/photos/{photos[0]['id']}?v=thumb")
    assert thumb.mimetype == "image/jpeg" and max(Image.open(io.BytesIO(thumb.data)).size) <= 360
    assert "no-store" in thumb.headers["Cache-Control"]
    # reorder + delete
    order = [photos[1]["id"], photos[0]["id"]]
    assert client.post(f"/api/observations/{oid}/photos/order", {"order": order}).status_code == 200
    st = client.get(f"/api/rounds/{rid}").get_json()
    assert [p["id"] for p in st["observations"][0]["photos"]] == order
    assert client.delete(f"/api/photos/{order[0]}").status_code == 200
    with app.Session() as db:
        assert db.query(Photo).count() == 1


def test_wide_row_only_without_photos(client):
    rid = new_round(client)
    o = add_obs(client, rid, wide=True)
    assert o["state"]["observations"][0]["wide"] is True
    client.upload(o["id"], "portrait_1.jpg")
    assert client.get(f"/api/rounds/{rid}").get_json()["observations"][0]["wide"] is False


# ======================================================== completion & retention
def test_complete_reopen_and_read_only(client):
    rid = new_round(client)
    oid = add_obs(client, rid)["id"]
    st = client.post(f"/api/rounds/{rid}/complete").get_json()
    assert st["round"]["status"] == "completed"
    from datetime import datetime
    due = datetime.fromisoformat(st["round"]["delete_due"]) - datetime.fromisoformat(st["round"]["completed_at"])
    assert due == timedelta(days=7)
    assert client.post(f"/api/rounds/{rid}/observations", {"text": "x", "repeated": "no"}).status_code == 409
    assert client.patch(f"/api/observations/{oid}", {"text": "x", "repeated": "no", "version": 1}).status_code == 409
    assert client.upload(oid, "portrait_1.jpg").status_code == 409
    assert client.get(f"/api/rounds/{rid}/report.docx").status_code == 200, "report still available"
    st = client.post(f"/api/rounds/{rid}/reopen").get_json()
    assert st["round"]["status"] == "open" and st["round"]["completed_at"] is None


def test_retention_purge(app, client):
    keep = new_round(client)
    done_old = new_round(client, department="OLD")
    stale = new_round(client, department="STALE")
    done_recent = new_round(client, department="RECENT")
    for rid in (done_old, stale, done_recent):
        oid = add_obs(client, rid)["id"]
        client.upload(oid, "portrait_2.jpg")
    client.post(f"/api/rounds/{done_old}/complete")
    client.post(f"/api/rounds/{done_recent}/complete")
    with app.Session() as db:
        db.get(Round, done_old).purge_after = utcnow() - timedelta(minutes=1)
        db.get(Round, stale).last_activity_at = utcnow() - timedelta(days=31)
        db.add(AuthAttempt(client_hash="x", at=utcnow() - timedelta(hours=25)))
        db.commit()
        res = retention.purge_expired(db, app.settings)
    assert res == {"rounds_deleted": 2, "photos_deleted": 2, "signin_records_deleted": 1}
    ids = {r["id"] for r in client.get("/api/rounds").get_json()["rounds"]}
    assert ids == {keep, done_recent}
    with app.Session() as db:
        assert db.query(Photo).count() == 1, "photos of deleted rounds are gone"


def test_delete_now(app, client):
    rid = new_round(client)
    client.upload(add_obs(client, rid)["id"], "portrait_1.jpg")
    assert client.delete(f"/api/rounds/{rid}").status_code == 200
    assert client.get(f"/api/rounds/{rid}").status_code == 404
    with app.Session() as db:
        assert db.query(Photo).count() == 0


# ======================================================== Word report
def _doc(resp):
    z = zipfile.ZipFile(io.BytesIO(resp.data))
    assert z.testzip() is None
    return z, etree.fromstring(z.read("word/document.xml"))


def _txt(el):
    return "".join(t.text or "" for t in el.iter(W + "t"))


def test_word_report_from_database(client, tmp_path):
    rid = new_round(client, hospitalAction="رفع طلب صيانة")
    o1 = add_obs(client, rid, "لوحظ وجود المحلولين\nNormal Saline (0.9% NS)", "no", recommendations="فصل المحلولين")["id"]
    client.upload(o1, "portrait_1.jpg")
    client.upload(o1, "landscape_1.jpg")
    add_obs(client, rid, "بدون صور", "yes", correctivePlan="خطة", documentation="تم")
    add_obs(client, rid, "صف عريض", "no", wide=True)
    r = client.get(f"/api/rounds/{rid}/report.docx")
    assert r.status_code == 200
    assert "Clinical_Round_2A_2026-10-21.docx" in r.headers["Content-Disposition"]
    z, doc = _doc(r)
    rows = doc.find(W + "body").find(W + "tbl").findall(W + "tr")
    meta = _txt(rows[0]) + _txt(rows[1])
    for v in ("مستشفى الاختبار", "الجراحة 2A", "21 اكتوبر 2026", "عدد التمريض: 6", "عدد المرضى: 42",
              "رفع طلب صيانة", "Test Member"):
        assert v in meta, v
    obs = rows[3:]
    assert len(obs) == 3
    assert len(obs[0].findall(".//" + W + "drawing")) == 2
    assert _txt(obs[1].findall(W + "tc")[1]).strip() == "لايوجد"
    assert _txt(obs[1].findall(W + "tc")[3]) == "نعم"
    assert len(obs[2].findall(W + "tc")) == 5, "wide row uses template merged row"
    # template integrity: everything except body content is byte-identical
    t = zipfile.ZipFile(TEMPLATE)
    for n in t.namelist():
        if n not in ("word/document.xml", "word/_rels/document.xml.rels", "[Content_Types].xml"):
            assert z.read(n) == t.read(n), n
    import docx
    p = tmp_path / "out.docx"
    p.write_bytes(r.data)
    assert len(docx.Document(str(p)).tables[0].columns) == 6
    # reports are not stored anywhere: nothing new in the DB
    assert client.get(f"/api/rounds/{rid}/report.docx").data[:2] == b"PK"


def test_report_needs_observations(client):
    rid = new_round(client)
    r = client.get(f"/api/rounds/{rid}/report.docx")
    assert r.status_code == 400 and "at least one observation" in r.get_json()["error"]


def test_no_files_written_to_disk(client):
    watch = [tempfile.gettempdir(), ROOT]

    def snap():
        out = set()
        for d in watch:
            for root, dirs, files in os.walk(d):
                dirs[:] = [x for x in dirs if x not in (".git", "__pycache__", ".pytest_cache", "pytest-of-root")
                           and not x.startswith("pytest-")]
                out.update(os.path.join(root, f) for f in files)
        return out

    before = snap()
    rid = new_round(client)
    oid = add_obs(client, rid)["id"]
    for n in ("portrait_1.jpg", "screenshot.png", "truncated.jpg", "not_an_image.jpg"):
        client.upload(oid, n)
    client.get(f"/api/rounds/{rid}/report.docx")
    after = snap()
    new = {f for f in after - before if not f.endswith((".db", ".db-journal", ".db-wal", ".db-shm"))}
    assert not new, new
