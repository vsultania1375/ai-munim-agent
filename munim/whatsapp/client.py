from __future__ import annotations

import asyncio
import random
import ssl
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


class WhatsAppError(RuntimeError):
    """Base class for WhatsApp transport errors."""


class WhatsAppOutboundDisabled(WhatsAppError):
    """Raised when code tries to send without explicit outbound enablement."""


class WhatsAppRateLimitError(WhatsAppError):
    """Meta returned a rate-limit response."""


class WhatsAppTransientError(WhatsAppError):
    """Meta returned a transient transport/server failure."""


class WhatsAppMediaError(WhatsAppError):
    """A safely classified inbound Meta media access failure."""

    def __init__(
        self,
        code: str,
        stage: str,
        *,
        error_type: str,
        status_code: int | None = None,
        timeout: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.error_type = error_type
        self.status_code = status_code
        self.timeout = timeout


class WhatsAppReadReceiptError(WhatsAppError):
    """A safely classified Meta read-status failure."""

    def __init__(
        self,
        code: str,
        *,
        error_type: str,
        status_code: int | None = None,
        timeout: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.error_type = error_type
        self.status_code = status_code
        self.timeout = timeout


class WhatsAppTypingIndicatorError(WhatsAppError):
    """A safely classified Meta typing-indicator failure."""

    def __init__(
        self,
        code: str,
        *,
        error_type: str,
        status_code: int | None = None,
        timeout: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.error_type = error_type
        self.status_code = status_code
        self.timeout = timeout


class WhatsAppClient:
    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        http: httpx.AsyncClient | None = None,
        *,
        outbound_enabled: bool = False,
        graph_api_version: str = "v25.0",
        media_access_enabled: bool | None = None,
        read_receipts_enabled: bool = False,
        typing_enabled: bool = False,
        media_retry_attempts: int = 5,
        media_retry_delays: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0),
        media_retry_jitter_seconds: float = 0.0,
        media_metadata_refreshes: int = 1,
        media_retry_after_cap_seconds: float = 30.0,
    ) -> None:
        version = _normalize_graph_api_version(graph_api_version)
        self.graph_url = f"https://graph.facebook.com/{version}"
        self.base_url = f"https://graph.facebook.com/{version}/{phone_number_id}"
        self.headers = {"Authorization": f"Bearer {access_token}"}
        self.http = http or httpx.AsyncClient(timeout=20)
        self.outbound_enabled = outbound_enabled
        self.media_access_enabled = outbound_enabled if media_access_enabled is None else media_access_enabled
        self.read_receipts_enabled = read_receipts_enabled
        self.typing_enabled = typing_enabled
        self.provider_name = "meta_cloud"
        self.media_retry_attempts = max(1, media_retry_attempts)
        self.media_retry_delays = media_retry_delays
        self.media_retry_jitter_seconds = max(0.0, media_retry_jitter_seconds)
        self.media_metadata_refreshes = max(0, media_metadata_refreshes)
        self.media_retry_after_cap_seconds = max(0.0, media_retry_after_cap_seconds)
        self._media_url_to_id: dict[str, str] = {}

    async def send_text(self, to: str, text: str) -> dict[str, Any]:
        return await self._post(
            "/messages",
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text},
            },
        )

    async def send_buttons(self, to: str, body: str, buttons: list[dict[str, str]]) -> dict[str, Any]:
        action = {"buttons": [{"type": "reply", "reply": button} for button in buttons[:3]]}
        return await self._post(
            "/messages",
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {"type": "button", "body": {"text": body}, "action": action},
            },
        )

    async def send_list(
        self,
        to: str,
        body: str,
        rows: list[dict[str, str]],
        *,
        button_text: str = "Select",
    ) -> dict[str, Any]:
        action = {
            "button": button_text,
            "sections": [
                {
                    "title": "Options",
                    "rows": [
                        {
                            "id": row["id"],
                            "title": row["title"],
                            **({"description": row["description"]} if row.get("description") else {}),
                        }
                        for row in rows[:10]
                    ],
                }
            ],
        }
        return await self._post(
            "/messages",
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {"type": "list", "body": {"text": body}, "action": action},
            },
        )

    async def send_document(self, to: str, media_id: str, filename: str, caption: str) -> dict[str, Any]:
        return await self._post(
            "/messages",
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "document",
                "document": {"id": media_id, "filename": filename, "caption": caption},
            },
        )

    async def send_document_link(self, to: str, link: str, filename: str, caption: str) -> dict[str, Any]:
        return await self._post(
            "/messages",
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "document",
                "document": {"link": link, "filename": filename, "caption": caption},
            },
        )

    async def upload_media(self, content: bytes, *, filename: str, content_type: str) -> str:
        if not self.outbound_enabled:
            raise WhatsAppOutboundDisabled("WhatsApp media upload requires WA_OUTBOUND_ENABLED=true")
        files = {"file": (filename, content, content_type)}
        data = {"messaging_product": "whatsapp", "type": content_type}
        response = await self.http.post(
            f"{self.base_url}/media",
            headers=self.headers,
            data=data,
            files=files,
        )
        self._raise_for_status(response)
        media_id = response.json().get("id")
        if not isinstance(media_id, str) or not media_id:
            raise WhatsAppError("WhatsApp media upload returned no media id")
        return media_id

    async def upload_and_send_document(
        self,
        to: str,
        content: bytes,
        *,
        filename: str,
        content_type: str,
        caption: str,
    ) -> dict[str, Any]:
        media_id = await self.upload_media(content, filename=filename, content_type=content_type)
        return await self.send_document(to, media_id, filename, caption)

    async def upload_and_send_audio(self, to: str, content: bytes, *, filename: str) -> dict[str, Any]:
        """An OGG/Opus file shows as a voice note in WhatsApp."""
        media_id = await self.upload_media(content, filename=filename, content_type="audio/ogg")
        return await self._post("/messages", {"messaging_product": "whatsapp", "to": to, "type": "audio",
                                             "audio": {"id": media_id}})

    async def send_template(self, to: str, template_name: str, language_code: str = "en") -> dict[str, Any]:
        return await self._post(
            "/messages",
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {"name": template_name, "language": {"code": language_code}},
            },
        )

    async def mark_message_read(self, message_id: str) -> dict[str, Any]:
        if not self.read_receipts_enabled:
            raise WhatsAppOutboundDisabled("WhatsApp read receipts are disabled")
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        try:
            response = await self.http.post(
                f"{self.base_url}/messages",
                headers=self.headers,
                json=payload,
                timeout=5,
            )
        except httpx.TimeoutException as exc:
            raise WhatsAppReadReceiptError(
                "read_receipt_timeout",
                error_type=type(exc).__name__,
                timeout=True,
            ) from exc
        except httpx.TransportError as exc:
            raise WhatsAppReadReceiptError(
                "read_receipt_transport_failed",
                error_type=type(exc).__name__,
            ) from exc
        if response.status_code >= 400:
            category = f"{response.status_code // 100}xx"
            raise WhatsAppReadReceiptError(
                f"read_receipt_http_{category}",
                error_type="HTTPStatusError",
                status_code=response.status_code,
            )
        try:
            result = response.json()
        except ValueError:
            return {}
        return result if isinstance(result, dict) else {}

    async def start_typing(self, message_id: str) -> dict[str, Any]:
        if not self.typing_enabled:
            raise WhatsAppOutboundDisabled("WhatsApp typing indicators are disabled")
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
            "typing_indicator": {"type": "text"},
        }
        try:
            response = await self.http.post(
                f"{self.base_url}/messages",
                headers=self.headers,
                json=payload,
                timeout=5,
            )
        except httpx.TimeoutException as exc:
            raise WhatsAppTypingIndicatorError(
                "typing_indicator_timeout",
                error_type=type(exc).__name__,
                timeout=True,
            ) from exc
        except httpx.TransportError as exc:
            raise WhatsAppTypingIndicatorError(
                "typing_indicator_transport_failed",
                error_type=type(exc).__name__,
            ) from exc
        if response.status_code >= 400:
            category = f"{response.status_code // 100}xx"
            raise WhatsAppTypingIndicatorError(
                f"typing_indicator_http_{category}",
                error_type="HTTPStatusError",
                status_code=response.status_code,
            )
        try:
            result = response.json()
        except ValueError:
            return {}
        return result if isinstance(result, dict) else {}

    async def get_media_metadata(self, media_id: str) -> dict[str, Any]:
        if not self.media_access_enabled:
            raise WhatsAppOutboundDisabled("WhatsApp inbound media access is disabled")
        response = await self._media_get(f"{self.graph_url}/{media_id}", stage="media_metadata")
        try:
            payload = response.json()
        except ValueError as exc:
            raise WhatsAppMediaError(
                "media_metadata_invalid_response",
                "media_metadata",
                error_type=type(exc).__name__,
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise WhatsAppMediaError(
                "media_metadata_invalid_response",
                "media_metadata",
                error_type="InvalidPayloadType",
                status_code=response.status_code,
            )
        media_url = payload.get("url")
        if isinstance(media_url, str) and media_url:
            self._media_url_to_id[media_url] = media_id
        return payload

    async def download_media(self, media_url: str) -> bytes:
        if not self.media_access_enabled:
            raise WhatsAppOutboundDisabled("WhatsApp inbound media access is disabled")
        current_url = media_url
        refreshes = 0
        while True:
            try:
                return await self._download_media_once(current_url)
            except WhatsAppMediaError as exc:
                media_id = self._media_url_to_id.get(current_url)
                if (
                    media_id
                    and refreshes < self.media_metadata_refreshes
                    and _download_url_may_be_expired(exc)
                ):
                    refreshes += 1
                    metadata = await self.get_media_metadata(media_id)
                    refreshed_url = metadata.get("url")
                    if isinstance(refreshed_url, str) and refreshed_url and refreshed_url != current_url:
                        current_url = refreshed_url
                        continue
                raise

    async def _download_media_once(self, media_url: str) -> bytes:
        response = await self._media_get(media_url, stage="media_download")
        if not response.content:
            raise WhatsAppMediaError(
                "media_download_empty",
                "media_download",
                error_type="EmptyResponse",
                status_code=response.status_code,
            )
        content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type in {"text/html", "application/xhtml+xml"}:
            raise WhatsAppMediaError(
                "media_download_invalid_response",
                "media_download",
                error_type="UnexpectedContentType",
                status_code=response.status_code,
            )
        return response.content

    async def _media_get(self, url: str, *, stage: str) -> httpx.Response:
        last_error: WhatsAppMediaError | None = None
        for attempt in range(1, self.media_retry_attempts + 1):
            try:
                response = await self.http.get(url, headers=self.headers)
            except httpx.TimeoutException as exc:
                last_error = WhatsAppMediaError(
                    f"{stage}_timeout",
                    stage,
                    error_type=type(exc).__name__,
                    timeout=True,
                )
                if attempt >= self.media_retry_attempts:
                    raise last_error from exc
                await self._sleep_before_media_retry(attempt, None)
                continue
            except httpx.TransportError as exc:
                code = (
                    f"{stage}_tls_verification_failed"
                    if _contains_cause(exc, ssl.SSLCertVerificationError)
                    else f"{stage}_http_failed"
                )
                last_error = WhatsAppMediaError(code, stage, error_type=type(exc).__name__)
                if code.endswith("tls_verification_failed") or attempt >= self.media_retry_attempts:
                    raise last_error from exc
                await self._sleep_before_media_retry(attempt, None)
                continue

            if response.status_code < 400:
                return response
            content_type = response.headers.get("content-type", "").lower()
            auth_failure = response.status_code == 401 or (
                response.status_code == 403 and "json" in content_type
            )
            code = f"{stage}_{'auth_failed' if auth_failure else 'http_failed'}"
            last_error = WhatsAppMediaError(
                code,
                stage,
                error_type="HTTPStatusError",
                status_code=response.status_code,
            )
            if auth_failure or response.status_code not in {429, 500, 502, 503, 504} or attempt >= self.media_retry_attempts:
                raise last_error
            await self._sleep_before_media_retry(attempt, response)

        if last_error is not None:
            raise last_error
        raise WhatsAppMediaError(f"{stage}_http_failed", stage, error_type="RetryExhausted")

    async def _sleep_before_media_retry(self, attempt: int, response: httpx.Response | None) -> None:
        retry_after = (
            _retry_after_seconds(response, cap_seconds=self.media_retry_after_cap_seconds)
            if response is not None
            else None
        )
        if retry_after is not None:
            delay = retry_after
        elif attempt - 1 < len(self.media_retry_delays):
            delay = self.media_retry_delays[attempt - 1]
        else:
            delay = self.media_retry_delays[-1] if self.media_retry_delays else 0.0
        if self.media_retry_jitter_seconds:
            delay += random.uniform(0.0, self.media_retry_jitter_seconds)
        if delay > 0:
            await asyncio.sleep(delay)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.outbound_enabled:
            raise WhatsAppOutboundDisabled("WhatsApp outbound send requires WA_OUTBOUND_ENABLED=true")
        response = await self.http.post(f"{self.base_url}{path}", headers=self.headers, json=payload)
        self._raise_for_status(response)
        return response.json()

    async def _get(self, url: str) -> dict[str, Any]:
        if not self.outbound_enabled:
            raise WhatsAppOutboundDisabled("WhatsApp API access requires WA_OUTBOUND_ENABLED=true")
        response = await self.http.get(url, headers=self.headers)
        self._raise_for_status(response)
        return response.json()

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code == 429:
            raise WhatsAppRateLimitError("WhatsApp Cloud API rate limit")
        if response.status_code in {500, 502, 503, 504}:
            raise WhatsAppTransientError(f"WhatsApp Cloud API transient status {response.status_code}")
        response.raise_for_status()


def _normalize_graph_api_version(version: str) -> str:
    normalized = version.strip()
    if not normalized:
        return "v25.0"
    return normalized if normalized.startswith("v") else f"v{normalized}"


def _contains_cause(exc: BaseException, error_type: type[BaseException]) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, error_type):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _retry_after_seconds(response: httpx.Response | None, *, cap_seconds: float) -> float | None:
    if response is None:
        return None
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        delay = (parsed.astimezone(UTC) - datetime.now(UTC)).total_seconds()
    return min(max(0.0, delay), cap_seconds)


def _download_url_may_be_expired(exc: WhatsAppMediaError) -> bool:
    return exc.stage == "media_download" and exc.status_code in {400, 401, 403, 404}
