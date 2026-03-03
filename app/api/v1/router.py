from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.videos import router as videos_router
from app.api.v1.dictation import router as dictation_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.vocabulary import router as vocabulary_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(videos_router)
api_router.include_router(dictation_router)
api_router.include_router(dashboard_router)
api_router.include_router(vocabulary_router)
