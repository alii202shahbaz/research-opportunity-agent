import logging

from app.config import get_settings
from app.db import init_db
from app.scheduler import worker_loop

settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

if __name__ == "__main__":
    init_db()
    worker_loop()
