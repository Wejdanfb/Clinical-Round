"""Maintenance commands.

  python manage.py init-db          create tables (also done automatically at start-up)
  python manage.py purge-expired    apply the retention policy now (e.g. as a Render Cron Job)
"""
import json
import sys

from config import load_settings
from models import init_db, make_engine, make_session_factory
from retention import purge_expired


def main(argv):
    if len(argv) < 2 or argv[1] not in ("init-db", "purge-expired"):
        print(__doc__)
        return 2
    settings = load_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    if argv[1] == "purge-expired":
        db = make_session_factory(engine)()
        try:
            print(json.dumps(purge_expired(db, settings)))
        finally:
            db.close()
    else:
        print("Database tables are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
