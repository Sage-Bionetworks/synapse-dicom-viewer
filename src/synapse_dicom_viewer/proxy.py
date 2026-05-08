"""FastAPI app: serves OHIF, exposes DICOMweb endpoints, manages the index lifecycle."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from synapse_dicom_viewer.dicom_index import (
    DicomSource, LocalDicomSource, StudyTree, SynapseDicomSource, build_index,
)
from synapse_dicom_viewer.dicomweb import make_router
from synapse_dicom_viewer.instance_cache import InstanceCache

log = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_VIEWER_DIR = _STATIC_DIR / "viewer"

# Per-process Fernet key — encrypts hosted-mode PATs at rest.
# Generated fresh on every process start so old encrypted tokens become unreadable
# if the process restarts (and the user re-enters their PAT).
_fernet = Fernet(Fernet.generate_key())


@dataclass
class AppState:
    source: DicomSource | None = None
    tree: StudyTree | None = None
    instance_cache: InstanceCache = field(default_factory=InstanceCache)
    syn = None                           # synapseclient.Synapse (local mode), else None
    hosted_mode: bool = False             # True when users supply PAT via header
    hosted_token_enc: bytes | None = None  # Fernet-encrypted PAT for hosted mode
    pending_folder_id: str | None = None   # set by CLI; built in lifespan startup


_state = AppState()


def get_state() -> AppState:
    return _state


def set_synapse_client(syn) -> None:
    _state.syn = syn


def set_pending_folder(folder_id: str) -> None:
    _state.pending_folder_id = folder_id


def set_local_folder(path: str) -> None:
    """Switch the source to a local directory (no Synapse coupling)."""
    _state.source = LocalDicomSource(path)
    _state.tree = None


def set_hosted_mode(enabled: bool) -> None:
    """Enable hosted mode — users supply their Synapse PAT via X-Synapse-Token header."""
    _state.hosted_mode = enabled


def _hosted_token_factory() -> str:
    """Decrypt the stored PAT — only invoked at the moment of a URL refresh."""
    if _state.hosted_token_enc is None:
        raise RuntimeError("No Synapse token available (hosted mode requires X-Synapse-Token)")
    return _fernet.decrypt(_state.hosted_token_enc).decode()


def _store_hosted_token(token: str) -> None:
    _state.hosted_token_enc = _fernet.encrypt(token.encode())


def set_verbose(verbose: bool) -> None:
    if verbose:
        Path("logs").mkdir(exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        log_file = f"logs/session-{ts}.log"
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s.%(msecs)03d  %(name)s  %(message)s",
            datefmt="%H:%M:%S",
            handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
            force=True,
        )
        log.info("session %s  log: %s", ts, log_file)
    else:
        logging.basicConfig(level=logging.WARNING, force=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-enable hosted mode when HOSTED=1 (for container deploys)
    if os.environ.get("HOSTED", "").strip() in ("1", "true", "yes"):
        set_hosted_mode(True)
        log.info("hosted mode enabled via HOSTED env var")
    # Eager-build the index if the CLI registered a source/folder (local mode only)
    if _state.source is None and _state.pending_folder_id and _state.syn is not None:
        _state.source = SynapseDicomSource(_state.pending_folder_id, syn=_state.syn)
    if _state.source is not None and _state.tree is None:
        log.info("Building index for %s ...", _state.source.folder_id)
        _state.tree = await build_index(_state.source)
        log.info("Index ready: %d studies", len(_state.tree.studies))
    try:
        yield
    finally:
        if _state.source is not None:
            await _state.source.aclose()


app = FastAPI(lifespan=lifespan, title="synapse-dicom-viewer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "POST", "OPTIONS"],
    allow_headers=["Range", "Accept", "Content-Type", "Authorization", "X-Synapse-Token"],
    expose_headers=["Content-Range", "Content-Length", "Accept-Ranges", "Content-Type"],
)


_HOSTED_PUBLIC_PATHS = (
    "/", "/auth/me", "/auth/validate", "/api/clear", "/api/state",
)


@app.middleware("http")
async def hosted_auth_middleware(request: Request, call_next):
    """In hosted mode: capture the PAT, and gate data routes behind it.

    Public paths (landing page, /auth/me, /auth/validate, /api/clear) work
    without a token. All other endpoints in hosted mode require the
    X-Synapse-Token header so a warm Cloud Run instance can't leak a
    previous user's indexed studies or cached bytes."""
    if _state.hosted_mode:
        token = request.headers.get("x-synapse-token")
        if token:
            _store_hosted_token(token)
        path = request.url.path
        is_public = (
            path in _HOSTED_PUBLIC_PATHS
            or path.startswith(("/static/", "/viewer-mini.html", "/favicon"))
        )
        if not is_public and not token and request.method != "OPTIONS":
            return JSONResponse(
                {"detail": "Provide Synapse PAT via X-Synapse-Token header"},
                status_code=401,
            )
    return await call_next(request)

