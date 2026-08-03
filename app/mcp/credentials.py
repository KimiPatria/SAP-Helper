"""SAP credential source - Secrets Manager by default, .env only for local dev.

Manager requirement: the shipped build must NOT read SAP (or AWS) credentials
from `.env`. They live in AWS Secrets Manager and are fetched at runtime with
boto3, cached in memory after the first call so a long-running process makes
exactly one Secrets Manager request per secret.

The `.env` values are used ONLY when `LOCAL_DEV=true`, so a developer without
AWS access can still iterate against a tenant. This flag is the single switch
between the two sources - nothing else changes.

This composes with, and does not replace, `SAPClient._build_auth()`:
* `_build_auth()` is the *auth-method* seam (Basic today, OAuth later).
* this module is the *credential-source* seam (Secrets Manager vs .env).
`SAPClient` calls `resolve_sap_credentials()` from `_build_auth()` when it was
constructed without explicit credentials, so the source swap touches nothing
outside these two files.

Expected Secrets Manager secret shape (one JSON object, one secret):

    {
      "base_url": "https://myXXXXXX-api.s4hana.cloud.sap",
      "username": "<communication user>",
      "password": "<password>"
    }

IAM permission needed by the runtime role: `secretsmanager:GetSecretValue`
on the secret's ARN (see README).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

log = logging.getLogger("sap-mcp.credentials")


@dataclass(frozen=True)
class SAPCredentials:
    base_url: str
    username: str
    password: str


class CredentialError(Exception):
    """Raised when SAP credentials cannot be resolved from the chosen source."""


# Process-lifetime cache: {secret_name: SAPCredentials}. Secrets Manager
# charges per API call and rotates rarely, so one fetch per process is right;
# restart the process to pick up a rotated secret (documented in the README).
_cache: dict[str, SAPCredentials] = {}


def resolve_sap_credentials(settings) -> SAPCredentials:
    """Return SAP base URL + Basic-Auth credentials from the active source.

    LOCAL_DEV=true  -> the SAP_* values in .env (settings).
    otherwise       -> AWS Secrets Manager secret named SAP_SECRET_NAME, cached.

    Raises CredentialError with an actionable message on any misconfiguration,
    so the caller surfaces one sentence instead of a boto3 stack trace.
    """
    if settings.local_dev:
        if not settings.sap_base_url:
            raise CredentialError(
                "LOCAL_DEV=true but SAP_BASE_URL is empty in .env. Set the SAP_* "
                "values, or unset LOCAL_DEV to read from AWS Secrets Manager."
            )
        return SAPCredentials(
            base_url=settings.sap_base_url,
            username=settings.sap_username,
            password=settings.sap_password,
        )
    return _from_secrets_manager(
        secret_name=settings.sap_secret_name,
        region=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


def is_configured(settings) -> bool:
    """Whether a credential SOURCE is set up, without touching the network.

    Status/health callers need this: since Secrets Manager became the default
    source, `SAP_BASE_URL` is empty in a correctly configured deployment, so
    testing that variable reports a live tenant as "not configured". This checks
    whichever source is active instead. It is a configuration-shape check, not a
    liveness check - it deliberately makes no Secrets Manager call, because
    /api/status is polled.
    """
    if settings.local_dev:
        return bool(settings.sap_base_url)
    return bool(settings.sap_secret_name)


def _from_secrets_manager(
    secret_name: str,
    region: str,
    aws_access_key_id: str = "",
    aws_secret_access_key: str = "",
) -> SAPCredentials:
    cached = _cache.get(secret_name)
    if cached is not None:
        return cached

    if not secret_name:
        raise CredentialError(
            "SAP_SECRET_NAME is empty. Set it to the Secrets Manager secret id "
            "holding the SAP credentials JSON, or set LOCAL_DEV=true to use .env."
        )
    try:
        import boto3  # deferred: local-dev iteration never imports boto3 for this
        import botocore.exceptions
    except ImportError as exc:  # pragma: no cover - boto3 is a declared dependency
        raise CredentialError(f"boto3 is required to read Secrets Manager: {exc}") from exc

    try:
        client = boto3.client(
            "secretsmanager",
            region_name=region,
            # Empty strings -> boto3's default credential chain (env, role, SSO).
            aws_access_key_id=aws_access_key_id or None,
            aws_secret_access_key=aws_secret_access_key or None,
        )
        response = client.get_secret_value(SecretId=secret_name)
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "unknown")
        raise CredentialError(
            f"Could not read secret '{secret_name}' from Secrets Manager ({code}). "
            "Check SAP_SECRET_NAME, the region, and that the runtime role has "
            "secretsmanager:GetSecretValue on it."
        ) from exc
    except botocore.exceptions.BotoCoreError as exc:
        raise CredentialError(f"Secrets Manager error reading '{secret_name}': {exc}") from exc

    raw = response.get("SecretString")
    if not raw:
        raise CredentialError(
            f"Secret '{secret_name}' has no SecretString (binary secrets are not "
            "supported here - store the credentials as a JSON string)."
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CredentialError(
            f"Secret '{secret_name}' is not valid JSON. Expected keys "
            "base_url, username, password."
        ) from exc

    missing = [k for k in ("base_url", "username", "password") if not data.get(k)]
    if missing:
        raise CredentialError(
            f"Secret '{secret_name}' is missing required key(s): {', '.join(missing)}. "
            "Expected a JSON object with base_url, username, password."
        )

    credentials = SAPCredentials(
        base_url=str(data["base_url"]).rstrip("/"),
        username=str(data["username"]),
        password=str(data["password"]),
    )
    _cache[secret_name] = credentials
    log.info("Loaded SAP credentials from Secrets Manager secret '%s' (cached).", secret_name)
    return credentials


def clear_cache() -> None:
    """Drop the in-memory secret cache (e.g. after a rotation). Test/ops hook."""
    _cache.clear()
