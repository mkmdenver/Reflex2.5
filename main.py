from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cockpit.routers import api as api_router
from cockpit.services.cockpit_state import CockpitState

CORS_ALLOW_ORIGINS = ["*"]  # tighten later

@asynccontextmanager
async def lifespan(app: FastAPI):
    # create and attach shared state for routers to read via request.app.state
    app.state.cockpit_state = CockpitState()
    await app.state.cockpit_state.start()
    try:
        yield
    finally:
        await app.state.cockpit_state.stop()
        app.state.cockpit_state = None  # type: ignore

app = FastAPI(title="Reflex Cockpit", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API
app.include_router(api_router.router, prefix="/v1/cockpit", tags=["cockpit"])

# ---- Convenience routes ----
@app.get("/", tags=["meta"])
def root():
    return {
        "service": "Reflex Cockpit",
        "ok": True,
        "endpoints": {
            "health": "/health",
            "metrics": "/v1/cockpit/metrics",
            "docs": "/docs",
        },
    }

@app.get("/health", tags=["meta"])
def health():
    return {"ok": True}