app.include_router(make_router(get_state))


# ----- static + landing page -------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def landing() -> Response:
    index_html = _STATIC_DIR / "index.html"
    if index_html.exists():
        return FileResponse(index_html)
    # Fallback if landing page hasn't been written yet
    return HTMLResponse("<h1>synapse-dicom-viewer</h1><p>Static landing page missing.</p>")


@app.get("/{filename:path}.html")
async def serve_static_html(filename: str) -> Response:
    """Serve any *.html file under static/ (e.g. /viewer-mini.html)."""
    asset = _STATIC_DIR / f"{filename}.html"
    # Disallow path traversal outside static/
    try:
        asset.resolve().relative_to(_STATIC_DIR.resolve())
    except ValueError:
        raise HTTPException(404)
    if not asset.is_file():
        raise HTTPException(404)
    return FileResponse(asset)


@app.get("/viewer-config.js")
async def viewer_config() -> Response:
    """OHIF runtime config — sets window.config so we don't need to rebuild OHIF per deploy."""
    cfg = {
        "routerBasename": "/viewer",
        "extensions": [],
        "modes": [],
        "showStudyList": True,
        "maxNumberOfWebWorkers": 3,
        "omitQuotationForMultipartRequest": True,
        "showLoadingIndicator": True,
        "useSharedArrayBuffer": "AUTO",
        "dataSources": [{
            "namespace": "@ohif/extension-default.dataSourcesModule.dicomweb",
            "sourceName": "synapse",
            "configuration": {
                "friendlyName": "Synapse",
                "name": "synapse",
                "wadoUriRoot": "/dicom-web",
                "qidoRoot": "/dicom-web",
                "wadoRoot": "/dicom-web",
                "qidoSupportsIncludeField": False,
                "supportsReject": False,
                "supportsStow": False,
                "imageRendering": "wadors",
                "thumbnailRendering": "wadors",
                "enableStudyLazyLoad": True,
                "supportsFuzzyMatching": False,
                "supportsWildcard": False,
                "omitQuotationForMultipartRequest": True,
                "singlepart": "bulkdata,video",
            },
        }],
        "defaultDataSourceName": "synapse",
    }
    body = "window.config = " + json.dumps(cfg, indent=2) + ";\n"
    return PlainTextResponse(body, media_type="application/javascript")


@app.get("/api/state")
async def api_state(request: Request) -> JSONResponse:
    """Landing-page helper: report current source, study UIDs, etc.

    In hosted mode, only requests carrying X-Synapse-Token see state — this
    prevents a visitor from inheriting a previous user's indexed study just
    because the Cloud Run instance is still warm.
    """
    if _state.hosted_mode and not request.headers.get("x-synapse-token"):
        return JSONResponse({"source": None, "studies": []})
    if _state.source is None:
        return JSONResponse({"source": None, "studies": []})
    studies = []
    if _state.tree is not None:
        for sid, series in _state.tree.studies.items():
            studies.append({
                "study_instance_uid": sid,
                "n_series": len(series),
                "n_instances": sum(len(i) for i in series.values()),
                "modality": next(iter(series.values()))[0].modality,
                "patient_name": next(iter(series.values()))[0].patient_name,
                "study_date": next(iter(series.values()))[0].study_date,
            })
    return JSONResponse({
        "source": _state.source.folder_id,
        "indexing": _state.tree is None,
        "studies": studies,
        "cache": _state.instance_cache.stats(),
    })


