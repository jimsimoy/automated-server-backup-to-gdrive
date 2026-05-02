# automated-server-backup

A Python script for automated, scheduled server directory backups with optional cloud upload and alerting.

**Author:** Jan Ivan Simoy (jimsimoy@gmail.com)
**Powered by AI**

---

## Features

- Backs up any list of directories into dated `.tar` archives
- Special handling for CouchDB stacks — briefly stops containers for consistent backup
- Creates a MariaDB logical dump before archiving the main CouchDB stack
- Estimates required disk space before running; aborts with alert if space is too low
- Uploads archives to Google Drive via **rclone** or a **Google service account** (switchable)
- Prunes old backups locally and remotely based on a configurable retention window
- Sends alerts on backup success and failure via Telegram, Google Chat, or Microsoft Teams
- Restarts any stopped containers automatically on unexpected exit

---

## Project Structure

```
automated-server-backup-to-gdrive/
├── automated-server-backup.py        # Main backup script (Python)
├── .env                              # Live config (gitignored — copy from .env.example)
├── .env.example                      # Config template
├── directory-inclusions.txt          # Live inclusion list (gitignored — copy from .sample)
├── directory-inclusions.sample.txt   # Inclusion list template
├── venv/                             # Python virtualenv (gitignored)
├── secrets/                          # Service account key files (gitignored)
├── tmp/                              # Temporary backup staging area (gitignored)
├── git-commit.sh                     # Helper: commit + push
├── git-pull-current.sh               # Helper: pull current branch
└── git-push-current.sh               # Helper: push current branch
```

---

## Installation

### 1. Clone the project to the server

```bash
git clone <repo-url> /var/server_scripts/automated-server-backup-to-gdrive
cd /var/server_scripts/automated-server-backup-to-gdrive
```

### 2. Create the Python virtualenv and install dependencies

```bash
apt install -y python3.9 python3.9-venv python3.9-dev
python3.9 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install google-api-python-client google-auth
```

### 3. Create the config file

```bash
cp .env.example .env
```

Edit `.env` and fill in the required values (upload provider, credentials, alert settings).

### 4. Create the inclusion list

```bash
cp directory-inclusions.sample.txt directory-inclusions.txt
```

Edit `directory-inclusions.txt` to list the directories to back up (see format below).

### 5. Set permissions

```bash
chmod 750 automated-server-backup.py
chmod 640 .env
mkdir -p tmp
```

### 6. Install the cron job

```bash
crontab -e
```

Add a line such as:

```
30 2 * * * /var/server_scripts/automated-server-backup-to-gdrive/venv/bin/python3 /var/server_scripts/automated-server-backup-to-gdrive/automated-server-backup.py >> /var/log/automated-server-backup.log 2>&1
```

---

## Upload Providers

Set `UPLOAD_PROVIDER` in `.env` to choose how backups are uploaded.

### Option A — rclone (default)

```bash
UPLOAD_PROVIDER=rclone
RCLONE_REMOTE=gdrive:server-backups
```

Configure rclone first:

```bash
rclone config
```

Then set `RCLONE_REMOTE` to the configured remote name and folder path.

---

### Option B — Google Service Account (Shared Drive)

```bash
UPLOAD_PROVIDER=gdrive_service_account
GDRIVE_SERVICE_ACCOUNT_JSON=/var/server_scripts/automated-server-backup-to-gdrive/secrets/your-key.json
GDRIVE_SHARED_DRIVE_ID=<shared-drive-id>
GDRIVE_PARENT_FOLDER_ID=<folder-id>
```

**Setup steps:**

