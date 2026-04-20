# automated-server-backup

A Bash script for automated, scheduled server directory backups with optional cloud upload and alerting.

**Author:** Jan Ivan Simoy — jimsimoy@gmail.com

---

## Features

- Backs up any list of directories into dated `.tar` archives
- Special handling for CouchDB stacks — briefly stops containers for consistent backup
- Creates a MariaDB logical dump before archiving the main CouchDB stack
- Estimates required disk space before running; aborts with alert if space is too low
- Uploads archives to any rclone-supported remote (Google Drive, S3, etc.)
- Prunes old backups locally and remotely based on configurable retention window
- Sends failure alerts via Telegram, Google Chat, or Microsoft Teams
- Restarts any stopped containers automatically on unexpected exit

---

## Project Structure

```
automated-server-backup/
├── automated-server-backup.sh              # Main backup script
├── automated-server-backup.env             # Live config (gitignored — copy from .env.example)
├── automated-server-backup.env.example     # Config template
├── automated-server-backup-inclusions.txt  # Live inclusion list (gitignored — copy from .sample)
├── automated-server-backup-inclusions.sample.txt  # Inclusion list template
├── git-commit.sh                           # Helper: commit + push
├── git-pull-current.sh                     # Helper: pull current branch
└── git-push-current.sh                     # Helper: push current branch
```

---

## Installation

1. **Clone or copy this project to the server:**
   ```bash
   git clone <repo-url> /var/automated-server-backup
   ```

2. **Create the config file from the example:**
   ```bash
   cp automated-server-backup.env.example automated-server-backup.env
   ```
   Then fill in `RCLONE_REMOTE`, alert credentials, and other values.

3. **Create the inclusion list from the sample:**
   ```bash
   cp automated-server-backup-inclusions.sample.txt automated-server-backup-inclusions.txt
   ```
   Then edit it to list the directories you want to back up.

4. **Set permissions:**
   ```bash
   chmod 750 /var/automated-server-backup/automated-server-backup.sh
   chmod 640 /var/automated-server-backup/automated-server-backup.env
   mkdir -p /var/backups/automated-server-backup /var/tmp/automated-server-backup
   touch /var/log/automated-server-backup.log
   ```

5. **Install the cron job:**
   ```bash
   cat > /etc/cron.d/automated-server-backup << 'EOF'
   SHELL=/bin/bash
   PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

   # Run daily at 02:30 server time
   30 2 * * * root /var/automated-server-backup/automated-server-backup.sh >> /var/log/automated-server-backup.log 2>&1
   EOF
   ```

6. **Configure rclone** (if using cloud upload):
   ```bash
   rclone config
   ```
   Then set `RCLONE_REMOTE` in `automated-server-backup.env` to the configured remote name and path.

---

## Inclusion List Format

Each non-comment line in `automated-server-backup-inclusions.txt` must follow this format:

```
name:/absolute/path:mode
```

### Supported modes

| Mode | Behavior |
|---|---|
| `direct` | Archives the directory without any container interaction |
| `couchdb_stack` | Stops the `couchdb_<name>` container, archives, then restarts it |
| `couchdb_main` | Dumps MariaDB first, stops `mariadb` + `couchdb`, archives, then restarts both |

> `couchdb_main` is only valid for an entry named `docker-couchdb`.

### Example

```
docker-couchdb:/var/docker-couchdb:couchdb_main
app-couchdb:/var/app-couchdb:couchdb_stack
app-data:/srv/app-data:direct
ops:/var/ops:direct
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `BACKUP_ROOT` | `/var/backups/automated-server-backup` | Local directory where dated backup folders are created |
| `LOG_FILE` | `/var/log/automated-server-backup.log` | Log file path |
| `RETENTION_DAYS` | `4` | Number of days to keep backups locally and remotely |
| `ESTIMATE_MARGIN_PERCENT` | `20` | Safety margin added to disk space estimate |
| `RCLONE_REMOTE` | _(empty)_ | rclone remote and path for cloud upload; skipped if empty |
| `ALERT_PROVIDER` | _(empty)_ | `telegram`, `google_chat`, `teams`, or empty for no alerts |
| `ALERT_WEBHOOK_URL` | _(empty)_ | Webhook URL for Google Chat or Teams alerts |
| `TELEGRAM_BOT_TOKEN` | _(empty)_ | Telegram bot token |
| `TELEGRAM_CHAT_ID` | _(empty)_ | Telegram chat or user ID |
| `ALERT_TITLE` | `Automated Server Backup Alert` | Prefix added to alert messages |
| `INCLUSION_LIST_FILE` | `/var/automated-server-backup/automated-server-backup-inclusions.txt` | Path to the inclusion list |

---

## How Backups Work

1. Loads config from `automated-server-backup.env`
2. Loads the inclusion list and validates all paths exist
3. Validates rclone remote access (aborts with alert if unreachable)
4. Estimates required disk space; aborts with alert if free space is insufficient
5. Creates one `.tar` archive per inclusion into `/var/backups/automated-server-backup/YYYYMMDD/`
6. Prunes local backup folders older than `RETENTION_DAYS`
7. Uploads today's archives to `RCLONE_REMOTE` via rclone
8. Prunes remote backups older than `RETENTION_DAYS`
9. Removes the local dated folder after a successful upload

> If the script exits unexpectedly while a container is stopped, the cleanup trap restarts it automatically.

---

## Prerequisites

| Requirement | Purpose |
|---|---|
| `bash` 4+ | Script runtime |
| `tar` | Archive creation |
| `docker` | Container stop/start for CouchDB modes |
| `python3` | URL encoding and webhook HTTP calls |
| `rclone` | Cloud upload (optional) |
| `curl` | Telegram alerts |

---

## Alerts

Configure one of the following providers in `automated-server-backup.env`:

- **Telegram** — set `ALERT_PROVIDER=telegram`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID`
- **Google Chat** — set `ALERT_PROVIDER=google_chat` and `ALERT_WEBHOOK_URL`
- **Microsoft Teams** — set `ALERT_PROVIDER=teams` and `ALERT_WEBHOOK_URL`

Alerts are sent when:
- Free disk space is too low to proceed
- rclone remote access validation fails

---

## Local-Only Files (gitignored)

These files contain server-specific configuration and are not committed to the repository:

- `automated-server-backup.env` — live config with credentials
- `automated-server-backup-inclusions.txt` — live inclusion list with server-specific paths
- `automated-server-backup-replication-prompt.md` — generated replication notes

Use the provided `.example` and `.sample` files as starting templates on each new server.

---

## License

MIT
