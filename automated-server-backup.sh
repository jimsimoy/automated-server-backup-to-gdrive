#!/usr/bin/env bash
set -euo pipefail

CONFIG_FILE="${CONFIG_FILE:-/var/automated-server-backup/automated-server-backup.env}"

if [[ -f "${CONFIG_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${CONFIG_FILE}"
fi

BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/automated-server-backup}"
LOG_FILE="${LOG_FILE:-/var/log/automated-server-backup.log}"
RCLONE_REMOTE="${RCLONE_REMOTE:-}"
RETENTION_DAYS="${RETENTION_DAYS:-4}"
DATE_STAMP="$(date +%Y%m%d)"
RUN_BACKUP_DIR="${BACKUP_ROOT}/${DATE_STAMP}"
ESTIMATE_MARGIN_PERCENT="${ESTIMATE_MARGIN_PERCENT:-20}"
ALERT_PROVIDER="${ALERT_PROVIDER:-}"
ALERT_WEBHOOK_URL="${ALERT_WEBHOOK_URL:-}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
ALERT_TITLE="${ALERT_TITLE:-Automated Server Backup Alert}"
TMP_WORK_ROOT="${TMP_WORK_ROOT:-/var/tmp/automated-server-backup}"
INCLUSION_LIST_FILE="${INCLUSION_LIST_FILE:-/var/automated-server-backup/automated-server-backup-inclusions.txt}"
STACK_DIRS=()

mkdir -p "${BACKUP_ROOT}"
mkdir -p "${RUN_BACKUP_DIR}"
mkdir -p "${TMP_WORK_ROOT}"
touch "${LOG_FILE}"

CURRENT_STOPPED_CONTAINERS=()
RUN_WORK_DIR="${TMP_WORK_ROOT}/${DATE_STAMP}-$$"
mkdir -p "${RUN_WORK_DIR}"

log() {
  echo "[$(date '+%F %T')] $*" | tee -a "${LOG_FILE}"
}

urlencode() {
  python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1"
}

send_alert() {
  local message="$1"

  log "Sending alert via provider: ${ALERT_PROVIDER:-none}"

  case "${ALERT_PROVIDER}" in
    telegram)
      if [[ -z "${TELEGRAM_BOT_TOKEN}" || -z "${TELEGRAM_CHAT_ID}" ]]; then
        log "Telegram alert not sent: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing"
        return 0
      fi
      curl -fsS -X POST \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${ALERT_TITLE}
${message}" >/dev/null || log "Telegram alert send failed"
      ;;
    google_chat)
      if [[ -z "${ALERT_WEBHOOK_URL}" ]]; then
        log "Google Chat alert not sent: ALERT_WEBHOOK_URL is missing"
        return 0
      fi
      python3 - <<'PY' "${ALERT_WEBHOOK_URL}" "${ALERT_TITLE}" "${message}" || log "Google Chat alert send failed"
import json, sys, urllib.request
url, title, message = sys.argv[1], sys.argv[2], sys.argv[3]
payload = json.dumps({"text": f"{title}\n{message}"}).encode()
req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json; charset=UTF-8"})
urllib.request.urlopen(req, timeout=15).read()
PY
      ;;
    teams)
      if [[ -z "${ALERT_WEBHOOK_URL}" ]]; then
        log "Teams alert not sent: ALERT_WEBHOOK_URL is missing"
        return 0
      fi
      python3 - <<'PY' "${ALERT_WEBHOOK_URL}" "${ALERT_TITLE}" "${message}" || log "Teams alert send failed"
import json, sys, urllib.request
url, title, message = sys.argv[1], sys.argv[2], sys.argv[3]
payload = json.dumps({"text": f"{title}\n{message}"}).encode()
req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
urllib.request.urlopen(req, timeout=15).read()
PY
      ;;
    ""|none)
      log "No alert provider configured; alert message was: ${message}"
      ;;
    *)
      log "Unknown ALERT_PROVIDER '${ALERT_PROVIDER}'"
      ;;
  esac
}

validate_remote_access() {
  if [[ -z "${RCLONE_REMOTE}" ]]; then
    log "Skipping remote access validation: RCLONE_REMOTE is not configured"
    return 0
  fi

  if ! command -v rclone >/dev/null 2>&1; then
    log "Skipping remote access validation: rclone is not installed"
    return 0
  fi

  log "Validating remote access for ${RCLONE_REMOTE}"
  if ! rclone lsf "${RCLONE_REMOTE}" >/dev/null 2>&1; then
    message="Backup stopped because Google Drive / rclone access validation failed for ${RCLONE_REMOTE}. OAuth or remote access may need to be reconnected."
    log "${message}"
    send_alert "${message}"
    exit 1
  fi
}