@app.post("/api/index")
async def api_index(folder_id: str, request: Request) -> JSONResponse:
    """Trigger an index build for a Synapse folder ID.
    In local mode the CLI-injected synapseclient is used; in hosted mode
    the X-Synapse-Token header is required."""
    if _state.hosted_mode:
        if request.headers.get("x-synapse-token") is None and _state.hosted_token_enc is None:
            raise HTTPException(401, "Provide Synapse PAT via X-Synapse-Token header")
        _state.source = SynapseDicomSource(folder_id, token_factory=_hosted_token_factory)
    else:
        if _state.syn is None:
            raise HTTPException(400, "Synapse not authenticated. Restart with a token or enable hosted mode.")
        _state.source = SynapseDicomSource(folder_id, syn=_state.syn)
    _state.tree = await build_index(_state.source)
    return JSONResponse({
        "folder_id": folder_id,
        "studies": list(_state.tree.studies.keys()),
    })


@app.post("/api/clear")
async def api_clear() -> JSONResponse:
    """Drop the in-memory study index, instance bytes cache, and (in hosted mode)
    the stored PAT. Used by the landing page's 'Clear' button to start fresh."""
    if _state.source is not None:
        try:
            await _state.source.aclose()
        except Exception:
            pass
    _state.source = None
    _state.tree = None
    _state.instance_cache = InstanceCache()
    if _state.hosted_mode:
        _state.hosted_token_enc = None
    return JSONResponse({"cleared": True})


@app.get("/auth/me")
async def auth_me(request: Request) -> JSONResponse:
    """Tell the landing page whether to render a PAT input.

    `has_token` reflects THIS REQUEST's auth state, not the server's stored
    encrypted token (which may belong to a different user)."""
    if _state.hosted_mode:
        return JSONResponse({
            "mode": "hosted",
            "has_token": bool(request.headers.get("x-synapse-token")),
        })
    user_id = None
    if _state.syn is not None:
        try:
            user_id = _state.syn.credentials.owner_id
        except Exception:
            pass
    return JSONResponse({"mode": "local", "user_id": user_id})


@app.post("/auth/validate")
async def auth_validate(request: Request) -> JSONResponse:
    """Validate a Synapse PAT server-side before stashing it."""
    token = request.headers.get("x-synapse-token")
    if not token:
        raise HTTPException(400, "Missing X-Synapse-Token header")
    async with httpx.AsyncClient(timeout=10.0) as h:
        r = await h.get(
            "https://repo-prod.prod.sagebase.org/repo/v1/userProfile",
            headers={"Authorization": f"Bearer {token}"},
        )
    if r.status_code != 200:
        raise HTTPException(401, f"Synapse rejected token ({r.status_code})")
    profile = r.json()
    return JSONResponse({
        "user_id": profile.get("ownerId"),
        "username": profile.get("userName"),
    })


# OHIF SPA routing — serve viewer/index.html for any path under /viewer/
@app.get("/viewer")
@app.get("/viewer/")
async def viewer_root() -> Response:
    return _serve_viewer_index()


@app.get("/viewer/{full_path:path}")
async def viewer_assets(full_path: str) -> Response:
    if not _VIEWER_DIR.exists():
        raise HTTPException(404, "OHIF bundle not installed; see README")
    asset = _VIEWER_DIR / full_path
    if asset.is_file():
        return FileResponse(asset)
    # SPA fallback: any unmatched route renders the OHIF shell
    return _serve_viewer_index()


def _serve_viewer_index() -> Response:
    index = _VIEWER_DIR / "index.html"
    if not index.exists():
        return HTMLResponse(
            "<h1>OHIF bundle not installed</h1>"
            "<p>Run the build script in scripts/build-ohif.sh and copy dist/ to "
            f"<code>{_VIEWER_DIR}</code>.</p>",
            status_code=503,
        )
    return FileResponse(index)
