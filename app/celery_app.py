from celery import Celery

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
    worker_pool="solo",
)

celery.conf.include = ["app.tasks.transcription"]
