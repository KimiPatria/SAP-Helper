"""SAP OData error translation.

SAP Gateway error responses are verbose JSON or XML with SAP-internal
message codes. The chatbot layer should see one plain-language sentence;
the full payload is preserved on the exception for logging only - it is
never returned to the user.

JSON shape (OData v2):
    {"error": {"code": "MEPO/002", "message": {"lang": "en", "value": "..."},
               "innererror": {"errordetails": [{"code": ..., "message": ...}]}}}
JSON shape (OData v4):
    {"error": {"code": "...", "message": "...", "details": [{"message": ...}]}}
XML (either version): <error><code>...</code><message>...</message></error>
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET


class SAPRequestError(Exception):
    """A failed SAP OData call, carrying both audiences' views of it.

    `user_message` is safe to surface in chat; `raw` is for logs only.
    """

    def __init__(self, user_message: str, status_code: int | None = None,
                 sap_code: str = "", raw: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.status_code = status_code
        self.sap_code = sap_code
        self.raw = raw


def _text(node: object) -> str:
    # v2 nests message as {"lang": ..., "value": ...}; v4 uses a bare string.
    if isinstance(node, dict):
        return str(node.get("value") or node.get("message") or "").strip()
    return str(node or "").strip()


def _from_json(body: str) -> tuple[str, str, list[str]] | None:
    try:
        error = json.loads(body).get("error")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(error, dict):
        return None
    code = str(error.get("code") or "")
    message = _text(error.get("message"))
    details: list[str] = []
    inner = error.get("innererror") or {}
    for detail in (inner.get("errordetails") or error.get("details") or []):
        if isinstance(detail, dict):
            text = _text(detail.get("message"))
            # Gateway repeats the top-level message in errordetails; keep
            # only detail lines that add information.
            if text and text != message and text not in details:
                details.append(text)
    return code, message, details


def _from_xml(body: str) -> tuple[str, str, list[str]] | None:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    if not root.tag.endswith("error"):
        return None
    code = message = ""
    for child in root.iter():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "code" and not code:
            code = (child.text or "").strip()
        elif tag == "message" and not message:
            message = (child.text or "").strip()
    return code, message, []


def translate_sap_error(status_code: int, body: str, context: str) -> SAPRequestError:
    """Build a SAPRequestError with a plain-language message.

    `context` is a human phrase for what we were doing ("creating the
    purchase order", "searching suppliers") so the surfaced message reads
    as a sentence even when SAP's payload is unparseable.
    """
    parsed = _from_json(body) or _from_xml(body)
    if parsed:
        code, message, details = parsed
        text = message or "SAP returned an error without a message text."
        if details:
            text += " Details: " + "; ".join(details[:3])
        return SAPRequestError(
            f"SAP rejected {context}: {text}",
            status_code=status_code, sap_code=code, raw=body,
        )

    # Unparseable body - fall back to what the HTTP status implies.
    generic = {
        400: "the request was malformed or a field value is invalid",
        401: "authentication failed - check SAP_USERNAME / SAP_PASSWORD",
        403: "the user is not authorized for this service (or the CSRF token was rejected)",
        404: "the service or entity was not found - check the service path and document number",
        500: "an internal SAP error occurred",
        503: "the SAP service is temporarily unavailable",
    }.get(status_code, f"SAP returned HTTP {status_code}")
    return SAPRequestError(
        f"SAP request failed while {context}: {generic}.",
        status_code=status_code, raw=body,
    )
