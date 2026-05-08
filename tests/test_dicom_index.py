"""Index a real folder of .dcm files and assert basic shape + tag extraction."""

from pathlib import Path

import pytest

from synapse_dicom_viewer.dicom_index import LocalDicomSource, build_index

SAMPLE_FOLDER = Path("/Users/ataylor/Downloads/eg_brain_t1")
pytestmark = pytest.mark.skipif(
    not SAMPLE_FOLDER.is_dir(),
    reason=f"sample folder not present: {SAMPLE_FOLDER}",
)


async def test_local_index_parses_all_files():
    src = LocalDicomSource(SAMPLE_FOLDER)
    tree = await build_index(src)
    assert len(tree.studies) == 1
    study = next(iter(tree.studies.values()))
    assert sum(len(s) for s in study.values()) == 416


async def test_local_index_extracts_required_tags():
    src = LocalDicomSource(SAMPLE_FOLDER)
    tree = await build_index(src)
    study = next(iter(tree.studies.values()))
    series = next(iter(study.values()))
    sample = series[0]
    assert sample.modality == "MR"
    assert sample.rows and sample.columns
    assert sample.transfer_syntax_uid.startswith("1.2.840.10008.1.2")
    assert sample.sop_instance_uid in tree.sop_to_entity


async def test_series_sorted_by_instance_number():
    src = LocalDicomSource(SAMPLE_FOLDER)
    tree = await build_index(src)
    study = next(iter(tree.studies.values()))
    for series in study.values():
        numbers = [m.instance_number for m in series if m.instance_number is not None]
        assert numbers == sorted(numbers)
