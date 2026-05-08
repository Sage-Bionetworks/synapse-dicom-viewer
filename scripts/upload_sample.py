"""Upload a local folder of .dcm files to a new (or existing) Synapse project.

Used to seed test data for `synapse-dicom-viewer synXXX` end-to-end runs.
Idempotent: re-running skips files that already exist in the target folder.

Usage:
    uv run python scripts/upload_sample.py \\
        --local /Users/ataylor/Downloads/eg_brain_t1 \\
        --project "synapse-dicom-viewer-test" \\
        --folder eg_brain_t1
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import synapseclient
from synapseclient import File, Folder, Project


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--local", required=True, type=Path,
                   help="Local directory containing .dcm files")
    p.add_argument("--project", required=True,
                   help="Synapse project name. Created if it doesn't exist.")
    p.add_argument("--folder", required=True,
                   help="Folder name within the project. Created if needed.")
    p.add_argument("--workers", type=int, default=6,
                   help="Parallel uploads (default 6)")
    p.add_argument("--pattern", default="*.dcm",
                   help="Glob for files to upload (default *.dcm)")
    return p.parse_args()


def get_or_create_project(syn: synapseclient.Synapse, name: str) -> Project:
    eid = syn.findEntityId(name)
    if eid:
        proj = syn.get(eid)
        print(f"Using existing project: {proj.id}  ({name})")
        return proj
    proj = syn.store(Project(name))
    print(f"Created project: {proj.id}  ({name})")
    return proj


def get_or_create_folder(syn: synapseclient.Synapse, name: str, parent_id: str) -> Folder:
    eid = syn.findEntityId(name, parent=parent_id)
    if eid:
        f = syn.get(eid)
        print(f"Using existing folder: {f.id}  ({name})")
        return f
    f = syn.store(Folder(name, parent=parent_id))
    print(f"Created folder: {f.id}  ({name})")
    return f


def upload_one(syn: synapseclient.Synapse, path: Path, parent_id: str,
               existing_names: set[str]) -> tuple[Path, str]:
    if path.name in existing_names:
        return path, "skipped"
    syn.store(File(str(path), parent=parent_id), forceVersion=False)
    return path, "uploaded"


def main():
    args = parse_args()
    if not args.local.is_dir():
        sys.exit(f"not a directory: {args.local}")

    syn = synapseclient.Synapse()
    syn.login(silent=True)
    print(f"Logged in as {syn.credentials.owner_id}")

    proj = get_or_create_project(syn, args.project)
    folder = get_or_create_folder(syn, args.folder, proj.id)

    # Pre-fetch existing file names so we can skip in parallel without N round-trips
    existing = {c["name"] for c in syn.getChildren(folder.id, includeTypes=["file"])}
    if existing:
        print(f"Folder already has {len(existing)} files; will skip duplicates.")

    files = sorted(args.local.glob(args.pattern))
    if not files:
        sys.exit(f"no files matching {args.pattern} in {args.local}")
    print(f"Uploading {len(files)} files with {args.workers} workers ...")

    t0 = time.time()
    uploaded = skipped = errors = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(upload_one, syn, f, folder.id, existing) for f in files]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                path, status = fut.result()
                if status == "uploaded":
                    uploaded += 1
                else:
                    skipped += 1
            except Exception as e:
                errors += 1
                print(f"  ERROR: {e}", file=sys.stderr)
            if i % 25 == 0 or i == len(files):
                rate = i / max(0.001, time.time() - t0)
                print(f"  [{i}/{len(files)}]  uploaded={uploaded} skipped={skipped} errors={errors}  ({rate:.1f}/s)")

    print(f"\nDone in {time.time() - t0:.1f}s. Folder: {folder.id}")
    print(f"Test with:  uv run synapse-dicom-viewer {folder.id} --verbose")


if __name__ == "__main__":
    main()
