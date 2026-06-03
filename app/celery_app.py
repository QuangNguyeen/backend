import logging

from celery import Celery
from celery.signals import setup_logging

from app.config import get_settings

settings = get_settings()

celery = Celery(
    "dictalearn",
    broker=settings.celery_broker,
    backend=settings.celery_backend,
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    worker_pool="prefork",
)

celery.conf.include = ["app.tasks.transcription", "app.tasks.vocabulary_enrichment"]


@setup_logging.connect
def configure_worker_logging(**kwargs):
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s: %(levelname)s/%(name)s] %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    logging.getLogger("app").setLevel(logging.INFO)