cleanup() {
  if [[ ${#CURRENT_STOPPED_CONTAINERS[@]} -gt 0 ]]; then
    for container in "${CURRENT_STOPPED_CONTAINERS[@]}"; do
      log "Ensuring container is running again: ${container}"
      docker start "${container}" >/dev/null 2>&1 || log "Failed to restart ${container}; manual check needed"
    done
  fi
  rm -rf "${RUN_WORK_DIR}"
}

trap cleanup EXIT

container_is_running() {
  local container="$1"
  local status=""

  status="$(docker inspect -f '{{.State.Running}}' "${container}" 2>/dev/null || echo false)"
  [[ "${status}" == "true" ]]
}

stop_container_if_running() {
  local container="$1"

  if container_is_running "${container}"; then
    log "Stopping container for consistent backup: ${container}"
    docker stop "${container}" >/dev/null
    CURRENT_STOPPED_CONTAINERS+=("${container}")
  else
    log "Container already stopped, leaving as-is: ${container}"
  fi
}

start_stopped_containers() {
  local container
  if [[ ${#CURRENT_STOPPED_CONTAINERS[@]} -eq 0 ]]; then
    return 0
  fi

  for container in "${CURRENT_STOPPED_CONTAINERS[@]}"; do
    log "Starting container after backup: ${container}"
    docker start "${container}" >/dev/null
  done
  CURRENT_STOPPED_CONTAINERS=()
}

create_mariadb_dump() {
  local dump_dir="${RUN_WORK_DIR}/docker-couchdb-extra"
  local dump_path="${dump_dir}/mariadb-all-databases-${DATE_STAMP}.sql"

  mkdir -p "${dump_dir}"
  log "Creating MariaDB logical dump: ${dump_path}"
  docker exec mariadb sh -lc 'exec mariadb-dump -uroot -p"$MYSQL_ROOT_PASSWORD" --all-databases --single-transaction --quick --lock-tables=false' > "${dump_path}"
}

run_tar_command() {
  local stderr_log="${RUN_WORK_DIR}/tar-stderr-$$.log"
  local status=0

  set +e
  tar "$@" 2> >(tee "${stderr_log}" >&2)
  status=$?
  set -e

  if [[ ${status} -eq 0 ]]; then
    rm -f "${stderr_log}"
    return 0
  fi

  if [[ ${status} -eq 1 ]] && [[ -f "${stderr_log}" ]]; then
    if grep -Ev "^(tar: Removing leading . from member names|tar: Removing leading . from hard link targets|tar: .*: file changed as we read it)$" "${stderr_log}" >/dev/null 2>&1; then
      log "tar returned warning exit code with non-benign messages"
    else
      log "tar returned warning exit code only because files changed during read; continuing"
      rm -f "${stderr_log}"
      return 0
    fi
  fi

  rm -f "${stderr_log}"
  return "${status}"
}

create_archive_for_stack() {
  local name="$1"
  local dir="$2"
  local mode="$3"
  local archive_path="${RUN_BACKUP_DIR}/${name}-${DATE_STAMP}.tar"

  case "${mode}" in
    couchdb_main)
      create_mariadb_dump
      stop_container_if_running "mariadb"
      stop_container_if_running "couchdb"
      log "Creating backup archive with MariaDB dump and stopped CouchDB data: ${archive_path}"
      run_tar_command \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='mariadb_data' \
        -cf "${archive_path}" \
        "${dir}" \
        -C "${RUN_WORK_DIR}" docker-couchdb-extra
      start_stopped_containers
      ;;
    couchdb_stack)
      stop_container_if_running "couchdb_${name%-couchdb}"
      log "Creating backup archive while stack container is stopped: ${archive_path}"
      run_tar_command \
        --exclude='.git' \
        --exclude='__pycache__' \
        -cf "${archive_path}" \
        "${dir}"
      start_stopped_containers
      ;;
    direct)
      log "Creating backup archive for included path: ${archive_path}"
      run_tar_command \
        --exclude='.git' \
        --exclude='__pycache__' \
        -cf "${archive_path}" \
        "${dir}"
      ;;
    *)
      log "Invalid backup mode '${mode}' for inclusion '${name}'"
      exit 1
      ;;
  esac
}

load_inclusion_list() {
  local line=""
  local name=""
  local dir=""
  local mode=""

  if [[ ! -f "${INCLUSION_LIST_FILE}" ]]; then
    log "Missing inclusion list file: ${INCLUSION_LIST_FILE}"
    exit 1
  fi

  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"

    if [[ -z "${line}" || "${line}" == \#* ]]; then
      continue
    fi

    if [[ "${line}" != *:*:* ]]; then
      log "Invalid inclusion list entry (expected name:path:mode): ${line}"
      exit 1
    fi

    name="${line%%:*}"
    mode="${line##*:}"
    dir="${line#*:}"
    dir="${dir%:*}"

    if [[ -z "${name}" || -z "${dir}" || -z "${mode}" ]]; then
      log "Invalid inclusion list entry (empty name, path, or mode): ${line}"
      exit 1
    fi

    case "${mode}" in
      couchdb_main|couchdb_stack|direct)
        ;;
      *)
        log "Invalid inclusion list mode '${mode}' in entry: ${line}"
        exit 1
        ;;
    esac

    if [[ "${mode}" == "couchdb_main" && "${name}" != "docker-couchdb" ]]; then
      log "couchdb_main mode is currently only supported for docker-couchdb: ${line}"
      exit 1
    fi

    STACK_DIRS+=("${name}:${dir}:${mode}")
  done < "${INCLUSION_LIST_FILE}"

  if [[ ${#STACK_DIRS[@]} -eq 0 ]]; then
    log "Inclusion list is empty: ${INCLUSION_LIST_FILE}"
    exit 1
  fi
}

latest_backup_size_or_dir_size() {
  local name="$1"
  local dir="$2"
  local latest_backup=""

  latest_backup="$(find "${BACKUP_ROOT}" -mindepth 2 -maxdepth 2 -type f -name "${name}-*.tar" -printf '%T@ %s %p\n' 2>/dev/null | sort -nr | head -n1 | awk '{print $2}')"
  if [[ -n "${latest_backup}" ]]; then
    echo "${latest_backup}"
    return 0
  fi

  du -sb "${dir}" | awk '{print $1}'
}

estimate_required_bytes() {
  local total=0
  local current=0
  local estimated=0
  local name dir mode

  for entry in "${STACK_DIRS[@]}"; do
    name="${entry%%:*}"
    mode="${entry##*:}"
    dir="${entry#*:}"
    dir="${dir%:*}"
    current="$(latest_backup_size_or_dir_size "${name}" "${dir}")"
    total=$((total + current))
  done

  estimated=$(( total + (total * ESTIMATE_MARGIN_PERCENT / 100) ))
  echo "${estimated}"
}

free_bytes_available() {
  df -PB1 "${BACKUP_ROOT}" | awk 'NR==2 {print $4}'
}

load_inclusion_list

for entry in "${STACK_DIRS[@]}"; do
  dir="${entry#*:}"
  dir="${dir%:*}"
  if [[ ! -d "${dir}" ]]; then
    log "Missing required stack directory: ${dir}"
    exit 1
  fi
done

validate_remote_access

required_bytes="$(estimate_required_bytes)"
free_bytes="$(free_bytes_available)"

log "Estimated required free space: ${required_bytes} bytes"
log "Available free space: ${free_bytes} bytes"

if (( free_bytes < required_bytes )); then
  message="Backup skipped because free disk space is too low. Required=${required_bytes} bytes, available=${free_bytes} bytes."
  log "${message}"
  send_alert "${message}"
  exit 1
fi

for entry in "${STACK_DIRS[@]}"; do
  name="${entry%%:*}"
  mode="${entry##*:}"
  dir="${entry#*:}"
  dir="${dir%:*}"
  create_archive_for_stack "${name}" "${dir}" "${mode}"
done

log "Pruning local backups older than ${RETENTION_DAYS} days"
find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -regextype posix-extended -regex ".*/[0-9]{8}" -mtime +"$((RETENTION_DAYS - 1))" -exec rm -rf {} +

if [[ -z "${RCLONE_REMOTE}" ]]; then
  log "Skipping upload: RCLONE_REMOTE is not configured in ${CONFIG_FILE}"
  exit 0
fi

if ! command -v rclone >/dev/null 2>&1; then
  log "Skipping upload: rclone is not installed"
  exit 0
fi

log "Uploading backup archives to ${RCLONE_REMOTE}"
rclone copy "${RUN_BACKUP_DIR}" "${RCLONE_REMOTE}${DATE_STAMP}/" --include '*.tar'

log "Pruning remote backups older than ${RETENTION_DAYS} days from ${RCLONE_REMOTE}"
rclone delete "${RCLONE_REMOTE}" --include '*.tar' --min-age "${RETENTION_DAYS}d"
rclone rmdirs "${RCLONE_REMOTE}" --leave-root || true

log "Removing local backup folder after successful upload: ${RUN_BACKUP_DIR}"
rm -rf "${RUN_BACKUP_DIR}"

log "Backup completed successfully"
