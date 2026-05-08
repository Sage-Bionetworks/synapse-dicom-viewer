"""DICOMweb endpoints (QIDO-RS + WADO-RS) backed by an in-memory StudyTree.

QIDO responses use the DICOM JSON model (PS3.18 Annex F): 8-char hex tag keys,
{vr, Value} shape. WADO-RS retrieve responses are wrapped in a single-part
multipart/related envelope, which is what OHIF's dicomweb-client expects.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from fastapi import APIRouter, HTTPException, Request, Response

from synapse_dicom_viewer.dicom_index import InstanceMeta, StudyTree

log = logging.getLogger(__name__)

MULTIPART_BOUNDARY = "synapse-dicom-viewer-boundary"
DICOM_JSON = "application/dicom+json"


def make_router(get_state) -> APIRouter:
    """Build the /dicom-web router. `get_state()` returns the current AppState."""
    router = APIRouter(prefix="/dicom-web", tags=["dicomweb"])

    @router.get("/studies")
    async def studies_qido() -> Response:
        tree = _require_tree(get_state)
        items = [_study_json(uid, series) for uid, series in tree.studies.items()]
        return _json_response(items)

    @router.get("/studies/{study_uid}/series")
    async def series_qido(study_uid: str) -> Response:
        tree = _require_tree(get_state)
        study = tree.studies.get(study_uid)
        if study is None:
            raise HTTPException(404, f"Study not found: {study_uid}")
        items = [_series_json(study_uid, suid, instances) for suid, instances in study.items()]
        return _json_response(items)

    @router.get("/studies/{study_uid}/series/{series_uid}/instances")
    async def instances_qido(study_uid: str, series_uid: str) -> Response:
        instances = _require_series(get_state, study_uid, series_uid)
        items = [_instance_qido_json(m) for m in instances]
        return _json_response(items)

    @router.get("/studies/{study_uid}/series/{series_uid}/metadata")
    async def series_metadata(study_uid: str, series_uid: str) -> Response:
        """Full per-instance metadata array (geometry + W/L). Cornerstone needs this before render."""
        instances = _require_series(get_state, study_uid, series_uid)
        items = [_instance_metadata_json(m) for m in instances]
        return _json_response(items)

    @router.get("/studies/{study_uid}/metadata")
    async def study_metadata(study_uid: str) -> Response:
        tree = _require_tree(get_state)
        study = tree.studies.get(study_uid)
        if study is None:
            raise HTTPException(404, f"Study not found: {study_uid}")
        items = [_instance_metadata_json(m) for instances in study.values() for m in instances]
        return _json_response(items)

    @router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}")
    async def wado_instance(study_uid: str, series_uid: str, sop_uid: str, request: Request) -> Response:
        """WADO-RS retrieve. Returns multipart/related when the client asks for it
        (OHIF / cornerstone-wado-image-loader 'wadors' scheme), or raw application/dicom
        for the legacy 'wadouri' scheme used by simpler viewers."""
        state = get_state()
        tree = _require_tree(get_state)
        if study_uid not in tree.studies or series_uid not in tree.studies[study_uid]:
            raise HTTPException(404, "Series not found")
        entity_id = tree.sop_to_entity.get(sop_uid)
        if entity_id is None:
            raise HTTPException(404, f"Instance not found: {sop_uid}")

        async def fetch() -> bytes:
            return await state.source.fetch_bytes(entity_id, length=None)

        data = await state.instance_cache.get_or_fetch(entity_id, fetch)

        accept = request.headers.get("accept", "")
        if "multipart/related" in accept or accept == "":
            body = _multipart_wrap(
                data, content_type="application/dicom",
                content_location=str(request.url),
            )
            return Response(
                content=body,
                media_type=f'multipart/related; type="application/dicom"; boundary={MULTIPART_BOUNDARY}',
            )
        # WADO-URI / single-part fallback
        return Response(content=data, media_type="application/dicom")

    return router


def _require_tree(get_state) -> StudyTree:
    state = get_state()
    if state.tree is None:
        raise HTTPException(503, "Index not yet built. Call POST /index/{folder_id} first.")
    return state.tree


def _require_series(get_state, study_uid: str, series_uid: str) -> list[InstanceMeta]:
    tree = _require_tree(get_state)
    study = tree.studies.get(study_uid)
    if study is None:
        raise HTTPException(404, f"Study not found: {study_uid}")
    instances = study.get(series_uid)
    if instances is None:
        raise HTTPException(404, f"Series not found: {series_uid}")
    return instances


def _json_response(items: list[dict]) -> Response:
    import json
    return Response(content=json.dumps(items), media_type=DICOM_JSON)


# ----- DICOM JSON tag helpers ------------------------------------------------

def _t(vr: str, value: Any) -> dict:
    """Build a DICOM JSON tag entry, omitting Value when empty/None."""
    if value is None:
        return {"vr": vr}
    if isinstance(value, list):
        if not value:
            return {"vr": vr}
        return {"vr": vr, "Value": value}
    return {"vr": vr, "Value": [value]}


def _pn(name: str | None) -> dict:
    """PersonName VR — values are objects: {Alphabetic, Ideographic, Phonetic}."""
    if not name:
        return {"vr": "PN"}
    return {"vr": "PN", "Value": [{"Alphabetic": name}]}


def _study_json(study_uid: str, series_dict: dict[str, list[InstanceMeta]]) -> dict:
    sample = next(iter(series_dict.values()))[0]
    modalities = sorted({i.modality for s in series_dict.values() for i in s if i.modality})
    n_series = len(series_dict)
    n_instances = sum(len(s) for s in series_dict.values())
    return {
        "0020000D": _t("UI", study_uid),
        "00100010": _pn(sample.patient_name),
        "00100020": _t("LO", sample.patient_id),
        "00080020": _t("DA", sample.study_date),
        "00080030": _t("TM", sample.study_time),
        "00080050": _t("SH", sample.accession_number),
        "00080061": _t("CS", modalities) if modalities else {"vr": "CS"},
        "00080090": _pn(None),
        "00201206": _t("IS", n_series),
        "00201208": _t("IS", n_instances),
    }


def _series_json(study_uid: str, series_uid: str, instances: list[InstanceMeta]) -> dict:
    sample = instances[0]
    return {
        "0020000D": _t("UI", study_uid),
        "0020000E": _t("UI", series_uid),
        "00080060": _t("CS", sample.modality),
        "0008103E": _t("LO", sample.series_description),
        "00200011": _t("IS", sample.series_number),
        "00201209": _t("IS", len(instances)),
        "00080021": _t("DA", sample.series_date),
    }


def _instance_qido_json(m: InstanceMeta) -> dict:
    """Minimal per-instance QIDO response."""
    return {
        "00080016": _t("UI", m.sop_class_uid),
        "00080018": _t("UI", m.sop_instance_uid),
        "0020000D": _t("UI", m.study_instance_uid),
        "0020000E": _t("UI", m.series_instance_uid),
        "00200013": _t("IS", m.instance_number),
        "00280010": _t("US", m.rows),
        "00280011": _t("US", m.columns),
        "00280100": _t("US", m.bits_allocated),
        "00280008": _t("IS", m.number_of_frames),
    }


def _instance_metadata_json(m: InstanceMeta) -> dict:
    """Full per-instance metadata for Cornerstone rendering + side-panel display."""
    d = _instance_qido_json(m)
    d.update({
        # File Meta — OHIF reads TransferSyntaxUID from here
        "00020010": _t("UI", m.transfer_syntax_uid),
        # Patient
        "00100010": _pn(m.patient_name),
        "00100020": _t("LO", m.patient_id),
        "00100040": _t("CS", m.patient_sex),
        "00101010": _t("AS", m.patient_age),
        "00100030": _t("DA", m.patient_birth_date),
        # Study
        "00080020": _t("DA", m.study_date),
        "00080030": _t("TM", m.study_time),
        "00080050": _t("SH", m.accession_number),
        "00081030": _t("LO", m.study_description),
        "00080090": _pn(m.referring_physician),
        # Series
        "00080060": _t("CS", m.modality),
        "0008103E": _t("LO", m.series_description),
        "00200011": _t("IS", m.series_number),
        "00180015": _t("CS", m.body_part_examined),
        "00181030": _t("LO", m.protocol_name),
        "00080008": _t("CS", m.image_type),
        # MR acquisition (no-op if absent for non-MR modalities)
        "00180020": _t("CS", m.scanning_sequence),
        "00180021": _t("CS", m.sequence_variant),
        "00180080": _t("DS", m.repetition_time),
        "00180081": _t("DS", m.echo_time),
        "00180082": _t("DS", m.inversion_time),
        "00181314": _t("DS", m.flip_angle),
        "00180010": _t("LO", m.contrast_bolus_agent),
        # Source / equipment
        "00080070": _t("LO", m.manufacturer),
        "00081090": _t("LO", m.model_name),
        "00081010": _t("SH", m.station_name),
        "00181020": _t("LO", m.software_versions),
        "00080022": _t("DA", m.acquisition_date),
        "00080032": _t("TM", m.acquisition_time),
        # Image
        "00280101": _t("US", m.bits_stored),
        "00280102": _t("US", m.high_bit),
        "00280103": _t("US", m.pixel_representation),
        "00280002": _t("US", m.samples_per_pixel),
        "00280004": _t("CS", m.photometric_interpretation),
        "00280030": _t("DS", m.pixel_spacing),
        "00200032": _t("DS", m.image_position_patient),
        "00200037": _t("DS", m.image_orientation_patient),
        "00180050": _t("DS", m.slice_thickness),
        "00200052": _t("UI", m.frame_of_reference_uid),
        "00281053": _t("DS", m.rescale_slope),
        "00281052": _t("DS", m.rescale_intercept),
        "00281050": _t("DS", m.window_center),
        "00281051": _t("DS", m.window_width),
    })
    return d


# ----- WADO-RS multipart wrapper ---------------------------------------------

def _multipart_wrap(data: bytes, content_type: str, content_location: str) -> bytes:
    """Wrap a single body part as multipart/related per RFC 2046 + DICOMweb conventions."""
    crlf = b"\r\n"
    boundary = MULTIPART_BOUNDARY.encode()
    parts = [
        b"--" + boundary + crlf,
        f"Content-Type: {content_type}".encode() + crlf,
        f"Content-Location: {content_location}".encode() + crlf,
        crlf,
        data,
        crlf,
        b"--" + boundary + b"--" + crlf,
    ]
    return b"".join(parts)
