#!/usr/bin/env python3
"""Automated server backup — archives directories and uploads to Google Drive or rclone remote."""

import atexit
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()

CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", SCRIPT_DIR / ".env"))


def _load_env(path: Path) -> dict:
    env = {}
    if not path.is_file():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        env[key.strip()] = value
    return env


_env = _load_env(CONFIG_FILE)


def _cfg(key: str, default: str = "") -> str:
    return _env.get(key, os.environ.get(key, default))


BACKUP_ROOT             = Path(_cfg("BACKUP_ROOT", "/var/backups/automated-server-backup"))
LOG_FILE                = Path(_cfg("LOG_FILE", "/var/log/automated-server-backup.log"))
UPLOAD_PROVIDER         = _cfg("UPLOAD_PROVIDER", "rclone")
RCLONE_REMOTE           = _cfg("RCLONE_REMOTE")
GDRIVE_SA_JSON          = _cfg("GDRIVE_SERVICE_ACCOUNT_JSON")
GDRIVE_SHARED_DRIVE_ID  = _cfg("GDRIVE_SHARED_DRIVE_ID")
GDRIVE_PARENT_FOLDER_ID = _cfg("GDRIVE_PARENT_FOLDER_ID")
RETENTION_DAYS          = int(_cfg("RETENTION_DAYS", "4"))
MARGIN_PERCENT          = int(_cfg("ESTIMATE_MARGIN_PERCENT", "20"))
ALERT_PROVIDER          = _cfg("ALERT_PROVIDER")
ALERT_WEBHOOK_URL       = _cfg("ALERT_WEBHOOK_URL")
TELEGRAM_BOT_TOKEN      = _cfg("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID        = _cfg("TELEGRAM_CHAT_ID")
ALERT_TITLE             = _cfg("ALERT_TITLE", "Automated Server Backup Alert")
TMP_WORK_ROOT           = Path(_cfg("TMP_WORK_ROOT", "/var/tmp/automated-server-backup"))
INCLUSION_LIST_FILE     = Path(_cfg("INCLUSION_LIST_FILE", "/var/automated-server-backup/directory-inclusions.txt"))

DATE_STAMP      = datetime.now().strftime("%Y%m%d")
RUN_BACKUP_DIR  = BACKUP_ROOT / DATE_STAMP
RUN_WORK_DIR    = TMP_WORK_ROOT / f"{DATE_STAMP}-{os.getpid()}"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

for _d in (BACKUP_ROOT, RUN_BACKUP_DIR, TMP_WORK_ROOT, RUN_WORK_DIR):
    _d.mkdir(parents=True, exist_ok=True)
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
LOG_FILE.touch(exist_ok=True)

_fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("backup")
log.setLevel(logging.INFO)
for _h in (logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)):
    _h.setFormatter(_fmt)
    log.addHandler(_h)

# ---------------------------------------------------------------------------
# Cleanup trap
# ---------------------------------------------------------------------------

_stopped_containers: list = []


