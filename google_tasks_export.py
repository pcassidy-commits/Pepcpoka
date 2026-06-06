"""
Google Tasks → Google Drive hourly CSV exporter.

Reads from 5 hard-coded task lists and uploads a timestamped CSV to a
Google Drive folder every hour.  Designed to run 24/7 as a long-lived
process (e.g. via systemd or Docker).

Authentication flow
-------------------
First run: OAuth consent screen opens in a browser and writes a token
file (token.json) next to this script.  Subsequent runs reuse that token
and refresh it automatically.

Required environment variables
-------------------------------
GOOGLE_OAUTH_CREDENTIALS  - path to the OAuth 2.0 client_secret JSON
                             downloaded from Google Cloud Console
                             (defaults to "credentials.json")
EXPORT_FOLDER_NAME        - Drive folder name to upload into
                             (defaults to "Google Tasks Exports")
TOKEN_PATH                - path where the OAuth token is cached
                             (defaults to "token.json")
"""

import csv
import io
import logging
import os
import sys
import time
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/tasks.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

TASK_LISTS: dict[str, str] = {
    "MTA1OTgyNTI5Nzg2NjU4Mjc2NDQ6MDow": "My Tasks",
    "cEZCRURYZ0RwME5XM1dSNw": "TTE",
    "TEtudFlGaGYyVmNiQ0xZWg": "Poka general",
    "SXB3NW9lSU0zNnYtaEYzMg": "Product/Manufacturing",
    "UVZ3RG5EVmp3WEp4TGZaXw": "Lessons etc",
}

CREDENTIALS_PATH = os.environ.get("GOOGLE_OAUTH_CREDENTIALS", "credentials.json")
TOKEN_PATH = os.environ.get("TOKEN_PATH", "token.json")
EXPORT_FOLDER_NAME = os.environ.get("EXPORT_FOLDER_NAME", "Google Tasks Exports")

CSV_COLUMNS = ["Task Title", "Due Date", "Notes", "Completed", "List Name"]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def authenticate_google_services() -> tuple:
    """Return (tasks_service, drive_service) authenticated clients.

    Uses cached token.json when available; triggers OAuth browser flow on
    first run.
    """
    creds = None

    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing expired OAuth token.")
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise FileNotFoundError(
                    f"OAuth credentials file not found: {CREDENTIALS_PATH}. "
                    "Download it from Google Cloud Console and set "
                    "GOOGLE_OAUTH_CREDENTIALS accordingly."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "w") as fh:
            fh.write(creds.to_json())
        log.info("OAuth token saved to %s", TOKEN_PATH)

    tasks_service = build("tasks", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)
    return tasks_service, drive_service


def fetch_all_tasks_from_lists(tasks_service, list_ids: dict[str, str]) -> list[dict]:
    """Return a flat list of task dicts fetched from every list in list_ids.

    list_ids maps {task_list_id: human_readable_name}.
    Completed tasks are included.  Pagination is handled automatically.
    Network/API errors on a single list are logged and skipped so the rest
    of the export still succeeds.
    """
    all_tasks: list[dict] = []

    for list_id, list_name in list_ids.items():
        log.info("Fetching tasks from list: %s (%s)", list_name, list_id)
        page_token = None
        list_task_count = 0

        while True:
            try:
                response = (
                    tasks_service.tasks()
                    .list(
                        tasklist=list_id,
                        showCompleted=True,
                        showHidden=True,
                        maxResults=100,
                        pageToken=page_token,
                    )
                    .execute()
                )
            except HttpError as exc:
                log.error(
                    "HTTP %s fetching list %s (%s) — skipping: %s",
                    exc.resp.status,
                    list_name,
                    list_id,
                    exc,
                )
                break
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "Unexpected error fetching list %s (%s) — skipping: %s",
                    list_name,
                    list_id,
                    exc,
                )
                break

            items = response.get("items", [])
            for item in items:
                all_tasks.append(
                    {
                        "title": item.get("title", "").strip(),
                        "due_date": _parse_due(item.get("due")),
                        "notes": item.get("notes", "").strip(),
                        "completed": item.get("status") == "completed",
                        "task_list_name": list_name,
                    }
                )
                list_task_count += 1

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        log.info("  → %d task(s) from %s", list_task_count, list_name)

    log.info("Total tasks fetched: %d", len(all_tasks))
    return all_tasks


