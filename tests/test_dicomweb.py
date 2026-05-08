"""DICOMweb endpoint shape + multipart envelope correctness."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from synapse_dicom_viewer.proxy import app, set_local_folder

SAMPLE_FOLDER = Path("/Users/ataylor/Downloads/eg_brain_t1")
pytestmark = pytest.mark.skipif(
    not SAMPLE_FOLDER.is_dir(),
    reason=f"sample folder not present: {SAMPLE_FOLDER}",
)


@pytest.fixture
def client():
    set_local_folder(str(SAMPLE_FOLDER))
    with TestClient(app) as c:
        yield c


def test_qido_studies(client):
    r = client.get("/dicom-web/studies")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/dicom+json")
    studies = r.json()
    assert len(studies) >= 1
    s = studies[0]
    assert "0020000D" in s
    assert s["0020000D"]["vr"] == "UI"
    assert s["00100010"]["vr"] == "PN"
    # PN values must be objects, not strings
    pn = s["00100010"].get("Value", [{}])[0]
    assert isinstance(pn, dict) and "Alphabetic" in pn


def test_series_metadata_has_pixel_geometry(client):
    suid = client.get("/dicom-web/studies").json()[0]["0020000D"]["Value"][0]
    series_uid = client.get(f"/dicom-web/studies/{suid}/series").json()[0]["0020000E"]["Value"][0]
    meta = client.get(f"/dicom-web/studies/{suid}/series/{series_uid}/metadata").json()
    assert meta
    sample = meta[0]
    for required_tag in ("00280010", "00280011", "00280100", "00080018", "00020010"):
        assert required_tag in sample, f"missing {required_tag}"


def test_wado_multipart_envelope(client):
    suid = client.get("/dicom-web/studies").json()[0]["0020000D"]["Value"][0]
    series_uid = client.get(f"/dicom-web/studies/{suid}/series").json()[0]["0020000E"]["Value"][0]
    sop = client.get(f"/dicom-web/studies/{suid}/series/{series_uid}/instances").json()[0]["00080018"]["Value"][0]

    r = client.get(
        f"/dicom-web/studies/{suid}/series/{series_uid}/instances/{sop}",
        headers={"Accept": 'multipart/related; type="application/dicom"'},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("multipart/related")
    body = r.content
    assert body.startswith(b"--synapse-dicom-viewer-boundary")
    assert body.rstrip().endswith(b"--synapse-dicom-viewer-boundary--")
    assert b"Content-Type: application/dicom" in body


def test_wado_raw_bytes_for_wadouri_clients(client):
    """cornerstone-wado-image-loader's wadouri scheme expects raw application/dicom."""
    suid = client.get("/dicom-web/studies").json()[0]["0020000D"]["Value"][0]
    series_uid = client.get(f"/dicom-web/studies/{suid}/series").json()[0]["0020000E"]["Value"][0]
    sop = client.get(f"/dicom-web/studies/{suid}/series/{series_uid}/instances").json()[0]["00080018"]["Value"][0]

    r = client.get(
        f"/dicom-web/studies/{suid}/series/{series_uid}/instances/{sop}",
        headers={"Accept": "*/*"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/dicom"
    # DICM magic at offset 128 (after 128-byte preamble)
    assert r.content[128:132] == b"DICM"
