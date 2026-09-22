"""Read-only Gmail source adapter."""

from __future__ import annotations

import base64
from pathlib import Path
from email.utils import parsedate_to_datetime
from .core import Message, Thread

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _body(part: dict) -> str:
    if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
        raw = part["body"]["data"]
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
    for child in part.get("parts", []):
        value = _body(child)
        if value:
            return value
    return ""


def normalize(raw: dict) -> Thread:
    messages = []
    for item in sorted(raw.get("messages", []), key=lambda x: int(x.get("internalDate", 0))):
        payload = item.get("payload", {})
        headers = {h["name"].lower(): h.get("value", "") for h in payload.get("headers", [])}
        messages.append(Message(str(item["id"]), headers.get("from", ""),
                                headers.get("to", ""), headers.get("subject", ""),
                                _body(payload), item.get("internalDate", "")))
    return Thread("gmail", str(raw["id"]), tuple(messages))


def service(credentials_path: str, token_path: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token = Path(token_path)
    credentials = Credentials.from_authorized_user_file(token, SCOPES) if token.exists() else None
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            credentials = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES).run_local_server(port=0)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(credentials.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=credentials)


def fetch_threads(api, query: str, limit: int) -> list[Thread]:
    if not 1 <= limit <= 50:
        raise ValueError("limit must be 1..50")
    response = api.users().threads().list(userId="me", q=query, maxResults=limit).execute()
    refs = response.get("threads", [])[:limit]
    return [normalize(api.users().threads().get(userId="me", id=ref["id"], format="full").execute())
            for ref in refs]

