from fastapi import APIRouter, Request

router = APIRouter()

@router.get("/metrics")
async def cockpit_metrics(request: Request):
    state = getattr(request.app.state, "cockpit_state", None)
    if state is None:
        return {
            "ok": False,
            "reason": "state-not-initialized",
            "hint": "Ensure app uses cockpit.main:app (with lifespan) when starting.",
        }
    snap = await state.snapshot()
    return {"ok": True, "snapshot": snap}
