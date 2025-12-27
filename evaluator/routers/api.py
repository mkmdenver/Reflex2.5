# evaluator/routers/api.py
from __future__ import annotations
from fastapi import APIRouter
from evaluator.state import EVAL_STATE

router = APIRouter()

@router.get("/health")
def health():
    return {"ok": True, "service": "Evaluator"}

@router.get("/v1/metrics")
def metrics():
    return EVAL_STATE.snapshot_metrics()

@router.get("/v1/positions")
def positions():
    return {"ok": True, "items": EVAL_STATE.snapshot_positions()}

@router.get("/v1/orders/active")
def active_orders():
    return {"ok": True, "items": EVAL_STATE.snapshot_orders()}
