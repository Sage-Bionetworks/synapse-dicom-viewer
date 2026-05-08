"""Build an in-memory study/series/instance index from a folder of .dcm files.

Two source backends:
- SynapseDicomSource: enumerates a Synapse folder, range-fetches headers via presigned URLs
- LocalDicomSource: walks a directory on disk, reads files directly

The recovery ladder (64KB -> 256KB -> 1MB -> full) handles cases where header
metadata extends beyond the initial range read (enhanced multi-frame, large
private tags, ICC profiles).
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

import httpx
import pydicom
from pydicom.errors import InvalidDicomError

from synapse_dicom_viewer.refreshing_url import SynapseRefreshingUrl

log = logging.getLogger(__name__)

HEADER_LADDER = (65536, 262144, 1048576, None)  # bytes; None = full file
INDEX_CONCURRENCY = 16


@dataclass
class InstanceMeta:
    """Per-instance DICOM metadata extracted from the file header."""
    sop_instance_uid: str
    sop_class_uid: str
    series_instance_uid: str
    study_instance_uid: str
    modality: str
    transfer_syntax_uid: str
    entity_id: str  # Synapse syn-id, or absolute path for local mode
    file_size: int | None = None

    instance_number: int | None = None
    rows: int | None = None
    columns: int | None = None
    bits_allocated: int | None = None
    bits_stored: int | None = None
    high_bit: int | None = None
    pixel_representation: int | None = None
    samples_per_pixel: int | None = None
    photometric_interpretation: str | None = None
    pixel_spacing: list[float] | None = None
    image_position_patient: list[float] | None = None
    image_orientation_patient: list[float] | None = None
    slice_thickness: float | None = None
    frame_of_reference_uid: str | None = None
    rescale_slope: float | None = None
    rescale_intercept: float | None = None
    window_center: float | None = None
    window_width: float | None = None
    number_of_frames: int = 1

    patient_name: str | None = None
    patient_id: str | None = None
    patient_sex: str | None = None
    patient_age: str | None = None
    patient_birth_date: str | None = None
    study_date: str | None = None
    study_time: str | None = None
    study_description: str | None = None
    accession_number: str | None = None
    referring_physician: str | None = None
    series_description: str | None = None
    series_number: int | None = None
    series_date: str | None = None
    body_part_examined: str | None = None
    protocol_name: str | None = None
    image_type: list[str] | None = None
    scanning_sequence: list[str] | None = None
    sequence_variant: list[str] | None = None
    repetition_time: float | None = None
    echo_time: float | None = None
    inversion_time: float | None = None
    flip_angle: float | None = None
    contrast_bolus_agent: str | None = None
    manufacturer: str | None = None
    model_name: str | None = None
    station_name: str | None = None
    software_versions: str | None = None
    acquisition_date: str | None = None
    acquisition_time: str | None = None


@dataclass
class StudyTree:
    """All studies/series/instances discovered in a folder, indexed for QIDO/WADO."""
    folder_id: str
    studies: dict[str, dict[str, list[InstanceMeta]]] = field(default_factory=dict)
    sop_to_entity: dict[str, str] = field(default_factory=dict)
    entity_to_meta: dict[str, InstanceMeta] = field(default_factory=dict)

    def add(self, meta: InstanceMeta) -> None:
        study = self.studies.setdefault(meta.study_instance_uid, {})
        series = study.setdefault(meta.series_instance_uid, [])
        series.append(meta)
        self.sop_to_entity[meta.sop_instance_uid] = meta.entity_id
        self.entity_to_meta[meta.entity_id] = meta

    def sort_series(self) -> None:
        """Sort each series by InstanceNumber for predictable scroll order."""
        for study in self.studies.values():
            for series in study.values():
                series.sort(key=lambda m: (m.instance_number or 0))


class DicomSource(ABC):
    """Abstract source: enumerates .dcm entities and fetches their bytes."""

    folder_id: str

    @abstractmethod
    async def list_entities(self) -> list[str]:
        """Return entity IDs (Synapse syn-id or local file path) for all .dcm files."""
        ...

    @abstractmethod
    async def fetch_bytes(self, entity_id: str, length: int | None) -> bytes:
        """Fetch the first `length` bytes of an entity, or all bytes if length is None."""
        ...

    @abstractmethod
    async def aclose(self) -> None:
        ...


class LocalDicomSource(DicomSource):
    """Read .dcm files from a local directory (no Synapse coupling)."""

    def __init__(self, folder_path: str | Path):
        self.folder_path = Path(folder_path).resolve()
        self.folder_id = f"local:{self.folder_path}"

    async def list_entities(self) -> list[str]:
        if not self.folder_path.is_dir():
            raise FileNotFoundError(f"Not a directory: {self.folder_path}")
        paths = sorted(
            str(p) for p in self.folder_path.iterdir()
            if p.is_file() and p.suffix.lower() == ".dcm"
        )
        log.info("LocalDicomSource: %d .dcm files in %s", len(paths), self.folder_path)
        return paths

    async def fetch_bytes(self, entity_id: str, length: int | None) -> bytes:
        path = Path(entity_id)
        return await asyncio.to_thread(_read_partial, path, length)

    async def aclose(self) -> None:
        pass


def _read_partial(path: Path, length: int | None) -> bytes:
    with path.open("rb") as fh:
        if length is None:
            return fh.read()
        return fh.read(length)


class SynapseDicomSource(DicomSource):
    """Enumerate a Synapse folder and range-fetch via presigned URLs.

    Two auth modes:
    - syn=synapseclient.Synapse instance (local mode — single user)
    - token_factory=callable returning a PAT string (hosted mode — token decrypted
      momentarily at refresh time, never retained)

    For folder enumeration in hosted mode we issue plain Synapse REST calls
    using the token; no synapseclient instance is created.
    """

    SYNAPSE_REPO = "https://repo-prod.prod.sagebase.org/repo/v1"

    def __init__(self, folder_id: str, syn=None, token_factory=None):
        if syn is None and token_factory is None:
            raise ValueError("SynapseDicomSource requires either syn= or token_factory=")
        self.folder_id = folder_id
        self._syn = syn
        self._token_factory = token_factory
        self._refreshers: dict[str, SynapseRefreshingUrl] = {}
        self._http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    async def list_entities(self) -> list[str]:
        if self._syn is not None:
            children = await asyncio.to_thread(
                lambda: list(self._syn.getChildren(self.folder_id, includeTypes=["file"]))
            )
        else:
            children = await self._list_via_rest()
        ids = [c["id"] for c in children if c.get("name", "").lower().endswith(".dcm")]
        log.info("SynapseDicomSource: %d .dcm entities in folder %s", len(ids), self.folder_id)
        return ids

    async def _list_via_rest(self) -> list[dict]:
        """Page through /entity/children using the hosted-mode token."""
        results: list[dict] = []
        next_token = None
        while True:
            body: dict = {
                "parentId": self.folder_id,
                "includeTypes": ["file"],
                "sortBy": "NAME",
                "sortDirection": "ASC",
            }
            if next_token:
                body["nextPageToken"] = next_token
            token = self._token_factory()
            r = await self._http.post(
                f"{self.SYNAPSE_REPO}/entity/children",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            data = r.json()
            results.extend(data.get("page", []))
            next_token = data.get("nextPageToken")
            if not next_token:
                break
        return results

    def _refresher(self, entity_id: str) -> SynapseRefreshingUrl:
        r = self._refreshers.get(entity_id)
        if r is None:
            r = SynapseRefreshingUrl(entity_id, self._syn or self._token_factory)
            self._refreshers[entity_id] = r
        return r

    async def fetch_bytes(self, entity_id: str, length: int | None) -> bytes:
        refresher = self._refresher(entity_id)
        url = await asyncio.to_thread(refresher.get)
        headers = {}
        if length is not None:
            headers["Range"] = f"bytes=0-{length - 1}"
        r = await self._http.get(url, headers=headers)
        if r.status_code == 403:
            await asyncio.to_thread(refresher.invalidate)
            url = await asyncio.to_thread(refresher.get)
            r = await self._http.get(url, headers=headers)
        r.raise_for_status()
        return r.content

    async def aclose(self) -> None:
        await self._http.aclose()


def _try_parse(data: bytes) -> pydicom.Dataset | None:
    """Parse partial DICOM bytes; return Dataset if metadata is intact, else None."""
    try:
        ds = pydicom.dcmread(
            BytesIO(data),
            stop_before_pixels=True,
            force=False,
            defer_size="512 KB",
        )
    except (InvalidDicomError, EOFError, AttributeError, OSError):
        return None
    # Sanity check: required identifiers must be present
    if not all(hasattr(ds, t) for t in ("SOPInstanceUID", "SeriesInstanceUID", "StudyInstanceUID")):
        return None
    return ds


async def _parse_one(source: DicomSource, entity_id: str, sem: asyncio.Semaphore) -> InstanceMeta | None:
    async with sem:
        for size in HEADER_LADDER:
            try:
                data = await source.fetch_bytes(entity_id, size)
            except Exception as e:
                log.warning("fetch failed for %s at size=%s: %s", entity_id, size, e)
                return None
            ds = _try_parse(data)
            if ds is not None:
                return _to_meta(ds, entity_id, file_size=len(data) if size is None else None)
            log.debug("header parse failed for %s at size=%s; retrying", entity_id, size)
        log.error("header parse exhausted ladder for %s", entity_id)
        return None


def _to_meta(ds: pydicom.Dataset, entity_id: str, file_size: int | None = None) -> InstanceMeta:
    file_meta = getattr(ds, "file_meta", None)
    transfer_syntax = (
        str(file_meta.TransferSyntaxUID) if file_meta and "TransferSyntaxUID" in file_meta
        else "1.2.840.10008.1.2"  # Implicit VR Little Endian default
    )
    return InstanceMeta(
        sop_instance_uid=str(ds.SOPInstanceUID),
        sop_class_uid=str(getattr(ds, "SOPClassUID", "")),
        series_instance_uid=str(ds.SeriesInstanceUID),
        study_instance_uid=str(ds.StudyInstanceUID),
        modality=str(getattr(ds, "Modality", "")),
        transfer_syntax_uid=transfer_syntax,
        entity_id=entity_id,
        file_size=file_size,
        instance_number=_int_or_none(getattr(ds, "InstanceNumber", None)),
        rows=_int_or_none(getattr(ds, "Rows", None)),
        columns=_int_or_none(getattr(ds, "Columns", None)),
        bits_allocated=_int_or_none(getattr(ds, "BitsAllocated", None)),
        bits_stored=_int_or_none(getattr(ds, "BitsStored", None)),
        high_bit=_int_or_none(getattr(ds, "HighBit", None)),
        pixel_representation=_int_or_none(getattr(ds, "PixelRepresentation", None)),
        samples_per_pixel=_int_or_none(getattr(ds, "SamplesPerPixel", None)),
        photometric_interpretation=_str_or_none(getattr(ds, "PhotometricInterpretation", None)),
        pixel_spacing=_floats_or_none(getattr(ds, "PixelSpacing", None)),
        image_position_patient=_floats_or_none(getattr(ds, "ImagePositionPatient", None)),
        image_orientation_patient=_floats_or_none(getattr(ds, "ImageOrientationPatient", None)),
        slice_thickness=_float_or_none(getattr(ds, "SliceThickness", None)),
        frame_of_reference_uid=_str_or_none(getattr(ds, "FrameOfReferenceUID", None)),
        rescale_slope=_float_or_none(getattr(ds, "RescaleSlope", None)),
        rescale_intercept=_float_or_none(getattr(ds, "RescaleIntercept", None)),
        window_center=_first_float_or_none(getattr(ds, "WindowCenter", None)),
        window_width=_first_float_or_none(getattr(ds, "WindowWidth", None)),
        number_of_frames=_int_or_none(getattr(ds, "NumberOfFrames", None)) or 1,
        patient_name=_pn_or_none(getattr(ds, "PatientName", None)),
        patient_id=_str_or_none(getattr(ds, "PatientID", None)),
        patient_sex=_str_or_none(getattr(ds, "PatientSex", None)),
        patient_age=_str_or_none(getattr(ds, "PatientAge", None)),
        patient_birth_date=_str_or_none(getattr(ds, "PatientBirthDate", None)),
        study_date=_str_or_none(getattr(ds, "StudyDate", None)),
        study_time=_str_or_none(getattr(ds, "StudyTime", None)),
        study_description=_str_or_none(getattr(ds, "StudyDescription", None)),
        accession_number=_str_or_none(getattr(ds, "AccessionNumber", None)),
        referring_physician=_pn_or_none(getattr(ds, "ReferringPhysicianName", None)),
        series_description=_str_or_none(getattr(ds, "SeriesDescription", None)),
        series_number=_int_or_none(getattr(ds, "SeriesNumber", None)),
        series_date=_str_or_none(getattr(ds, "SeriesDate", None)),
        body_part_examined=_str_or_none(getattr(ds, "BodyPartExamined", None)),
        protocol_name=_str_or_none(getattr(ds, "ProtocolName", None)),
        image_type=_strs_or_none(getattr(ds, "ImageType", None)),
        scanning_sequence=_strs_or_none(getattr(ds, "ScanningSequence", None)),
        sequence_variant=_strs_or_none(getattr(ds, "SequenceVariant", None)),
        repetition_time=_float_or_none(getattr(ds, "RepetitionTime", None)),
        echo_time=_float_or_none(getattr(ds, "EchoTime", None)),
        inversion_time=_float_or_none(getattr(ds, "InversionTime", None)),
        flip_angle=_float_or_none(getattr(ds, "FlipAngle", None)),
        contrast_bolus_agent=_str_or_none(getattr(ds, "ContrastBolusAgent", None)),
        manufacturer=_str_or_none(getattr(ds, "Manufacturer", None)),
        model_name=_str_or_none(getattr(ds, "ManufacturerModelName", None)),
        station_name=_str_or_none(getattr(ds, "StationName", None)),
        software_versions=_str_or_none(getattr(ds, "SoftwareVersions", None)),
        acquisition_date=_str_or_none(getattr(ds, "AcquisitionDate", None)),
        acquisition_time=_str_or_none(getattr(ds, "AcquisitionTime", None)),
    )


def _int_or_none(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float_or_none(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v)
    return s if s else None


def _pn_or_none(v: Any) -> str | None:
    """PersonName -> string; pydicom returns PersonName3 which str()s to the family^given form."""
    return _str_or_none(v)


def _floats_or_none(v: Any) -> list[float] | None:
    if v is None:
        return None
    try:
        return [float(x) for x in v]
    except (TypeError, ValueError):
        return None


def _strs_or_none(v: Any) -> list[str] | None:
    if v is None:
        return None
    if isinstance(v, str):
        return [v] if v else None
    try:
        out = [str(x) for x in v if str(x)]
        return out or None
    except TypeError:
        s = str(v)
        return [s] if s else None


def _first_float_or_none(v: Any) -> float | None:
    """WindowCenter/Width can be a single value or a list; return the first."""
    if v is None or v == "":
        return None
    if isinstance(v, (list, tuple, pydicom.multival.MultiValue)):
        if not v:
            return None
        try:
            return float(v[0])
        except (TypeError, ValueError):
            return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def build_index(source: DicomSource) -> StudyTree:
    """Enumerate the source, parse all DICOM headers concurrently, return a StudyTree."""
    entity_ids = await source.list_entities()
    if not entity_ids:
        log.warning("No .dcm entities found in %s", source.folder_id)
        return StudyTree(folder_id=source.folder_id)

    sem = asyncio.Semaphore(INDEX_CONCURRENCY)
    tasks = [_parse_one(source, eid, sem) for eid in entity_ids]
    results = await asyncio.gather(*tasks)

    tree = StudyTree(folder_id=source.folder_id)
    parsed = 0
    for meta in results:
        if meta is not None:
            tree.add(meta)
            parsed += 1
    tree.sort_series()
    log.info(
        "Indexed %s: %d/%d parsed; %d studies, %d series",
        source.folder_id, parsed, len(entity_ids),
        len(tree.studies), sum(len(s) for s in tree.studies.values()),
    )
    return tree
