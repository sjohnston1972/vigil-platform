import datetime

import pytest
from fastapi import HTTPException

from tests import saml_fixtures as fx

METADATA_URL = "https://ise.example.com/saml/metadata"


@pytest.fixture
def idp_keypair():
    return fx.generate_key_and_cert()


@pytest.fixture(autouse=True)
def _patch_metadata_fetch(monkeypatch, idp_keypair):
    """
    Every test gets a fresh signing cert cache and a fake metadata fetch that
    returns metadata embedding this test's IdP certificate — no real network call.
    """
    import middleware.auth as auth

    key_pem, cert_pem = idp_keypair
    metadata_xml = fx.build_metadata_xml(cert_pem)

    monkeypatch.setenv("ISE_SAML_METADATA_URL", METADATA_URL)
    auth._reset_signing_cert_cache()

    calls = {"count": 0}

    def fake_fetch(url):
        assert url == METADATA_URL
        calls["count"] += 1
        return metadata_xml

    monkeypatch.setattr(auth, "_fetch_metadata_xml", fake_fetch)
    yield calls
    auth._reset_signing_cert_cache()


class TestValidToken:
    def test_valid_signed_token_returns_claims(self, idp_keypair):
        import middleware.auth as auth

        key_pem, cert_pem = idp_keypair
        signed = fx.build_signed_assertion(key_pem, cert_pem, tenant_id="tenant-a", user_identity="alice@tenant-a.com")
        token = fx.as_bearer_token(signed)

        claims = auth.validate_token(token)

        assert claims == {"tenant_id": "tenant-a", "user_identity": "alice@tenant-a.com"}

    def test_valid_token_accepts_raw_xml_not_only_base64(self, idp_keypair):
        import middleware.auth as auth

        key_pem, cert_pem = idp_keypair
        signed = fx.build_signed_assertion(key_pem, cert_pem, tenant_id="tenant-b", user_identity="bob@tenant-b.com")

        claims = auth.validate_token(signed.decode("utf-8"))

        assert claims["tenant_id"] == "tenant-b"
        assert claims["user_identity"] == "bob@tenant-b.com"

    def test_metadata_is_cached_not_fetched_per_request(self, idp_keypair, _patch_metadata_fetch):
        import middleware.auth as auth

        key_pem, cert_pem = idp_keypair
        token = fx.as_bearer_token(fx.build_signed_assertion(key_pem, cert_pem))

        auth.validate_token(token)
        auth.validate_token(token)
        auth.validate_token(token)

        assert _patch_metadata_fetch["count"] == 1