1. In [Google Cloud Console](https://console.cloud.google.com), create a **Service Account** under your project.
2. Create and download a **JSON key** for it. Place the file inside the `secrets/` directory.
3. Enable the **Google Drive API** for the project:
   `APIs & Services → Library → Google Drive API → Enable`
4. In Google Drive, open the **Shared Drive**, click **Manage members**, and add the service account email as a **Contributor** (or higher).
5. Get the IDs from the folder URL:
   `https://drive.google.com/drive/folders/<FOLDER_ID>`
   - `GDRIVE_PARENT_FOLDER_ID` = the folder ID from the URL above
   - `GDRIVE_SHARED_DRIVE_ID` = the ID of the Shared Drive that contains the folder (query the API or check the Shared Drive root URL)

---

### Option C — No upload

```bash
UPLOAD_PROVIDER=none
```

Backups are created and retained locally only.

---

## Telegram Alert Setup

### Step 1 — Create a bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Follow the prompts — give it a name and a username (must end in `bot`)
4. BotFather replies with your **bot token**, e.g.:
   ```
   7812345678:AAFxxxxxxxxxxxxxxxxxxxxxx
   ```

### Step 2 — Get your Chat ID

1. Send any message to your new bot in Telegram (it must receive at least one message first)
2. Run this on the server, replacing `<TOKEN>` with your bot token:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
   ```
3. Find the `"chat"` object in the response — the `"id"` inside it is your `TELEGRAM_CHAT_ID`:
   ```json
   "chat": {
     "id": 123456789,
     ...
   }
   ```
   > For a **group chat**, the ID will be a negative number (e.g. `-987654321`).

### Step 3 — Set values in `.env`

```bash
ALERT_PROVIDER=telegram
TELEGRAM_BOT_TOKEN=7812345678:AAFxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=123456789
ALERT_TITLE="[Server] Backup Alert"
```

### Step 4 — Test the connection

```bash
curl -s -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  --data-urlencode "chat_id=<CHAT_ID>" \
  --data-urlencode "text=Test alert from backup script"
```

A response with `"ok": true` confirms it is working.

---

## Inclusion List Format

Edit `directory-inclusions.txt`. Each non-comment line must follow:

```
name:/absolute/path:mode
```

### Modes

| Mode | Behaviour |
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
| `RETENTION_DAYS` | `4` | Days to keep backups locally and remotely |
| `ESTIMATE_MARGIN_PERCENT` | `20` | Safety margin added to disk space estimate |
| `UPLOAD_PROVIDER` | `rclone` | `rclone`, `gdrive_service_account`, or `none` |
| `RCLONE_REMOTE` | _(empty)_ | rclone remote and path; used when `UPLOAD_PROVIDER=rclone` |
| `GDRIVE_SERVICE_ACCOUNT_JSON` | _(empty)_ | Path to service account JSON key file |
| `GDRIVE_SHARED_DRIVE_ID` | _(empty)_ | Google Shared Drive ID |
| `GDRIVE_PARENT_FOLDER_ID` | _(empty)_ | Parent folder ID inside the shared drive |
| `ALERT_PROVIDER` | _(empty)_ | `telegram`, `google_chat`, `teams`, or empty for no alerts |
| `ALERT_WEBHOOK_URL` | _(empty)_ | Webhook URL for Google Chat or Teams |
| `TELEGRAM_BOT_TOKEN` | _(empty)_ | Telegram bot token |
| `TELEGRAM_CHAT_ID` | _(empty)_ | Telegram chat or user ID |
| `ALERT_TITLE` | `Automated Server Backup Alert` | Prefix added to all alert messages |
| `INCLUSION_LIST_FILE` | `/var/automated-server-backup/directory-inclusions.txt` | Path to the inclusion list |

---

## How Backups Work

1. Loads config from `.env`
2. Loads the inclusion list and validates all paths exist
3. Validates remote access for the configured upload provider (aborts with alert on failure)
4. Estimates required disk space; aborts with alert if free space is insufficient
5. Creates one `.tar` archive per inclusion into `BACKUP_ROOT/YYYYMMDD/`
6. Prunes local backup folders older than `RETENTION_DAYS`
7. Uploads today's archives to the configured remote
8. Prunes remote backups older than `RETENTION_DAYS`
9. Removes the local dated folder after a successful upload

> If the script exits unexpectedly while a container is stopped, the cleanup trap restarts it automatically.

---

## Prerequisites

| Requirement | Purpose |
|---|---|
| `python3.9+` + `venv` | Script runtime and virtualenv |
| `tar` | Archive creation |
| `docker` | Container stop/start for CouchDB modes |
| `google-api-python-client` | Google Drive service account upload (installed in `venv/`) |
| `google-auth` | Service account authentication (installed in `venv/`) |
| `rclone` | Cloud upload when `UPLOAD_PROVIDER=rclone` (optional) |

---

## Local-Only Files (gitignored)

These files contain server-specific configuration and are never committed:

| File / Directory | Description |
|---|---|
| `.env` | Live config with credentials |
| `directory-inclusions.txt` | Live inclusion list with server-specific paths |
| `secrets/` | Service account key files |
| `venv/` | Python virtualenv |
| `tmp/` | Temporary backup staging area |

Use `.env.example` and `directory-inclusions.sample.txt` as starting templates on each new server.

---

## License

MIT
