"""
ISE SAML token validation middleware.

This module is the Gateway's security boundary. All requests must be validated here
before being forwarded to downstream services.

Validates ISE-issued SAML tokens:
- Fetches ISE SAML IdP metadata from ISE_SAML_METADATA_URL and caches the parsed
  X.509 signing certificate with a TTL (not re-fetched on every request)
- Verifies the SAML assertion's XML digital signature against that certificate —
  the assertion is never trusted without a valid, cryptographically verified signature
- Enforces the assertion's Conditions (NotBefore / NotOnOrAfter) — expired or
  not-yet-valid assertions are rejected
- Extracts tenant_id from saml:Attribute[@Name='tenant_id'] and user_identity from
  saml:NameID
- Rejects expired, malformed, tampered, or unsigned tokens with HTTP 401

The bearer token presented in the Authorization header is the base64-encoded SAML
assertion XML (a SAML bearer assertion, as used by RFC 7522-style flows) issued by
ISE after AD authentication + Duo MFA. A raw (non-base64) XML string is also
accepted, to keep local testing and tooling simple.

Required environment variables:
  ISE_SAML_METADATA_URL          — URL to the ISE SAML metadata XML endpoint
Optional environment variables:
  ISE_SAML_METADATA_CACHE_TTL_SECONDS — how long the fetched signing cert is
                                         cached before being re-fetched (default 3600)

There are no committed default certificates, keys, or metadata URLs — all signing
material is sourced from the configured IdP metadata endpoint or mounted secrets at
runtime. Never modify this module without explicit instruction — it is the security
boundary.
"""

import base64
import logging
import os
import threading
import time
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException
from lxml import etree
from signxml import XMLVerifier

logger = logging.getLogger(__name__)

SAML_ASSERTION_NS = "urn:oasis:names:tc:SAML:2.0:assertion"
SAML_METADATA_NS = "urn:oasis:names:tc:SAML:2.0:metadata"
DS_NS = "http://www.w3.org/2000/09/xmldsig#"

_NS = {"saml": SAML_ASSERTION_NS, "md": SAML_METADATA_NS, "ds": DS_NS}

_DEFAULT_METADATA_CACHE_TTL_SECONDS = 3600

# Module-scope cache for the parsed signing certificate (PEM). Guarded by a lock
# since FastAPI may run request handling across multiple threads/tasks.
_cert_cache_lock = threading.Lock()
_cert_cache: dict = {"pem": None, "fetched_at": 0.0}


def _metadata_cache_ttl_seconds() -> float:
    try:
        return float(os.getenv("ISE_SAML_METADATA_CACHE_TTL_SECONDS", str(_DEFAULT_METADATA_CACHE_TTL_SECONDS)))
    except ValueError:
        return float(_DEFAULT_METADATA_CACHE_TTL_SECONDS)


def _fetch_metadata_xml(metadata_url: str) -> bytes:
    """Fetch the raw ISE SAML metadata document. Raises HTTPException(503) on failure."""
    try:
        response = httpx.get(metadata_url, timeout=10.0)
        response.raise_for_status()
        return response.content
    except Exception as exc:
        logger.error("Failed to fetch ISE SAML metadata from %s: %s", metadata_url, exc)
        raise HTTPException(status_code=503, detail="Unable to fetch IdP metadata") from exc


def _extract_signing_cert_pem(metadata_xml: bytes) -> str:
    """
    Parse IdP metadata and return the PEM-encoded signing certificate.

    Prefers a KeyDescriptor explicitly marked use="signing"; falls back to any
    KeyDescriptor carrying an X509Certificate if none is explicitly marked (some
    IdPs omit the `use` attribute when they publish a single key for both purposes).
    """
    try:
        root = etree.fromstring(metadata_xml)
    except etree.XMLSyntaxError as exc:
        logger.error("Malformed ISE SAML metadata: %s", exc)
        raise HTTPException(status_code=503, detail="Malformed IdP metadata") from exc

    cert_b64 = None
    for key_descriptor in root.iter(f"{{{SAML_METADATA_NS}}}KeyDescriptor"):
        cert_elements = key_descriptor.findall(f".//{{{DS_NS}}}X509Certificate")
        if not cert_elements or not cert_elements[0].text:
            continue
        use = key_descriptor.get("use")
        if use == "signing":
            cert_b64 = cert_elements[0].text
            break
        if cert_b64 is None:
            cert_b64 = cert_elements[0].text

    if not cert_b64:
        logger.error("No X509 signing certificate found in ISE SAML metadata")
        raise HTTPException(status_code=503, detail="No signing certificate found in IdP metadata")

    cert_b64 = "".join(cert_b64.split())
    pem_lines = [cert_b64[i : i + 64] for i in range(0, len(cert_b64), 64)]
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(pem_lines) + "\n-----END CERTIFICATE-----\n"


