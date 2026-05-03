#!/usr/bin/env python3
"""
archive-directory.py

For each configured directory:
  1. Packs all files into a .tar archive in a local tmp folder
  2. Uploads the .tar to the specified Google Drive folder
  3. On success, deletes source files matching delete_after_upload patterns
     and removes the local .tar file

Usage:
    python3 archive-directory.py [--config /path/to/archive-directory-config.json]
"""

import argparse
import fnmatch
import json
import logging
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
DEFAULT_CONFIG = Path(__file__).parent / "archive-directory-config.json"


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup_logging(log_file: str) -> None:
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    handler_file = logging.FileHandler(log_file)
    handler_file.setFormatter(fmt)

    handler_stdout = logging.StreamHandler(sys.stdout)
    handler_stdout.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler_file)
    root.addHandler(handler_stdout)


def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def build_drive_service(sa_json: str):
    creds = service_account.Credentials.from_service_account_file(
        sa_json, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# Tar creation
# ---------------------------------------------------------------------------

def create_tar(source_path: Path, tar_path: Path) -> list[Path]:
    """Pack all files in source_path into tar_path. Returns list of files included."""
    files = sorted(f for f in source_path.iterdir() if f.is_file())
    if not files:
        return []

    with tarfile.open(tar_path, "w") as tar:
        for f in files:
            tar.add(f, arcname=f.name)

    return files


# ---------------------------------------------------------------------------
# Google Drive helpers
# ---------------------------------------------------------------------------

def tar_already_uploaded(svc, tar_name: str, folder_id: str, shared_drive_id: str) -> bool:
    params = dict(
        q=f"name='{tar_name}' and '{folder_id}' in parents and trashed=false",
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    )
    if shared_drive_id:
        params["driveId"] = shared_drive_id
        params["corpora"] = "drive"

    result = svc.files().list(**params).execute()
    return len(result.get("files", [])) > 0


def upload_tar(svc, tar_path: Path, folder_id: str, shared_drive_id: str) -> bool:
    media = MediaFileUpload(str(tar_path), mimetype="application/x-tar", resumable=True)
    body = {"name": tar_path.name, "parents": [folder_id]}

    try:
        svc.files().create(
            body=body,
            media_body=media,
            supportsAllDrives=True,
            fields="id",
        ).execute()
        return True
    except HttpError as e:
        logging.error("Upload failed for %s: %s", tar_path.name, e)
        return False


# ---------------------------------------------------------------------------
# Per-directory processing
# ---------------------------------------------------------------------------

def process_directory(svc, entry: dict, shared_drive_id: str, tmp_dir: Path) -> None:
    name = entry["name"]
    source_path = Path(entry["source_path"])
    folder_id = entry["gdrive_folder_id"]
    delete_patterns = entry.get("delete_after_upload", ["*"])

    logging.info("=== Processing [%s] => %s ===", name, source_path)

    if not source_path.exists():
        logging.error("[%s] Source path does not exist: %s", name, source_path)
        return

    files = sorted(f for f in source_path.iterdir() if f.is_file())
    if not files:
        logging.info("[%s] No files found in source directory.", name)
        return

    logging.info("[%s] Found %d file(s) to archive.", name, len(files))

    # Name the tar with a timestamp so each run produces a unique archive
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    tar_name = f"{name}_{timestamp}.tar"
    tar_path = tmp_dir / tar_name

    # Skip if this exact tar was already uploaded (e.g. script re-run same second)
    try:
        if tar_already_uploaded(svc, tar_name, folder_id, shared_drive_id):
            logging.info("[%s] Tar already present in GDrive, skipping upload.", name)
            return
    except HttpError as e:
        logging.error("[%s] Could not check GDrive folder: %s", name, e)
        return

    # --- Step 1: create tar ---
    logging.info("[%s] Creating archive: %s", name, tar_path)
    try:
        archived_files = create_tar(source_path, tar_path)
    except Exception as e:
        logging.error("[%s] Failed to create tar: %s", name, e)
        return

    tar_size_mb = tar_path.stat().st_size / 1_048_576
    logging.info("[%s] Archive created: %s (%.1f MB, %d files)",
                 name, tar_name, tar_size_mb, len(archived_files))

    # --- Step 2: upload tar to GDrive ---
    logging.info("[%s] Uploading %s to GDrive...", name, tar_name)
    if not upload_tar(svc, tar_path, folder_id, shared_drive_id):
        logging.error("[%s] Upload failed — source files and tar preserved.", name)
        return

    logging.info("[%s] Upload successful.", name)

    # --- Step 3: delete matching source files ---
    archived_names = {f.name for f in archived_files}
    deleted_sources = []

    for pattern in delete_patterns:
        for file_path in source_path.iterdir():
            if not file_path.is_file():
                continue
            if not fnmatch.fnmatch(file_path.name, pattern):
                continue
            if file_path.name not in archived_names:
                # File appeared after tar was created — leave it
                continue
            try:
                file_path.unlink()
                deleted_sources.append(file_path.name)
            except OSError as e:
                logging.error("[%s] Could not delete source file %s: %s",
                              name, file_path.name, e)

    logging.info("[%s] Deleted %d source file(s) matching patterns %s.",
                 name, len(deleted_sources), delete_patterns)

    # --- Step 4: delete local tar ---
    try:
        tar_path.unlink()
        logging.info("[%s] Deleted local tar: %s", name, tar_name)
    except OSError as e:
        logging.error("[%s] Could not delete local tar %s: %s", name, tar_name, e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Archive directories to Google Drive.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="Path to JSON config file (default: archive-directory-config.json)")
    args = parser.parse_args()

    cfg = load_config(args.config)

    log_file = cfg.get("log_file", str(Path(__file__).parent / "tmp" / "archive-directory.log"))
    setup_logging(log_file)

    sa_json = cfg.get("gdrive_service_account_json")
    if not sa_json or not Path(sa_json).exists():
        logging.error("gdrive_service_account_json not found: %s", sa_json)
        sys.exit(1)

    shared_drive_id = cfg.get("gdrive_shared_drive_id", "")

    tmp_dir = Path(cfg.get("tmp_dir", str(Path(__file__).parent / "tmp")))
    tmp_dir.mkdir(parents=True, exist_ok=True)

    directories = cfg.get("directories", [])
    if not directories:
        logging.error("No directories defined in config.")
        sys.exit(1)

    logging.info("Authenticating with Google Drive service account.")
    try:
        svc = build_drive_service(sa_json)
    except Exception as e:
        logging.error("Failed to build Drive service: %s", e)
        sys.exit(1)

    for entry in directories:
        try:
            process_directory(svc, entry, shared_drive_id, tmp_dir)
        except Exception as e:
            logging.error("Unexpected error processing [%s]: %s", entry.get("name", "?"), e)

    logging.info("Done.")


if __name__ == "__main__":
    main()
