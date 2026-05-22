from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.dictation import router as dictation_router
from app.api.v1.dictionary import router as dictionary_router
from app.api.v1.users import router as users_router
from app.api.v1.videos import router as videos_router
from app.api.v1.vocabulary import router as vocabulary_router
from app.api.v1.quiz import router as quiz_router
from app.api.v1.rooms import router as rooms_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(videos_router)
api_router.include_router(dictation_router)
api_router.include_router(dashboard_router)
api_router.include_router(vocabulary_router)
api_router.include_router(dictionary_router)
api_router.include_router(quiz_router)
api_router.include_router(rooms_router)
