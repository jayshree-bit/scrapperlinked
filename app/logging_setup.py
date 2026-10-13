import logging
from logging.handlers import RotatingFileHandler
from app.config import LOG_LEVEL


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler("verifier.log", maxBytes=2_000_000, backupCount=3),
        ],
    )
