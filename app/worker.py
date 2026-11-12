import argparse
import logging

from app.config import get_settings
from app.db import init_db
from app.scheduler import worker_loop

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Research opportunity background worker")
    parser.add_argument("--once", action="store_true", help="Process due jobs and exit")
    args = parser.parse_args()
    init_db()
    if args.once:
        from app.scheduler import claim_due_users, process_user
        processed = 0
        while True:
            user_ids = claim_due_users(limit=25)
            if not user_ids:
                break
            for user_id in user_ids:
                process_user(user_id)
                processed += 1
        logging.getLogger(__name__).info(
            "Scheduled run finished; processed %s user jobs", processed
        )
    else:
        worker_loop()
