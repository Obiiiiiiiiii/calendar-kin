"""Kindroid REST client (phase 2).

Reads new chat messages via GET /get-chat-messages with the
`start_after_timestamp` cursor so no message is processed twice. This tool
never writes to Kindroid — /update-info is deliberately unused because the
tool must never touch the kin's profile.

NOTE: the exact message field names are normalized defensively below; verify
them against the live API on first run (the brief documents the endpoint and
cursor, not the payload shape).
"""

from __future__ import annotations

from typing import List, Optional

import requests

BASE_URL = "https://api.kindroid.ai/v1"
PAGE_LIMIT = 100  # API max per the brief
MAX_PAGES = 10  # safety cap per scan cycle


class KindroidClient:
    def __init__(self, api_key: str, ai_id: str, base_url: str = BASE_URL):
        self.api_key = api_key
        self.ai_id = ai_id
        self.base_url = base_url

    def get_new_messages(self, start_after_timestamp: Optional[str] = None) -> List[dict]:
        """All messages newer than the cursor, oldest first, normalized.

        Pages until caught up so infrequent polls can't silently miss
        messages beyond the per-request limit.
        """
        messages: List[dict] = []
        cursor = start_after_timestamp
        for _ in range(MAX_PAGES):
            page = [normalize_message(m) for m in self._fetch_page(cursor)]
            messages.extend(page)
            if len(page) < PAGE_LIMIT:
                break
            cursor = last_timestamp(page) or cursor
        return messages

    def _fetch_page(self, start_after_timestamp: Optional[str]) -> List[dict]:
        params: dict = {"ai_id": self.ai_id, "limit": PAGE_LIMIT}
        if start_after_timestamp:
            params["start_after_timestamp"] = start_after_timestamp
        response = requests.get(
            f"{self.base_url}/get-chat-messages",
            headers={"Authorization": f"Bearer {self.api_key}"},
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else payload.get("messages", [])


def normalize_message(raw: dict) -> dict:
    """Map whichever field names the API uses onto {timestamp, sender, text}."""
    timestamp = raw.get("timestamp") or raw.get("created_at") or raw.get("sent_at") or ""
    sender = raw.get("sender") or raw.get("role") or raw.get("author") or "unknown"
    text = raw.get("message") or raw.get("text") or raw.get("content") or ""
    return {"timestamp": str(timestamp), "sender": str(sender), "text": str(text)}


def format_transcript(messages: List[dict]) -> str:
    """Plain-text transcript, oldest first, for the mention-detection prompt."""
    return "\n".join(f"[{m['sender']}] {m['text']}" for m in messages if m["text"])


def last_timestamp(messages: List[dict]) -> Optional[str]:
    """Cursor value for the next poll."""
    stamps = [m["timestamp"] for m in messages if m["timestamp"]]
    return stamps[-1] if stamps else None
