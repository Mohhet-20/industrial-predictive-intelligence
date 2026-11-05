"""Start the hosted public demo (deploy/demo/Dockerfile): fresh database, locked accounts, then the API.

The image build already produced /app/demo/showcase.db: the synthetic dataset plus a trained detector, a flagged
anomaly, an investigation and a scenario run (scripts/e2e_acceptance_test.py and e2e_scenario_acceptance_test.py),
so a cold start takes seconds instead of re-running the pipeline on a small free instance. On every start this:

1. copies the showcase database to a fresh working file, so each restart is a clean demo;
2. gives every seeded account (the documented demo admin and viewer included) a random password nobody knows, so the
   credentials printed in SETUP.md and the build scripts cannot be used against the public site;
3. creates the guest engineer behind the sign-in page's "Explore the live demo" button (POST /auth/guest; it can
   ingest data, run detectors and review, not administer) and, when PRED_ADMIN_EMAIL and PRED_ADMIN_PASSWORD are
   set as hosting secrets, the owner's administrator;
4. replaces itself with uvicorn on $PORT.
"""

from __future__ import annotations

import os
import secrets
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
SHOWCASE = Path(os.environ.get("PRED_SHOWCASE_DB", "/app/demo/showcase.db"))
WORKING = Path(os.environ.get("PRED_WORKING_DB", "/tmp/pred_demo.db"))  # noqa: S108 - container-local scratch file
GUEST_EMAIL = "guest@riverbend-demo.io"
DEMO_ORG_ADMIN = "admin@riverbend-demo.io"


def main() -> int:
    if not SHOWCASE.is_file():
        print(f"[demo] {SHOWCASE} is missing; the image build did not produce the showcase database", file=sys.stderr)
        return 2
    # Remove the previous run's database AND its journal files first: SQLite would replay a stale -wal onto the fresh
    # copy and resurrect the old rows.
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{WORKING}{suffix}").unlink(missing_ok=True)
    shutil.copyfile(SHOWCASE, WORKING)
    os.environ["DATABASE_URL"] = f"sqlite:///{WORKING.as_posix()}"
    os.environ["GUEST_ACCESS_ENABLED"] = "true"
    os.environ["GUEST_EMAIL"] = GUEST_EMAIL
    sys.path.insert(0, str(BACKEND))

    from app.core.security import hash_password
    from app.db.session import SessionLocal
    from app.models.user import User, UserRole

    db = SessionLocal()
    try:
        users = db.query(User).all()
        for user in users:
            user.hashed_password = hash_password(secrets.token_urlsafe(32))
        org_id = next((u.organization_id for u in users if u.email == DEMO_ORG_ADMIN), None)
        if org_id is None:
            print("[demo] showcase database has no demo organisation", file=sys.stderr)
            return 2
        db.add(User(organization_id=org_id, email=GUEST_EMAIL, full_name="Guest engineer", role=UserRole.ENGINEER,
                    is_active=True, hashed_password=hash_password(secrets.token_urlsafe(32))))
        owner_email = os.environ.get("PRED_ADMIN_EMAIL", "").strip().lower()
        owner_password = os.environ.get("PRED_ADMIN_PASSWORD", "")
        if owner_email and owner_password:
            if len(owner_password) < 12:
                print("[demo] PRED_ADMIN_PASSWORD must be at least 12 characters", file=sys.stderr)
                return 2
            db.add(User(organization_id=org_id, email=owner_email, full_name="Owner", role=UserRole.ADMIN,
                        is_active=True, hashed_password=hash_password(owner_password)))
            print(f"[demo] owner administrator {owner_email} ready", flush=True)
        else:
            print("[demo] PRED_ADMIN_EMAIL/PRED_ADMIN_PASSWORD not set: no administrator account", flush=True)
        db.commit()
        print(f"[demo] locked {len(users)} seeded accounts; guest engineer ready", flush=True)
    finally:
        db.close()

    port = os.environ.get("PORT", "8000")
    os.chdir(BACKEND)
    cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", port]  # noqa: S104
    os.execvpe(cmd[0], cmd, os.environ)  # noqa: S606 - hand PID 1 to uvicorn so it receives SIGTERM
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
