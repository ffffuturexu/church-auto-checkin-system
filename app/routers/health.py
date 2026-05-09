from fastapi import APIRouter, Request

from app.core.process_metrics import get_process_metrics

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", summary="Health check")
async def health_check(request: Request) -> dict:
    runtime = getattr(request.app.state, "runtime", None)
    ws_manager = getattr(request.app.state, "ws_manager", None)
    dispatcher = getattr(request.app.state, "event_dispatcher", None)
    cleanup_service = getattr(request.app.state, "cleanup_service", None)
    payload = {"status": "ok"}
    payload["system"] = get_process_metrics().__dict__
    if runtime is not None:
        payload["runtime"] = runtime.state().__dict__
    if ws_manager is not None:
        payload["websocket"] = (await ws_manager.get_stats()).__dict__
    if dispatcher is not None:
        payload["event_dispatcher_running"] = dispatcher.is_running()
    if cleanup_service is not None:
        payload["cleanup"] = cleanup_service.state().__dict__
    return payload