class TestRejections:
    def test_expired_assertion_is_rejected(self, idp_keypair):
        """NotOnOrAfter in the past — signature is valid but the token has expired."""
        import middleware.auth as auth

        key_pem, cert_pem = idp_keypair
        signed = fx.build_signed_assertion(
            key_pem,
            cert_pem,
            not_before_delta=datetime.timedelta(minutes=-30),
            not_after_delta=datetime.timedelta(minutes=-10),
        )
        token = fx.as_bearer_token(signed)

        with pytest.raises(HTTPException) as exc_info:
            auth.validate_token(token)
        assert exc_info.value.status_code == 401

    def test_not_yet_valid_assertion_is_rejected(self, idp_keypair):
        import middleware.auth as auth

        key_pem, cert_pem = idp_keypair
        signed = fx.build_signed_assertion(
            key_pem,
            cert_pem,
            not_before_delta=datetime.timedelta(minutes=10),
            not_after_delta=datetime.timedelta(minutes=30),
        )
        token = fx.as_bearer_token(signed)

        with pytest.raises(HTTPException) as exc_info:
            auth.validate_token(token)
        assert exc_info.value.status_code == 401

    def test_tampered_assertion_is_rejected(self, idp_keypair):
        """Signature no longer matches content that was altered post-signing."""
        import middleware.auth as auth

        key_pem, cert_pem = idp_keypair
        signed = fx.build_signed_assertion(key_pem, cert_pem, tenant_id="tenant-a")
        tampered = fx.tamper_assertion(signed, new_tenant_id="tenant-hacked")
        token = fx.as_bearer_token(tampered)

        with pytest.raises(HTTPException) as exc_info:
            auth.validate_token(token)
        assert exc_info.value.status_code == 401

    def test_assertion_signed_by_untrusted_key_is_rejected(self, idp_keypair):
        """Signed by a different keypair than the one published in IdP metadata."""
        import middleware.auth as auth

        _, real_cert_pem = idp_keypair
        attacker_key_pem, attacker_cert_pem = fx.generate_key_and_cert("attacker")
        signed = fx.build_signed_assertion(attacker_key_pem, attacker_cert_pem, tenant_id="tenant-a")
        token = fx.as_bearer_token(signed)

        with pytest.raises(HTTPException) as exc_info:
            auth.validate_token(token)
        assert exc_info.value.status_code == 401

    def test_unsigned_assertion_is_rejected(self):
        import middleware.auth as auth

        unsigned_xml = b"""<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_x" Version="2.0" IssueInstant="2026-01-01T00:00:00Z">
          <saml:Issuer>https://ise.example.com</saml:Issuer>
          <saml:Subject><saml:NameID>eve@tenant-a.com</saml:NameID></saml:Subject>
          <saml:Conditions NotBefore="2020-01-01T00:00:00Z" NotOnOrAfter="2099-01-01T00:00:00Z"/>
          <saml:AttributeStatement>
            <saml:Attribute Name="tenant_id"><saml:AttributeValue>tenant-a</saml:AttributeValue></saml:Attribute>
          </saml:AttributeStatement>
        </saml:Assertion>"""
        token = fx.as_bearer_token(unsigned_xml)

        with pytest.raises(HTTPException) as exc_info:
            auth.validate_token(token)
        assert exc_info.value.status_code == 401

    def test_malformed_xml_is_rejected(self):
        import middleware.auth as auth

        with pytest.raises(HTTPException) as exc_info:
            auth.validate_token("not-valid-base64-or-xml-@@@")
        assert exc_info.value.status_code == 401

    def test_empty_token_is_rejected(self):
        import middleware.auth as auth

        with pytest.raises(HTTPException) as exc_info:
            auth.validate_token("")
        assert exc_info.value.status_code == 401

    def test_missing_tenant_id_attribute_is_rejected(self, idp_keypair):
        import middleware.auth as auth

        key_pem, cert_pem = idp_keypair
        now = datetime.datetime.now(datetime.timezone.utc)
        xml_str = f"""<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_noattr" Version="2.0" IssueInstant="{now.strftime('%Y-%m-%dT%H:%M:%SZ')}">
          <saml:Issuer>https://ise.example.com</saml:Issuer>
          <saml:Subject><saml:NameID>eve@tenant-a.com</saml:NameID></saml:Subject>
          <saml:Conditions NotBefore="{(now - datetime.timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')}" NotOnOrAfter="{(now + datetime.timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')}"/>
        </saml:Assertion>"""
        from lxml import etree
        from signxml import XMLSigner, methods

        root = etree.fromstring(xml_str.encode("utf-8"))
        signed_root = XMLSigner(
            method=methods.enveloped, signature_algorithm="rsa-sha256", digest_algorithm="sha256"
        ).sign(root, key=key_pem, cert=cert_pem)
        token = fx.as_bearer_token(etree.tostring(signed_root))

        with pytest.raises(HTTPException) as exc_info:
            auth.validate_token(token)
        assert exc_info.value.status_code == 401


class TestConfiguration:
    def test_missing_metadata_url_returns_503(self, monkeypatch, idp_keypair):
        import middleware.auth as auth

        monkeypatch.delenv("ISE_SAML_METADATA_URL", raising=False)
        auth._reset_signing_cert_cache()

        with pytest.raises(HTTPException) as exc_info:
            auth.validate_token("anything")
        assert exc_info.value.status_code == 503

    def test_metadata_fetch_failure_returns_503(self, monkeypatch):
        import middleware.auth as auth

        auth._reset_signing_cert_cache()

        def failing_fetch(url):
            raise HTTPException(status_code=503, detail="Unable to fetch IdP metadata")

        monkeypatch.setattr(auth, "_fetch_metadata_xml", failing_fetch)

        with pytest.raises(HTTPException) as exc_info:
            auth.validate_token("anything")
        assert exc_info.value.status_code == 503


class TestEndpointIntegration:
    def test_missing_authorization_header_returns_401(self):
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)
        response = client.post("/step-up/sur-001/approve", json={})
        assert response.status_code == 401

    def test_valid_token_flows_through_validate_ise_token(self, idp_keypair):
        from starlette.requests import Request
        from main import validate_ise_token

        key_pem, cert_pem = idp_keypair
        signed = fx.build_signed_assertion(key_pem, cert_pem, tenant_id="tenant-a", user_identity="alice@tenant-a.com")
        token = fx.as_bearer_token(signed)

        scope = {
            "type": "http",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
        request = Request(scope)
        claims = validate_ise_token(request)
        assert claims == {"tenant_id": "tenant-a", "user_identity": "alice@tenant-a.com"}
