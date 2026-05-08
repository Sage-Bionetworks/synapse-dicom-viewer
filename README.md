# synapse-dicom-viewer

Lightweight DICOMweb shim for Synapse-hosted radiology data, with a CDN-loaded
CornerstoneJS demo viewer. Sister project to
[synapse-avivator](https://github.com/Sage-Bionetworks/synapse-avivator).

**Live demo (Cloud Run, us-east1):**
[`https://synapse-dicom-viewer-350460119293.us-east1.run.app/`](https://synapse-dicom-viewer-350460119293.us-east1.run.app/)
— bring a Synapse Personal Access Token and a folder ID containing `.dcm` files.

## What this does

A thin FastAPI service that:

1. **Enumerates** a Synapse folder of `.dcm` entities (or a local directory).
2. **Range-reads the first 64 KB** of each file and parses the DICOM header
   with pydicom (`stop_before_pixels=True`) — no pre-generation step.
3. Builds an in-memory `study → series → instance` index.
4. Exposes **DICOMweb** (QIDO-RS + WADO-RS) endpoints under `/dicom-web/`.
5. Serves a small CornerstoneJS-based viewer that renders the study from the
   browser, scrolling slices with auto window/level and a metadata side panel.

The shim handles presigned-URL refresh transparently (Synapse URLs expire
after 15 min; the `SynapseRefreshingUrl` from `synapse-avivator` is reused
verbatim with a 60 s buffer + 403-retry).

## Quick start

### Local mode (CLI auth)

```bash
uv venv
uv pip install -e ".[dev]"

# Local directory of .dcm files (no Synapse needed)
uv run synapse-dicom-viewer --local-folder /path/to/dcm/folder --verbose

# Synapse folder (uses ~/.synapseConfig or SYNAPSE_AUTH_TOKEN)
uv run synapse-dicom-viewer synXXXXXXXX --verbose
```

The browser opens at `http://localhost:8000/`. The landing page polls
`/api/state` and auto-redirects to the viewer once the index is built.

### Hosted mode (browser PAT)

```bash
HOSTED=1 uv run uvicorn synapse_dicom_viewer.proxy:app --host 0.0.0.0 --port 8080
```

In hosted mode the landing page asks for a Synapse PAT, which is sent via
`X-Synapse-Token` on every request. The shim stores the token Fernet-encrypted
in memory and decrypts it only at the moment of a presigned-URL refresh.

### Cloud Run

```bash
gcloud run deploy synapse-dicom-viewer \
  --source . \
  --region us-east1 \
  --allow-unauthenticated \
  --memory 2Gi --cpu 2 \
  --set-env-vars HOSTED=1
```

`Dockerfile` is set up for source-deploy. `us-east1` is recommended — closest
to the AWS `us-east-1` region where Synapse stores files, so the index build
and per-slice fetches are 3× faster than running off a residential connection.

## Endpoints

| | |
|---|---|
| `GET /` | Landing page — PAT input (hosted) + folder picker |
| `GET /viewer-mini.html?StudyInstanceUIDs=…` | CornerstoneJS demo viewer |
| `GET /api/state` | JSON: current source, studies, cache stats |
| `POST /api/index?folder_id=synXXX` | Trigger index build for a Synapse folder |
| `POST /api/clear` | Drop the in-memory index, instance cache, and stored PAT |
| `GET /auth/me` · `POST /auth/validate` | Auth state + PAT validation |
| `GET /dicom-web/studies` | QIDO-RS study list |
| `GET /dicom-web/studies/{}/series` | QIDO-RS series list |
| `GET /dicom-web/studies/{}/series/{}/instances` | QIDO-RS instance list |
| `GET /dicom-web/studies/{}/series/{}/metadata` | Series metadata |
| `GET /dicom-web/studies/{}/series/{}/instances/{}` | WADO-RS retrieve (multipart or raw `application/dicom` based on `Accept`) |
| `GET /viewer-config.js` | Runtime config for an optional bundled OHIF v3 |

## Architecture

```
Browser  ──QIDO/WADO──▶  FastAPI shim  ──Synapse REST──▶  S3 byte-range
                          │
                          ├─ Study index (in-memory)
                          ├─ Header cache (parsed from first 64 KB)
                          ├─ Instance bytes cache (LRU, 1 GB budget)
                          └─ SynapseRefreshingUrl (15 min expiry, 60 s buffer, 403 retry)
```

Single Python process, single uvicorn worker — in-memory caches are not shared
across workers. In hosted mode, all data routes require `X-Synapse-Token` so a
warm Cloud Run instance can't leak a previous user's indexed studies.

## Viewer features

The bundled `viewer-mini.html` is a ~500 KB CDN-loaded demo:

- Slice scroll (mouse wheel) and slider
- Drag = window/level, shift+drag = zoom, middle-drag = pan
- **Auto W/L** — samples 7 slices, sets W/L from 1st–99th percentile, locks
  across the stack so brightness doesn't flicker on scroll
- Metadata side panel (Patient · Study · Series · Acquisition · Image · Source)
- Background prefetch with a 256 MB cap (closest-to-current slices first;
  caps automatically for very large series)
- Multi-series picker

## Tested vs. supported

- **Tested**: Implicit/Explicit VR Little Endian, single-frame radiology
  (CT/MR/PET), flat folders, 416-slice MR brain T1.
- **Not tested in the prototype**: compressed Transfer Syntaxes (JPEG 2000 etc.),
  enhanced multi-frame MR, recursive folders, DICOM-WSI pathology.

The header range-read uses a recovery ladder (64 KB → 256 KB → 1 MB → full
file) so unusually large headers (private vendor blobs, ICC profiles) won't
silently fail.

## Optional: full OHIF v3 bundle

The default viewer is sufficient for 2D radiology review. For the full
clinical-grade UI (study browser, hanging protocols, MPR, measurements,
3D volume rendering), bundle OHIF v3:

```bash
# Option A — Docker:
docker pull ohif/app:latest
docker create --name ohif-tmp ohif/app:latest
docker cp ohif-tmp:/usr/share/nginx/html src/synapse_dicom_viewer/static/viewer
docker rm ohif-tmp

# Option B — yarn build from source (~10 min):
git clone --depth 1 -b v3.9.0 https://github.com/OHIF/Viewers /tmp/ohif
cd /tmp/ohif && yarn install && PUBLIC_URL=/viewer/ yarn build
cp -r platform/app/dist/* path/to/synapse-dicom-viewer/src/synapse_dicom_viewer/static/viewer/
```

The shim's `/viewer-config.js` is already configured for OHIF; once the bundle
is in place, browse to `/viewer/?configUrl=/viewer-config.js&StudyInstanceUIDs=<uid>`.

## Tests

```bash
uv run pytest
```

Tests use the local sample folder at `/Users/ataylor/Downloads/eg_brain_t1`
and skip if absent. Adapt the path in `tests/conftest.py` (or set up your own
sample) to run them locally.

## Uploading sample data

```bash
uv run python scripts/upload_sample.py \
  --local /path/to/dcm/folder \
  --project "synapse-dicom-viewer-test" \
  --folder my_sample
```

Idempotent — re-running skips files that already exist.

## License

Apache 2.0. See `LICENSE`.
