"""HTTP client for S/4HANA Cloud OData services.

One class owns the two SAP-specific mechanics so no tool ever inlines them:

* Reads: plain GET with Basic Auth.
* Writes: the CSRF handshake - a GET carrying `X-CSRF-Token: Fetch` returns
  a token header plus a session cookie; the subsequent POST must send both.
  The token is cached per service and refreshed once automatically when SAP
  answers 403 "CSRF token validation failed" (expired session).

The CSRF token and session cookies live only in this object's memory (the
httpx cookie jar) - they are never written to disk and never logged. Raw
SAP error bodies are logged for debugging; auth headers and cookies are not.

Auth is isolated behind `_build_auth()`: swapping Basic for OAuth later is
one new branch there (client-credentials fetch against sap_oauth_token_url)
with zero changes to callers.

Where the base URL / username / password come FROM is a separate seam: the
optional `credentials` argument (an `app.mcp.credentials.SAPCredentials`).
Pass one and it is the source of truth for all three; omit it and the explicit
base_url/username/password args are used (the .env-driven path). This is how
the Secrets-Manager source plugs in without any tool or resolver change.
"""

from __future__ import annotations

import logging

import httpx

from app.mcp.errors import SAPRequestError, translate_sap_error

log = logging.getLogger("sap-mcp.client")


class SAPClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        auth_method: str = "basic",
        timeout_seconds: float = 30.0,
        credentials: object | None = None,
    ):
        # A resolved SAPCredentials (from app.mcp.credentials) wins over the
        # explicit args - that is the Secrets-Manager path. Without it, the
        # explicit args are used (LOCAL_DEV / direct construction).
        if credentials is not None:
            base_url = credentials.base_url
            username = credentials.username
            password = credentials.password
        if not base_url:
            raise SAPRequestError(
                "SAP is not configured: provide credentials via AWS Secrets "
                "Manager (default) or set LOCAL_DEV=true with SAP_BASE_URL / "
                "SAP_USERNAME / SAP_PASSWORD in .env."
            )
        self._auth_method = auth_method
        self._username = username
        self._password = password
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=self._build_auth(),
            timeout=timeout_seconds,
            headers={"Accept": "application/json"},
        )
        # Runtime-only write token, keyed by service root (each OData service
        # issues its own). Session cookies ride along in the httpx cookie jar.
        self._csrf_tokens: dict[str, str] = {}

    def _build_auth(self) -> httpx.Auth:
        """The single auth swap point. OAuth later = one new branch here."""
        if self._auth_method == "basic":
            return httpx.BasicAuth(self._username, self._password)
        raise SAPRequestError(
            f"SAP_AUTH_METHOD '{self._auth_method}' is not implemented yet; "
            "use 'basic' (OAuth is a planned extension - see app/mcp/README.md)."
        )

    # ---- reads ----------------------------------------------------------

    def get(self, path: str, params: dict | None = None, context: str = "reading from SAP") -> dict:
        """GET a JSON OData resource; raises SAPRequestError on any failure."""
        response = self._request("GET", path, params=params, context=context)
        if response.status_code >= 400:
            raise self._error(response, context)
        return self._json(response, context)

    # ---- writes ---------------------------------------------------------

    def post(self, service_root: str, path: str, payload: dict,
             context: str = "writing to SAP") -> dict:
        """POST with the CSRF handshake; retries once on an expired token."""
        token = self._csrf_tokens.get(service_root) or self._fetch_csrf(service_root, context)
        response = self._request(
            "POST", path, json_body=payload, context=context,
            headers={"X-CSRF-Token": token},
        )
        if response.status_code == 403 and \
                response.headers.get("x-csrf-token", "").lower() == "required":
            token = self._fetch_csrf(service_root, context)
            response = self._request(
                "POST", path, json_body=payload, context=context,
                headers={"X-CSRF-Token": token},
            )
        if response.status_code >= 400:
            raise self._error(response, context)
        return self._json(response, context)

    def _fetch_csrf(self, service_root: str, context: str) -> str:
        """The handshake GET. SAP sets the session cookie here; httpx keeps it."""
        response = self._request(
            "GET", service_root.rstrip("/") + "/", context=context,
            headers={"X-CSRF-Token": "Fetch"},
        )
        token = response.headers.get("x-csrf-token", "")
        if response.status_code >= 400 or not token:
            raise self._error(
                response,
                f"fetching a CSRF token before {context} (service {service_root})",
            )
        self._csrf_tokens[service_root] = token
        return token

    # ---- plumbing -------------------------------------------------------

    def _request(self, method: str, path: str, params: dict | None = None,
                 json_body: dict | None = None, headers: dict | None = None,
                 context: str = "") -> httpx.Response:
        try:
            return self._http.request(
                method, path, params=params, json=json_body, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise SAPRequestError(
                f"SAP did not respond within the timeout while {context}. "
                "The system may be busy - try again shortly. If this was a "
                "create call, check the document didn't get created before retrying.",
            ) from exc
        except httpx.HTTPError as exc:
            raise SAPRequestError(
                f"Could not reach SAP while {context}: {type(exc).__name__}. "
                "Check SAP_BASE_URL and network connectivity.",
            ) from exc

    def _error(self, response: httpx.Response, context: str) -> SAPRequestError:
        error = translate_sap_error(response.status_code, response.text, context)
        # Raw payload to logs for debugging; the caller only surfaces user_message.
        log.warning(
            "SAP %s %s -> HTTP %s (code %s): %s",
            response.request.method, response.request.url.path,
            response.status_code, error.sap_code or "-",
            (error.raw or "")[:2000],
        )
        return error

    @staticmethod
    def _json(response: httpx.Response, context: str) -> dict:
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise SAPRequestError(
                f"SAP returned a non-JSON response while {context} - the "
                "service path may point at a UI endpoint rather than an OData API.",
                status_code=response.status_code, raw=response.text[:2000],
            ) from exc

    def close(self) -> None:
        self._http.close()