def _cleanup():
    for container in _stopped_containers:
        log.info("Ensuring container is running again: %s", container)
        try:
            subprocess.run(
                ["docker", "start", container],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            log.info("Failed to restart %s; manual check needed", container)
    if RUN_WORK_DIR.exists():
        shutil.rmtree(RUN_WORK_DIR)


atexit.register(_cleanup)

# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def send_alert(message: str):
    log.info("Sending alert via provider: %s", ALERT_PROVIDER or "none")

    if ALERT_PROVIDER == "telegram":
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            log.info("Telegram alert not sent: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
            return
        try:
            data = urllib.parse.urlencode({
                "chat_id": TELEGRAM_CHAT_ID,
                "text": f"{ALERT_TITLE}\n{message}",
            }).encode()
            urllib.request.urlopen(
                urllib.request.Request(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    data=data,
                ),
                timeout=15,
            )
        except Exception as exc:
            log.info("Telegram alert send failed: %s", exc)

    elif ALERT_PROVIDER in ("google_chat", "teams"):
        if not ALERT_WEBHOOK_URL:
            log.info("%s alert not sent: ALERT_WEBHOOK_URL is missing", ALERT_PROVIDER)
            return
        try:
            content_type = (
                "application/json; charset=UTF-8"
                if ALERT_PROVIDER == "google_chat"
                else "application/json"
            )
            payload = json.dumps({"text": f"{ALERT_TITLE}\n{message}"}).encode()
            urllib.request.urlopen(
                urllib.request.Request(
                    ALERT_WEBHOOK_URL, data=payload,
                    headers={"Content-Type": content_type},
                ),
                timeout=15,
            )
        except Exception as exc:
            log.info("%s alert send failed: %s", ALERT_PROVIDER, exc)

    elif not ALERT_PROVIDER or ALERT_PROVIDER == "none":
        log.info("No alert provider configured; alert message was: %s", message)

    else:
        log.info("Unknown ALERT_PROVIDER '%s'", ALERT_PROVIDER)

# ---------------------------------------------------------------------------
# Remote access validation
# ---------------------------------------------------------------------------

def validate_remote_access():
    if UPLOAD_PROVIDER == "rclone":
        if not RCLONE_REMOTE:
            log.info("Skipping remote access validation: RCLONE_REMOTE is not configured")
            return
        if not shutil.which("rclone"):
            log.info("Skipping remote access validation: rclone is not installed")
            return
        log.info("Validating remote access for %s", RCLONE_REMOTE)
        if subprocess.run(
            ["rclone", "lsf", RCLONE_REMOTE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode != 0:
            msg = (
                f"Backup stopped because Google Drive / rclone access validation failed "
                f"for {RCLONE_REMOTE}. OAuth or remote access may need to be reconnected."
            )
            log.info(msg)
            send_alert(msg)
            sys.exit(1)

    elif UPLOAD_PROVIDER == "gdrive_service_account":
        if not all([GDRIVE_SA_JSON, GDRIVE_SHARED_DRIVE_ID, GDRIVE_PARENT_FOLDER_ID]):
            log.info(
                "Skipping remote access validation: GDRIVE_SERVICE_ACCOUNT_JSON, "
                "GDRIVE_SHARED_DRIVE_ID, or GDRIVE_PARENT_FOLDER_ID is not configured"
            )
            return
        log.info("Validating Google Drive service account access")
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            creds = service_account.Credentials.from_service_account_file(
                GDRIVE_SA_JSON, scopes=["https://www.googleapis.com/auth/drive"]
            )
            build("drive", "v3", credentials=creds).files().list(
                q=f"'{GDRIVE_PARENT_FOLDER_ID}' in parents",
                driveId=GDRIVE_SHARED_DRIVE_ID,
                includeItemsFromAllDrives=True, supportsAllDrives=True,
                corpora="drive", fields="files(id)", pageSize=1,
            ).execute()
        except Exception as exc:
            msg = (
                "Backup stopped because Google Drive service account access validation failed. "
                "Check GDRIVE_SERVICE_ACCOUNT_JSON and that the service account has access to the shared drive."
            )
            log.info("%s Error: %s", msg, exc)
            send_alert(msg)
            sys.exit(1)

    elif not UPLOAD_PROVIDER or UPLOAD_PROVIDER == "none":
        log.info("Skipping remote access validation: UPLOAD_PROVIDER is none")

    else:
        log.info("Unknown UPLOAD_PROVIDER '%s'", UPLOAD_PROVIDER)

# ---------------------------------------------------------------------------
# Container management
# ---------------------------------------------------------------------------

def _container_is_running(container: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == "true"


def stop_container_if_running(container: str):
    if _container_is_running(container):
        log.info("Stopping container for consistent backup: %s", container)
        subprocess.run(["docker", "stop", container], check=True, stdout=subprocess.DEVNULL)
        _stopped_containers.append(container)
    else:
        log.info("Container already stopped, leaving as-is: %s", container)


def start_stopped_containers():
    while _stopped_containers:
        container = _stopped_containers.pop()
        log.info("Starting container after backup: %s", container)
        subprocess.run(["docker", "start", container], check=True, stdout=subprocess.DEVNULL)

# ---------------------------------------------------------------------------
# MariaDB dump
# ---------------------------------------------------------------------------

def create_mariadb_dump():
    dump_dir = RUN_WORK_DIR / "docker-couchdb-extra"
    dump_dir.mkdir(parents=True, exist_ok=True)
    dump_path = dump_dir / f"mariadb-all-databases-{DATE_STAMP}.sql"
    log.info("Creating MariaDB logical dump: %s", dump_path)
    with open(dump_path, "wb") as f:
        subprocess.run(
            [
                "docker", "exec", "mariadb", "sh", "-lc",
                'exec mariadb-dump -uroot -p"$MYSQL_ROOT_PASSWORD" '
                "--all-databases --single-transaction --quick --lock-tables=false",
            ],
            stdout=f, check=True,
        )

# ---------------------------------------------------------------------------
# Tar archive creation
# ---------------------------------------------------------------------------

_BENIGN_TAR_RE = re.compile(
    r"^tar: (Removing leading .+ from (member names|hard link targets)|.+: file changed as we read it)$"
)


def _run_tar(*args):
    result = subprocess.run(["tar", *args], stderr=subprocess.PIPE, text=True)
    if result.returncode == 0:
        return
    if result.returncode == 1:
        non_benign = [
            line for line in result.stderr.splitlines()
            if line.strip() and not _BENIGN_TAR_RE.match(line.strip())
        ]
        if not non_benign:
            log.info("tar returned warning exit code only because files changed during read; continuing")
            return
    sys.stderr.write(result.stderr)
    raise subprocess.CalledProcessError(result.returncode, "tar")


def create_archive_for_stack(name: str, directory: str, mode: str):
    archive_path = RUN_BACKUP_DIR / f"{name}-{DATE_STAMP}.tar"
    exclude = ["--exclude=.git", "--exclude=__pycache__"]

    if mode == "couchdb_main":
        create_mariadb_dump()
        stop_container_if_running("mariadb")
        stop_container_if_running("couchdb")
        log.info("Creating backup archive with MariaDB dump and stopped CouchDB data: %s", archive_path)
        _run_tar(*exclude, "--exclude=mariadb_data",
                 "-cf", str(archive_path), directory,
                 "-C", str(RUN_WORK_DIR), "docker-couchdb-extra")
        start_stopped_containers()

    elif mode == "couchdb_stack":
        stop_container_if_running(f"couchdb_{name.removesuffix('-couchdb')}")
        log.info("Creating backup archive while stack container is stopped: %s", archive_path)
        _run_tar(*exclude, "-cf", str(archive_path), directory)
        start_stopped_containers()

    elif mode == "direct":
        log.info("Creating backup archive for included path: %s", archive_path)
        _run_tar(*exclude, "-cf", str(archive_path), directory)

    else:
        raise ValueError(f"Invalid backup mode '{mode}' for inclusion '{name}'")

# ---------------------------------------------------------------------------
# Inclusion list
# ---------------------------------------------------------------------------

def load_inclusion_list() -> list:
    if not INCLUSION_LIST_FILE.is_file():
        log.info("Missing inclusion list file: %s", INCLUSION_LIST_FILE)
        sys.exit(1)

    entries = []
    for raw in INCLUSION_LIST_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 3 or not all(parts):
            log.info("Invalid inclusion list entry (expected name:path:mode): %s", line)
            sys.exit(1)
        name, path, mode = parts
        if mode not in ("couchdb_main", "couchdb_stack", "direct"):
            log.info("Invalid inclusion list mode '%s' in entry: %s", mode, line)
            sys.exit(1)
        if mode == "couchdb_main" and name != "docker-couchdb":
            log.info("couchdb_main mode is only supported for docker-couchdb: %s", line)
            sys.exit(1)
        entries.append((name, path, mode))

    if not entries:
        log.info("Inclusion list is empty: %s", INCLUSION_LIST_FILE)
        sys.exit(1)

    return entries

# ---------------------------------------------------------------------------
# Disk space estimation
# ---------------------------------------------------------------------------

def _latest_backup_size_or_dir_size(name: str, directory: str) -> int:
    tars = sorted(BACKUP_ROOT.glob(f"*/{name}-*.tar"), key=lambda p: p.stat().st_mtime, reverse=True)
    if tars:
        return tars[0].stat().st_size
    return int(subprocess.run(["du", "-sb", directory], capture_output=True, text=True, check=True)
               .stdout.split()[0])


def _estimate_required_bytes(stack_dirs: list) -> int:
    total = sum(_latest_backup_size_or_dir_size(name, path) for name, path, _ in stack_dirs)
    return total + (total * MARGIN_PERCENT // 100)


def _free_bytes_available() -> int:
    return int(
        subprocess.run(["df", "-PB1", str(BACKUP_ROOT)], capture_output=True, text=True, check=True)
        .stdout.splitlines()[1].split()[3]
    )

# ---------------------------------------------------------------------------
# Upload — rclone
# ---------------------------------------------------------------------------

def upload_and_prune_rclone():
    if not RCLONE_REMOTE:
        log.info("Skipping upload: RCLONE_REMOTE is not configured in %s", CONFIG_FILE)
        return
    if not shutil.which("rclone"):
        log.info("Skipping upload: rclone is not installed")
        return

    log.info("Uploading backup archives to %s", RCLONE_REMOTE)
    subprocess.run(
        ["rclone", "copy", str(RUN_BACKUP_DIR), f"{RCLONE_REMOTE}{DATE_STAMP}/", "--include", "*.tar"],
        check=True,
    )
    log.info("Pruning remote backups older than %d days from %s", RETENTION_DAYS, RCLONE_REMOTE)
    subprocess.run(
        ["rclone", "delete", RCLONE_REMOTE, "--include", "*.tar", "--min-age", f"{RETENTION_DAYS}d"],
        check=True,
    )
    subprocess.run(["rclone", "rmdirs", RCLONE_REMOTE, "--leave-root"])
    log.info("Removing local backup folder after successful upload: %s", RUN_BACKUP_DIR)
    shutil.rmtree(RUN_BACKUP_DIR)

# ---------------------------------------------------------------------------
# Upload — Google Drive service account
# ---------------------------------------------------------------------------

def upload_and_prune_gdrive_service_account():
    if not all([GDRIVE_SA_JSON, GDRIVE_SHARED_DRIVE_ID, GDRIVE_PARENT_FOLDER_ID]):
        log.info(
            "Skipping upload: GDRIVE_SERVICE_ACCOUNT_JSON, GDRIVE_SHARED_DRIVE_ID, "
            "or GDRIVE_PARENT_FOLDER_ID is not configured"
        )
        return

    log.info("Uploading backup archives to Google Shared Drive via service account")

    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = service_account.Credentials.from_service_account_file(
        GDRIVE_SA_JSON, scopes=["https://www.googleapis.com/auth/drive"]
    )
    svc = build("drive", "v3", credentials=creds)

    def _get_or_create_folder(name: str, parent: str) -> str:
        results = svc.files().list(
            q=(f"name='{name}' and '{parent}' in parents "
               f"and mimeType='application/vnd.google-apps.folder' and trashed=false"),
            driveId=GDRIVE_SHARED_DRIVE_ID, includeItemsFromAllDrives=True,
            supportsAllDrives=True, corpora="drive", fields="files(id)",
        ).execute()
        existing = results.get("files", [])
        if existing:
            return existing[0]["id"]
        return svc.files().create(
            body={"name": name,
                  "mimeType": "application/vnd.google-apps.folder",
                  "parents": [parent]},
            supportsAllDrives=True, fields="id",
        ).execute()["id"]

    folder_id = _get_or_create_folder(DATE_STAMP, GDRIVE_PARENT_FOLDER_ID)

    for fpath in sorted(RUN_BACKUP_DIR.glob("*.tar")):
        media = MediaFileUpload(str(fpath), mimetype="application/x-tar", resumable=True)
        svc.files().create(
            body={"name": fpath.name, "parents": [folder_id]},
            media_body=media, supportsAllDrives=True, fields="id",
        ).execute()
        log.info("Uploaded: %s", fpath.name)

    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    old_folders = svc.files().list(
        q=(f"'{GDRIVE_PARENT_FOLDER_ID}' in parents "
           f"and mimeType='application/vnd.google-apps.folder' and trashed=false"),
        driveId=GDRIVE_SHARED_DRIVE_ID, includeItemsFromAllDrives=True,
        supportsAllDrives=True, corpora="drive", fields="files(id,name,createdTime)",
    ).execute().get("files", [])

    for f in old_folders:
        created = datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00"))
        if created < cutoff:
            svc.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
            log.info("Deleted old backup folder: %s", f["name"])

    log.info("Removing local backup folder after successful upload: %s", RUN_BACKUP_DIR)
    shutil.rmtree(RUN_BACKUP_DIR)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    stack_dirs = load_inclusion_list()

    for _, path, _ in stack_dirs:
        if not Path(path).is_dir():
            log.info("Missing required stack directory: %s", path)
            sys.exit(1)

    validate_remote_access()

    required = _estimate_required_bytes(stack_dirs)
    free = _free_bytes_available()
    log.info("Estimated required free space: %d bytes", required)
    log.info("Available free space: %d bytes", free)

    if free < required:
        msg = (
            f"Backup skipped because free disk space is too low. "
            f"Required={required} bytes, available={free} bytes."
        )
        log.info(msg)
        send_alert(msg)
        sys.exit(1)

    for name, path, mode in stack_dirs:
        create_archive_for_stack(name, path, mode)

    log.info("Pruning local backups older than %d days", RETENTION_DAYS)
    cutoff_ts = datetime.now().timestamp() - (RETENTION_DAYS - 1) * 86400
    for d in BACKUP_ROOT.iterdir():
        if d.is_dir() and re.fullmatch(r"\d{8}", d.name) and d.stat().st_mtime < cutoff_ts:
            shutil.rmtree(d)
            log.info("Removed old local backup folder: %s", d.name)

    if UPLOAD_PROVIDER == "rclone":
        upload_and_prune_rclone()
    elif UPLOAD_PROVIDER == "gdrive_service_account":
        upload_and_prune_gdrive_service_account()
    elif not UPLOAD_PROVIDER or UPLOAD_PROVIDER == "none":
        log.info("Skipping upload: UPLOAD_PROVIDER is none")
    else:
        log.info("Unknown UPLOAD_PROVIDER '%s'; skipping upload", UPLOAD_PROVIDER)

    log.info("Backup completed successfully")
    send_alert(
        f"Backup completed successfully on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.\n"
        f"Archives uploaded: {len(stack_dirs)} .tar file(s)."
    )


if __name__ == "__main__":
    main()
