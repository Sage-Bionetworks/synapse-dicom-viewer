"""CLI entry point for synapse-dicom-viewer."""

import argparse
import os
import threading
import webbrowser
from urllib.parse import quote

import uvicorn


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="synapse-dicom-viewer",
        description="View Synapse-hosted DICOM studies in OHIF Viewer",
    )
    parser.add_argument(
        "folder_id",
        nargs="?",
        default=None,
        help="Synapse folder ID (synXXXXX) containing .dcm entities. "
             "Omit when using --local-folder.",
    )
    parser.add_argument(
        "--local-folder",
        default=None,
        help="Path to a local directory of .dcm files (skips Synapse entirely).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for the local server (default: 8000)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Synapse personal access token. Falls back to SYNAPSE_AUTH_TOKEN env var, then ~/.synapseConfig.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open the browser on startup.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose logging to logs/ directory and stdout.",
    )
    return parser.parse_args(argv)


def authenticate_synapse(token: str | None):
    import synapseclient
    syn = synapseclient.Synapse()
    auth_token = token or os.environ.get("SYNAPSE_AUTH_TOKEN")
    if auth_token:
        syn.login(authToken=auth_token, silent=True)
    else:
        syn.login(silent=True)
    return syn


def build_browser_url(port: int, study_uid: str | None) -> str:
    base = f"http://localhost:{port}"
    if study_uid is None:
        return f"{base}/"
    return (
        f"{base}/viewer/?configUrl={quote('/viewer-config.js')}"
        f"&StudyInstanceUIDs={quote(study_uid)}"
    )


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    from synapse_dicom_viewer.proxy import (
        set_synapse_client, set_pending_folder, set_local_folder, set_verbose,
    )
    set_verbose(args.verbose)

    if args.local_folder and args.folder_id:
        raise SystemExit("Pass either folder_id OR --local-folder, not both.")

    if args.local_folder:
        print(f"Local folder mode: {args.local_folder}")
        set_local_folder(args.local_folder)
    elif args.folder_id:
        print("Authenticating with Synapse...")
        syn = authenticate_synapse(args.token)
        print(f"Logged in as {syn.credentials.owner_id}")
        set_synapse_client(syn)
        set_pending_folder(args.folder_id)
    else:
        print("No folder specified. Server will start; use POST /api/index?folder_id=synXXX to load.")

    # Open the landing page on startup; it polls /api/state and auto-redirects
    # to the viewer once the index is built.
    if not args.no_browser:
        landing_url = f"http://localhost:{args.port}/"
        threading.Timer(1.5, lambda: webbrowser.open(landing_url)).start()

    print(f"Starting server on http://localhost:{args.port}")
    uvicorn.run(
        "synapse_dicom_viewer.proxy:app",
        host="127.0.0.1",
        port=args.port,
        log_level="info" if args.verbose else "warning",
    )
