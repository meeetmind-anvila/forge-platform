"""
Forge Artifact Registry — FastAPI application.
Endpoints: /artifacts/{name}/{version}, /artifacts/{name}
"""
import asyncio
import hashlib
import io
import logging
import os
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from auth import get_token_identity, init_auth_db
from metadata import (
    ArtifactMeta,
    add_artifact,
    get_artifact,
    get_artifact_versions,
    init_metadata_db,
)
from resolver import DependencyResolver
from storage import BlobStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("registry")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(os.getenv("FORGE_DATA_DIR", "/data/registry"))
BLOB_DIR = BASE_DIR / "blobs"
DB_PATH = BASE_DIR / "registry.db"
AUTH_DB_PATH = BASE_DIR / "auth.db"
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))  # 500MB
SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    BLOB_DIR.mkdir(parents=True, exist_ok=True)
    init_metadata_db(str(DB_PATH))
    init_auth_db(str(AUTH_DB_PATH))
    app.state.store = BlobStore(str(BLOB_DIR))
    app.state.resolver = DependencyResolver(str(DB_PATH))
    yield


app = FastAPI(title="Forge Registry", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------
async def require_auth(authorization: str = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    token = authorization.removeprefix("Bearer ").strip()
    identity = get_token_identity(str(AUTH_DB_PATH), token)
    if identity is None:
        raise HTTPException(status_code=403, detail="Invalid or revoked token")
    return identity


# ---------------------------------------------------------------------------
# Upload artifact
# ---------------------------------------------------------------------------
@app.post("/artifacts/{name}/{version}", status_code=201)
async def upload_artifact(
    name: str,
    version: str,
    request: Request,
    file: UploadFile = File(...),
    checksum: str = Form(...),
    deps: str = Form(None),          # JSON-encoded list of {name, version} dicts
    identity: str = Depends(require_auth),
):
    """
    Upload a build artifact.
    - checksum must be "sha256:<hex>"
    - If (name, version) already exists → 409
    - If checksum mismatch → 400
    """
    import json

    # Validate semver
    if not SEMVER_RE.match(version):
        raise HTTPException(status_code=400, detail=f"Non-semver version: {version}")

    # Validate checksum format
    if not checksum.startswith("sha256:"):
        raise HTTPException(status_code=400, detail="checksum must be sha256:<hex>")
    declared_sha = checksum[7:].strip()

    # Check immutability
    if get_artifact(str(DB_PATH), name, version) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{name}@{version} already exists (immutable)",
        )

    # Stream to temp file, compute sha256 on the fly
    store: BlobStore = request.app.state.store
    tmp_path = store.tmp_path()
    computed_sha = hashlib.sha256()
    size = 0

    try:
        with open(tmp_path, "wb") as fout:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Artifact too large")
                computed_sha.update(chunk)
                fout.write(chunk)
    except HTTPException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    except Exception as exc:
        Path(tmp_path).unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(exc))

    hex_sha = computed_sha.hexdigest()

    if hex_sha != declared_sha:
        Path(tmp_path).unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"Checksum mismatch: declared={declared_sha} computed={hex_sha}",
        )

    # Commit blob
    blob_path = store.commit(tmp_path, hex_sha)

    # Parse deps
    parsed_deps = []
    if deps:
        try:
            parsed_deps = json.loads(deps)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="deps must be valid JSON")

    # Insert metadata (UNIQUE constraint catches race)
    try:
        add_artifact(str(DB_PATH), name, version, hex_sha, size, identity, parsed_deps)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(
                status_code=409,
                detail=f"{name}@{version} already exists (immutable)",
            )
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info("Published %s@%s sha256=%s by %s", name, version, hex_sha, identity)
    return {"name": name, "version": version, "sha256": hex_sha, "size": size}


# ---------------------------------------------------------------------------
# Download artifact
# ---------------------------------------------------------------------------
@app.get("/artifacts/{name}/{version}")
async def download_artifact(name: str, version: str, request: Request):
    meta = get_artifact(str(DB_PATH), name, version)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"{name}@{version} not found")

    store: BlobStore = request.app.state.store
    blob_path = store.path(meta["sha256"])
    if not blob_path.exists():
        raise HTTPException(status_code=500, detail="Blob missing from store")

    return FileResponse(
        str(blob_path),
        media_type="application/octet-stream",
        filename=f"{name}-{version}.tar.gz",
        headers={"X-Artifact-SHA256": meta["sha256"]},
    )


# ---------------------------------------------------------------------------
# Artifact metadata
# ---------------------------------------------------------------------------
@app.get("/artifacts/{name}/{version}/meta")
async def artifact_meta(name: str, version: str):
    meta = get_artifact(str(DB_PATH), name, version)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"{name}@{version} not found")
    return meta


# ---------------------------------------------------------------------------
# List versions
# ---------------------------------------------------------------------------
@app.get("/artifacts/{name}")
async def list_versions(name: str):
    versions = get_artifact_versions(str(DB_PATH), name)
    return {"name": name, "versions": versions}


# ---------------------------------------------------------------------------
# Resolve (used by engine before build)
# ---------------------------------------------------------------------------
@app.post("/resolve")
async def resolve_deps(payload: dict):
    """
    payload: {"dependencies": [{"name": "x", "version": "^1.0.0"}, ...]}
    Returns lockfile or error.
    """
    resolver: DependencyResolver = app.state.resolver
    deps = payload.get("dependencies", [])
    try:
        lockfile = resolver.resolve(deps)
        return {"status": "ok", "lockfile": lockfile}
    except Exception as exc:
        return JSONResponse(status_code=409, content={"status": "error", "detail": str(exc)})


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "registry"}
