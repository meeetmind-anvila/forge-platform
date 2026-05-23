"""
Forge CI Engine — FastAPI application.
Endpoints: /runs, /runs/{id}, /runs/{id}/logs, /runs/{id}/lockfile
"""
import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiofiles
from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse

from parser import parse_pipeline, PipelineError
from scheduler import RunManager
from logs import LogStreamer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("engine")

# Make registry modules importable
import sys as _sys
_sys.path.insert(0, "/registry")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.getenv("FORGE_ENGINE_DATA_DIR", "/data/engine"))
RUNS_DIR = DATA_DIR / "runs"
AUTH_DB = Path(os.getenv("FORGE_AUTH_DB", "/data/registry/auth.db"))
REGISTRY_URL = os.getenv("FORGE_REGISTRY_URL", "http://registry:8001")
MAX_CONCURRENCY = int(os.getenv("FORGE_MAX_CONCURRENCY", "4"))


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    app.state.run_manager = RunManager(
        runs_dir=str(RUNS_DIR),
        registry_url=REGISTRY_URL,
        auth_db=str(AUTH_DB),
        max_concurrency=MAX_CONCURRENCY,
    )
    await app.state.run_manager.start()
    yield
    await app.state.run_manager.stop()


app = FastAPI(title="Forge Engine", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Auth dependency (shares auth DB with registry)
# ---------------------------------------------------------------------------
async def require_auth(authorization: str = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    token = authorization.removeprefix("Bearer ").strip()
    from auth import get_token_identity
    identity = get_token_identity(str(AUTH_DB), token)
    if identity is None:
        raise HTTPException(status_code=403, detail="Invalid token")
    return identity


# ---------------------------------------------------------------------------
# Submit pipeline run
# ---------------------------------------------------------------------------
@app.post("/runs", status_code=201)
async def submit_run(
    request: Request,
    pipeline: UploadFile = File(...),
    identity: str = Depends(require_auth),
):
    content = await pipeline.read()
    try:
        parsed = parse_pipeline(content.decode())
    except PipelineError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    run_id = str(uuid.uuid4())
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True)

    # Save raw pipeline
    (run_dir / "pipeline.yaml").write_bytes(content)

    # Enqueue
    manager: RunManager = request.app.state.run_manager
    await manager.enqueue(run_id, parsed, identity)

    return {"run_id": run_id}


# ---------------------------------------------------------------------------
# Get run status
# ---------------------------------------------------------------------------
@app.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    manager: RunManager = request.app.state.run_manager
    state = manager.get_run_state(run_id)
    if state is None:
        # Check disk
        run_dir = RUNS_DIR / run_id
        state_file = run_dir / "state.json"
        if not state_file.exists():
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        state = json.loads(state_file.read_text())

    return {
        "run_id": run_id,
        "status": state.get("status", "unknown"),
        "jobs": state.get("jobs", {}),
        "lockfile_url": f"/runs/{run_id}/lockfile",
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
    }


# ---------------------------------------------------------------------------
# Get lockfile
# ---------------------------------------------------------------------------
@app.get("/runs/{run_id}/lockfile")
async def get_lockfile(run_id: str):
    run_dir = RUNS_DIR / run_id
    lf_path = run_dir / "lockfile.json"
    if not lf_path.exists():
        raise HTTPException(status_code=404, detail="Lockfile not yet produced")
    return json.loads(lf_path.read_text())


# ---------------------------------------------------------------------------
# Log streaming (SSE)
# ---------------------------------------------------------------------------
@app.get("/runs/{run_id}/logs")
async def stream_logs(
    run_id: str,
    request: Request,
    follow: bool = False,
):
    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    manager: RunManager = request.app.state.run_manager
    last_event_id = request.headers.get("last-event-id")
    streamer = LogStreamer(str(run_dir), run_id, manager)

    async def event_generator() -> AsyncIterator[str]:
        async for event in streamer.stream(follow=follow, last_event_id=last_event_id):
            yield event

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "engine"}


# ---------------------------------------------------------------------------
# Admin: token management
# ---------------------------------------------------------------------------
from pydantic import BaseModel as _BaseModel

class _TokenCreateBody(_BaseModel):
    name: str

@app.post("/admin/tokens")
async def admin_create_token(
    body: _TokenCreateBody,
    identity: str = Depends(require_auth),
):
    """Create a new API token (requires existing valid token)."""
    from auth import create_token
    try:
        token = create_token(str(AUTH_DB), body.name)
        return {"name": body.name, "token": token}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/admin/tokens")
async def admin_list_tokens(identity: str = Depends(require_auth)):
    from auth import list_tokens
    return {"tokens": list_tokens(str(AUTH_DB))}


@app.delete("/admin/tokens/{name}")
async def admin_revoke_token(name: str, identity: str = Depends(require_auth)):
    from auth import revoke_token
    ok = revoke_token(str(AUTH_DB), name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Token '{name}' not found")
    return {"revoked": name}