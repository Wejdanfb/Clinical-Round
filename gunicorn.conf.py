"""Gunicorn settings for Render (start command: gunicorn app:app)."""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
# Small instance (512 MB): 2 workers x 4 threads is plenty for one user.
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
worker_class = "gthread"
timeout = 120            # Word generation with many photos can take several seconds
graceful_timeout = 30
keepalive = 5
max_requests = 500       # recycle workers periodically to keep memory low
max_requests_jitter = 50
accesslog = "-"          # request lines only (method, path, status) - no form data
errorlog = "-"
loglevel = "info"
forwarded_allow_ips = "*"   # Render's proxy sets X-Forwarded-*

# Load the app once in the master: a configuration error (e.g. missing
# APP_PASSWORD) then stops start-up immediately with a clear log message
# instead of workers restarting in a loop.
preload_app = True


def post_fork(server, worker):
    # Each worker gets its own database connections (SQLAlchemy guidance
    # for forked processes).
    from app import app
    app.engine.dispose(close=False)
