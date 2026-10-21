"""One WSGI process owns the in-memory jobs and RSS scheduler."""
import logging
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    public_url = os.environ.get("WECHAT_MP_PUBLIC_URL", "")
    if public_url:
        parsed = urlsplit(public_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise SystemExit("WECHAT_MP_PUBLIC_URL must be an http(s) origin, without a path or credentials")

    from backend.config import DATA_DIR, ensure_dirs
    try:
        ensure_dirs()
        # Fail immediately with a useful NAS permissions error, not on first login.
        import tempfile
        with tempfile.TemporaryFile(dir=DATA_DIR):
            pass
    except OSError as exc:
        raise SystemExit(f"Cannot write {DATA_DIR}; grant the container UID/GID access to the dataset: {exc}")

    from app import app
    from waitress import serve
    # Do not run multiple workers/replicas: queues, account state and JSON stores
    # are process-local and app import starts the RSS scheduler.
    serve(app, host="0.0.0.0", port=5200, threads=8)


if __name__ == "__main__":
    main()