def _get_cached_signing_cert() -> str:
    """
    Return the cached PEM signing certificate, fetching and caching it if the TTL
    has expired (or nothing has been cached yet). Metadata is deliberately not
    fetched on every request.
    """
    now = time.monotonic()
    with _cert_cache_lock:
        cached_pem = _cert_cache["pem"]
        fresh = cached_pem is not None and (now - _cert_cache["fetched_at"]) < _metadata_cache_ttl_seconds()
        if fresh:
            return cached_pem

    metadata_url = os.getenv("ISE_SAML_METADATA_URL", "")
    if not metadata_url:
        logger.error("ISE_SAML_METADATA_URL not configured")
        raise HTTPException(status_code=503, detail="Auth service not configured")

    metadata_xml = _fetch_metadata_xml(metadata_url)
    pem = _extract_signing_cert_pem(metadata_xml)

    with _cert_cache_lock:
        _cert_cache["pem"] = pem
        _cert_cache["fetched_at"] = now
    return pem


def _reset_signing_cert_cache() -> None:
    """Test helper — forces the next validate_token call to re-fetch metadata."""
    with _cert_cache_lock:
        _cert_cache["pem"] = None
        _cert_cache["fetched_at"] = 0.0


def _decode_token(token: str) -> bytes:
    """
    The bearer token is the base64-encoded SAML assertion XML. Raw (non-encoded)
    XML is also accepted so local tooling/tests can pass an assertion directly.
    """
    stripped = token.strip()
    try:
        decoded = base64.b64decode(stripped, validate=True)
        if decoded.strip().startswith(b"<"):
            return decoded
    except Exception:
        pass
    if stripped.startswith("<"):
        return stripped.encode("utf-8")
    raise ValueError("token is neither valid base64-encoded XML nor raw XML")


def _parse_saml_datetime(value: str) -> datetime:
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _check_conditions(assertion) -> None:
    """Enforce NotBefore / NotOnOrAfter. Raises HTTPException(401) if outside the window."""
    conditions = assertion.find("saml:Conditions", _NS)
    if conditions is None:
        raise ValueError("assertion missing saml:Conditions")

    now = datetime.now(timezone.utc)

    not_on_or_after = conditions.get("NotOnOrAfter")
    if not_on_or_after:
        if now >= _parse_saml_datetime(not_on_or_after):
            raise ValueError("assertion expired (NotOnOrAfter)")

    not_before = conditions.get("NotBefore")
    if not_before:
        if now < _parse_saml_datetime(not_before):
            raise ValueError("assertion not yet valid (NotBefore)")


def _extract_tenant_id(assertion) -> str | None:
    for attribute in assertion.findall(".//saml:AttributeStatement/saml:Attribute", _NS):
        if attribute.get("Name") == "tenant_id":
            value_el = attribute.find("saml:AttributeValue", _NS)
            if value_el is not None and value_el.text and value_el.text.strip():
                return value_el.text.strip()
    return None


def _extract_user_identity(assertion) -> str | None:
    name_id = assertion.find(".//saml:Subject/saml:NameID", _NS)
    if name_id is not None and name_id.text and name_id.text.strip():
        return name_id.text.strip()
    return None


def validate_token(token: str) -> dict:
    """
    Validate an ISE-issued SAML token and return extracted claims.

    Args:
        token: Raw token string extracted from the Authorization header.

    Returns:
        dict with keys:
            tenant_id (str): Tenant identifier extracted from SAML attributes.
            user_identity (str): User identity (e.g. email) from SAML NameID.

    Raises:
        HTTPException 401: If the token is missing, malformed, expired, not-yet-valid,
                            fails signature validation, or is missing required claims.
        HTTPException 503: If the Gateway is not configured with an IdP metadata URL,
                            or the IdP metadata cannot be fetched/parsed.
    """
    if not token or not token.strip():
        raise HTTPException(status_code=401, detail="Empty token")

    # Fetching/caching the signing cert can itself raise HTTPException(503) for
    # configuration/connectivity problems — let those propagate as-is.
    cert_pem = _get_cached_signing_cert()

    try:
        xml_bytes = _decode_token(token)
        assertion = etree.fromstring(xml_bytes)
    except Exception as exc:
        logger.warning("Rejected malformed SAML token: %s", exc)
        raise HTTPException(status_code=401, detail="Malformed token") from exc

    try:
        result = XMLVerifier().verify(assertion, x509_cert=cert_pem)
        verified_assertion = result.signed_xml
    except Exception as exc:
        logger.warning("Rejected SAML token with invalid signature: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid token signature") from exc

    try:
        _check_conditions(verified_assertion)
        tenant_id = _extract_tenant_id(verified_assertion)
        user_identity = _extract_user_identity(verified_assertion)
        if not tenant_id or not user_identity:
            raise ValueError("token missing required tenant_id/user_identity claims")
    except ValueError as exc:
        logger.warning("Rejected SAML token: %s", exc)
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    return {"tenant_id": tenant_id, "user_identity": user_identity}