def _parse_due(due_str: str | None) -> str:
    """Convert RFC 3339 due date string to YYYY-MM-DD, or empty string."""
    if not due_str:
        return ""
    try:
        # Google Tasks returns dates like "2024-05-01T00:00:00.000Z"
        return due_str[:10]
    except Exception:  # noqa: BLE001
        return due_str


def format_tasks_as_csv(tasks: list[dict]) -> str:
    """Return a CSV string with header row from the task list."""
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=CSV_COLUMNS,
        quoting=csv.QUOTE_ALL,
        lineterminator="\n",
    )
    writer.writeheader()

    for task in tasks:
        writer.writerow(
            {
                "Task Title": task["title"],
                "Due Date": task["due_date"],
                "Notes": task["notes"],
                "Completed": "Yes" if task["completed"] else "No",
                "List Name": task["task_list_name"],
            }
        )

    return output.getvalue()


def _get_or_create_folder(drive_service, folder_name: str) -> str:
    """Return the Drive folder ID, creating it if it does not exist."""
    query = (
        f"name='{folder_name}' "
        "and mimeType='application/vnd.google-apps.folder' "
        "and trashed=false"
    )
    results = (
        drive_service.files()
        .list(q=query, spaces="drive", fields="files(id, name)")
        .execute()
    )
    files = results.get("files", [])
    if files:
        folder_id = files[0]["id"]
        log.info("Using existing Drive folder '%s' (id=%s)", folder_name, folder_id)
        return folder_id

    folder_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    folder = drive_service.files().create(body=folder_metadata, fields="id").execute()
    folder_id = folder["id"]
    log.info("Created Drive folder '%s' (id=%s)", folder_name, folder_id)
    return folder_id


def upload_to_drive(drive_service, csv_content: str, filename: str) -> str:
    """Upload csv_content as filename inside EXPORT_FOLDER_NAME.

    Returns the Drive file ID on success.
    Raises on failure (let the caller decide how to handle it).
    """
    folder_id = _get_or_create_folder(drive_service, EXPORT_FOLDER_NAME)

    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaIoBaseUpload(
        io.BytesIO(csv_content.encode("utf-8")),
        mimetype="text/csv",
        resumable=False,
    )
    file = (
        drive_service.files()
        .create(body=file_metadata, media_body=media, fields="id, name, webViewLink")
        .execute()
    )
    log.info(
        "Uploaded '%s' to Drive (id=%s) — %s",
        file["name"],
        file["id"],
        file.get("webViewLink", "no link"),
    )
    return file["id"]


# ---------------------------------------------------------------------------
# Scheduler job
# ---------------------------------------------------------------------------

# Module-level service handles reused across runs (refreshed inside
# authenticate_google_services when the token expires)
_tasks_service = None
_drive_service = None


def scheduled_export_job() -> None:
    """Orchestrates one full fetch-format-upload cycle."""
    global _tasks_service, _drive_service  # noqa: PLW0603

    log.info("=== Export job started ===")

    try:
        if _tasks_service is None or _drive_service is None:
            _tasks_service, _drive_service = authenticate_google_services()

        tasks = fetch_all_tasks_from_lists(_tasks_service, TASK_LISTS)

        if not tasks:
            log.warning("No tasks found across all lists — uploading empty CSV.")

        csv_content = format_tasks_as_csv(tasks)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"google_tasks_{timestamp}.csv"

        upload_to_drive(_drive_service, csv_content, filename)
        log.info("=== Export job completed successfully ===")

    except FileNotFoundError as exc:
        log.critical("Configuration error — stopping: %s", exc)
        sys.exit(1)

    except HttpError as exc:
        if exc.resp.status in (401, 403):
            # Token may have been revoked; force re-auth on next run
            log.error("Auth error (%s) — will re-authenticate on next run.", exc.resp.status)
            _tasks_service = None
            _drive_service = None
        else:
            log.error("Google API error during export: %s", exc)

    except Exception as exc:  # noqa: BLE001
        log.error("Unexpected error during export: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Authenticate once, run an immediate export, then schedule hourly runs."""
    log.info("Initialising Google Tasks hourly exporter.")

    # Validate credentials exist before blocking on the scheduler
    global _tasks_service, _drive_service  # noqa: PLW0603
    _tasks_service, _drive_service = authenticate_google_services()

    # Run once immediately so we don't wait an hour for the first export
    scheduled_export_job()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(scheduled_export_job, "interval", hours=1, id="tasks_export")

    log.info("Scheduler started — exporting every hour. Press Ctrl-C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
