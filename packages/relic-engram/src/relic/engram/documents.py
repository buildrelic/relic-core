"""The Firestore document mirror: the NoSQL half of the engram.

Documents live at ``engram/{workspaceId}/documents/{documentId}`` and carry the
typed body plus a rendered ``markdown`` the web workspace displays and lets a
human edit. The mirror is optional and best-effort: Postgres (``memories.body``)
stays authoritative, so an unconfigured or unreachable Firestore only degrades
the web UI, never an ingest.

The credential env names mirror the landing app's (``FIREBASE_PROJECT_ID``,
``FIREBASE_CLIENT_EMAIL``, ``FIREBASE_PRIVATE_KEY``) so one service account
serves both sides of the seam.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from google.cloud.firestore import AsyncClient

    from relic.config import Settings


def firestore_configured(settings: Settings) -> bool:
    """True when all three service-account fields are set."""
    return bool(
        settings.firebase_project_id
        and settings.firebase_client_email
        and settings.firebase_private_key
    )


class DocumentStore:
    """Thin async wrapper over the Firestore documents collection."""

    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings) -> DocumentStore:
        """Build a client from the shared FIREBASE_* env, initializing the app once."""
        import firebase_admin
        from firebase_admin import credentials, firestore_async

        if not firebase_admin._apps:  # noqa: SLF001 - the documented idempotence check
            cred = credentials.Certificate(
                {
                    "type": "service_account",
                    "project_id": settings.firebase_project_id,
                    "client_email": settings.firebase_client_email,
                    "private_key": settings.firebase_private_key,
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            )
            firebase_admin.initialize_app(cred)
        return cls(firestore_async.client())

    async def write(self, workspace_id: str, document_id: str, payload: dict[str, Any]) -> None:
        """Upsert one document, stamping ``updatedAt``. Merge keeps a human's edits
        to fields this write does not carry."""
        ref = (
            self._client.collection("engram")
            .document(workspace_id)
            .collection("documents")
            .document(document_id)
        )
        await ref.set({**payload, "updatedAt": datetime.now(UTC).isoformat()}, merge=True)
