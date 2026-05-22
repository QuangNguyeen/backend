import json
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class RoomConnectionManager:
    def __init__(self):
        self._connections: dict[str, dict[str, WebSocket]] = {}

    async def connect(self, room_code: str, user_id: str, ws: WebSocket):
        await ws.accept()
        self._connections.setdefault(room_code, {})[user_id] = ws
        logger.info("WS connected: room=%s user=%s", room_code, user_id)

    async def disconnect(self, room_code: str, user_id: str):
        room_conns = self._connections.get(room_code, {})
        room_conns.pop(user_id, None)
        if not room_conns:
            self._connections.pop(room_code, None)

    async def broadcast(self, room_code: str, message: dict):
        payload = json.dumps(message)
        for ws in list(self._connections.get(room_code, {}).values()):
            try:
                await ws.send_text(payload)
            except Exception:
                pass

    async def send_personal(self, room_code: str, user_id: str, message: dict):
        ws = self._connections.get(room_code, {}).get(user_id)
        if ws:
            try:
                await ws.send_json(message)
            except Exception:
                pass

    def get_connected_user_ids(self, room_code: str) -> list[str]:
        return list(self._connections.get(room_code, {}).keys())


manager = RoomConnectionManager()
