# Clinical Round

A personal, single-user web application for nursing clinical rounds. You create a round, enter its details, and keep all its observations (with photographs) in one table. You review and edit everything, then generate the Word report on the official form **نموذج الجولات الادارية للإدارة التنفيذية لخدمات التمريض**.

- The report is built from the **official blank template** (`assets/clinical_round_template.docx`), which is kept unchanged. The completed sample report was used only as a reference for how content is placed and formatted.
- Single user: no team accounts, roles, invitations or sharing. When hosted, one password protects the whole app.
- Round data is kept only while you need it (see [Data retention](#data-retention)). Word reports are never stored.

---

## How to use it

1. **Sign in** with your password. This is skipped when you run it locally without a password.
2. **New Round**: enter Facility Name, Department / Unit and Clinical Round Date (required). Nursing staff, patients, support team and the hospital's corrective action plan are optional. Click **Create round**.
3. **Add Observation** for each finding:
   - Observation (required)
   - Photos (JPEG/PNG, up to 15 MB each, up to 12 per observation, in the order you choose)
   - Recommendations
   - Repeated? Yes/No (required; written as نعم / لا)
   - Corrective Action Plan
   - Documentation / Follow-up
4. All observations appear in **one table** laid out like the report. Each row can be edited (✎), deleted (✕) or moved (↑ ↓). On a phone, the table turns into cards.
5. **Generate Word Report** downloads `Clinical_Round_<Unit>_<YYYY-MM-DD>.docx`. You can generate it again after any change.
6. When you are done, **Mark round completed**. The round becomes read-only and is permanently deleted 7 days later. **Delete now** removes it immediately. **Reopen round** lets you edit it again before then.

How the Word report is filled:

| Template element | Result |
|---|---|
| Logo header, title, blue metadata block, grey column titles, footer graphics, page size and margins, fonts, borders | Kept exactly as in the template. |
| Metadata labels (اسم المنشأة، القسم، التاريخ، …) | Your values are written after each label. The date is written as `21 اكتوبر 2026`. |
| Observation rows | Copied from the template's own empty row, one per observation, so borders and widths stay identical. |
| Photos | Placed in that observation's evidence cell, about 1.9 in wide, with the aspect ratio kept. |
| No photo | The evidence cell reads **لايوجد**, as in the sample. |
| "Text-only wide row" (optional) | Uses the template's merged last row, where the observation spans the photo column. |
| "Repeat header on every page" (optional) | Repeats the blue block and the column titles on each page. This uses more pages. |

---

## Run it locally (Windows)

Requirements: **Python 3.12 or newer** from <https://www.python.org/downloads/>. Tick "Add python.exe to PATH" during installation.

**Easiest:** double-click **`run_local.bat`**. The first run creates `.venv` and installs the packages (internet needed once). It then opens <http://127.0.0.1:5000>.

**Manual (Command Prompt in the project folder):**

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py
```

Open <http://127.0.0.1:5000>. **Stop** it with **Ctrl + C** or by closing the window.

Local defaults:
- Data goes into a SQLite file at `instance\clinical_round_dev.db` on your computer.
- With no `APP_PASSWORD`, the app opens without a sign-in. It listens only on `127.0.0.1`, so other computers cannot reach it.
- To use a password or fixed settings locally, copy `.env.example` to `.env` and fill it in. `.env` is git-ignored and must never be committed.

---

## Deploy to Render

The repository includes a Render Blueprint (`render.yaml`). It creates a web service and a PostgreSQL database, and links them together.

1. Sign in at <https://dashboard.render.com> and connect your GitHub account if you haven't already.
2. Click **New → Blueprint** and select the repository **Wejdanfb/Clinical-Round** (branch `main`).
3. Render reads `render.yaml` and asks for **APP_PASSWORD**. Enter a strong password of at least 10 characters. This is the password you will sign in with.
4. Click **Apply**. Render then:
   - creates the PostgreSQL database `clinical-round-db`, reachable only on Render's private network;
   - generates a random `SECRET_KEY`;
   - builds with `pip install -r requirements.txt` and starts with `gunicorn app:app -c gunicorn.conf.py`;
   - checks `/healthz` before sending traffic.
5. Open the `https://clinical-round-….onrender.com` address Render shows and sign in.

`autoDeploy` is off, so pushing to GitHub does not redeploy by itself. Use **Manual Deploy → Deploy latest commit** in Render whenever you want to update.

**Plans: important.** The Blueprint uses Render's **free** plans to avoid charges:
- **The free PostgreSQL database expires 30 days after creation** and its data is deleted ([Render: free tier](https://render.com/docs/free)). For ongoing use, change the database to a paid plan in Render, or recreate it every month.
- The free web service sleeps after a period of inactivity. The first visit afterwards takes about a minute.

**Without the Blueprint (manual setup):** create a PostgreSQL database (Access: private only), then a Python Web Service from this repository with:

| Setting | Value |
|---|---|
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn app:app -c gunicorn.conf.py` |
| Health check path | `/healthz` |
| Environment variables | `APP_ENV=production`, `DATABASE_URL` = the database's *Internal Database URL*, `SECRET_KEY` = a long random value, `APP_PASSWORD` = your password |

**Changing the password:** edit `APP_PASSWORD` under the service's **Environment** settings and redeploy. Existing sign-ins end automatically.

If a required setting is missing, the service refuses to start and the Render log names the problem. For example: `Configuration error: APP_PASSWORD must be set and at least 10 characters long.`

---

## Data retention

| What | Where | Removed |
|---|---|---|
| Round details, observation text | Database (PostgreSQL on Render, SQLite locally) | Automatically, by the rules below |
| Photos | Database, together with their observation. Metadata such as GPS location and camera details is removed on upload, and large photos are downscaled. | With their round, in the same transaction |
| Generated Word reports | **Never stored.** Built in memory and sent straight to your browser. | Not applicable |
| Files on the server's disk | **None.** Uploads are processed in memory and never written to disk. | Not applicable |
| Failed sign-in records (rate limiting) | Database. Only a keyed hash of the address is kept, not the IP itself. | After 24 hours |

Automatic deletion rules (configurable with environment variables):
- **Completed rounds** are permanently deleted **7 days** after you mark them completed (`COMPLETED_ROUND_RETENTION_DAYS`).
- **Rounds left in progress** are permanently deleted after **30 days** without any change (`INACTIVE_ROUND_DAYS`).
- **Delete now** removes a round and its photos immediately.

The app runs the cleanup at start-up and at most every 10 minutes while it is being used. A free Render service that is asleep runs it on its next visit. If deletion must happen on schedule even when nobody uses the app, add a **Render Cron Job** (a paid Render feature) with the command `python manage.py purge-expired` and the same environment variables. After rows are deleted, PostgreSQL reclaims the disk space automatically over time (autovacuum).

---

## Security

- One password (`APP_PASSWORD`) protects every page and API call when hosted. After 10 failed attempts within 15 minutes, further attempts are blocked.
- Session cookies are `HttpOnly`, `Secure` (HTTPS) and `SameSite=Lax`, and expire after 12 hours (`SESSION_HOURS`). Every change requires a CSRF token.
- Strict Content-Security-Policy, no framing, no referrer, HSTS. No third-party scripts, fonts, analytics or external calls.
- Photos are served only to a signed-in browser and are not cached (`no-store`).
- Secrets live only in environment variables. `.env` is git-ignored, and `render.yaml` contains no secret values.

**Clinical data on a hosted service:** having a password and no long-term storage does not make the hosting private. Render, as the hosting provider, operates the servers and database that hold your round data while it exists. Before entering real patient information, confirm with your organisation's information-security team that this hosting is approved. Avoid patient identifiers in text and photos where possible, or run the app locally instead.

---

## Tests

```bat
.venv\Scripts\python -m pip install pytest python-docx
.venv\Scripts\python tests\make_fixtures.py
.venv\Scripts\python -m pytest -q tests
```

To test against PostgreSQL as well, set `TEST_DATABASE_URL=postgresql://user:password@host/dbname` before running `pytest`.

The suite (23 tests) covers:
- production refusing to start without its secrets, and no secrets in the repository;
- sign-in, rate limiting and CSRF;
- rounds, observations, reordering, and stale-edit protection;
- photo validation, orientation and metadata removal;
- read-only completed rounds and the retention rules;
- nothing written to disk;
- Word output: metadata, photos, لايوجد, the wide row, template byte-integrity, long text, many pages and the repeat-header option.

---

## Project structure

```
app.py               Flask app: sign-in, rounds, observations, photos, Word download
report_builder.py    Word generation from the official template (XML-level)
models.py            Database tables (rounds, observations, photos, sign-in attempts)
retention.py         Retention policy and cleanup
config.py            Settings from environment variables
manage.py            init-db / purge-expired commands
gunicorn.conf.py     Production server settings (Render)
render.yaml          Render Blueprint (web service + PostgreSQL)
requirements.txt     Pinned dependencies
.python-version      Python version for Render
.env.example         Template for local settings (copy to .env - never commit .env)
run_local.bat        Windows launcher
assets/clinical_round_template.docx   official blank form (unchanged)
templates/index.html, static/app.js, static/style.css   interface
tests/               automated tests + synthetic photo generator
```

## Template notes

- The report uses the **Janna LT** font named by the template. Install it on the computer that opens the report, as with the original form; otherwise Word substitutes a similar font.
- The template's footer wave and "King Salman Medical City" mark sit behind the text, slightly inside the page's text area. On a completely full page, the bottom of the table can run over that mark. The text stays readable on top. This comes from the official template, which is left unchanged.
- The template ends with five empty lines after the table. These could push a blank page onto the end of the report, so the app keeps one of them (Word needs one after a table) at 1 pt height and removes the others.
- The filename uses the Latin letters and numbers from the department (`الجراحة 2A` → `Clinical_Round_2A_2026-10-21.docx`), because browsers replace Arabic download names with "download".
