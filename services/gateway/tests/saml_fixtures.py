"""
Test-only helpers for generating a signed SAML assertion fixture set:
a self-signed IdP key/cert pair, a metadata document embedding the cert, and
signed assertions (valid / expired / tampered) for exercising
middleware.auth.validate_token without any real ISE dependency.
"""

import base64
import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner, methods

SAML_NS = "urn:oasis:names:tc:SAML:2.0:assertion"


def generate_key_and_cert(common_name: str = "ise-test-idp"):
    """Return (key_pem: bytes, cert_pem: bytes) for a fresh self-signed RSA cert."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return key_pem, cert_pem


def build_metadata_xml(cert_pem: bytes) -> bytes:
    """Build a minimal IdP metadata document embedding the given signing cert."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    der_b64 = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
    xml = f"""<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata" entityID="https://ise.example.com">
  <IDPSSODescriptor>
    <KeyDescriptor use="signing">
      <ds:KeyInfo xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
        <ds:X509Data>
          <ds:X509Certificate>{der_b64}</ds:X509Certificate>
        </ds:X509Data>
      </ds:KeyInfo>
    </KeyDescriptor>
  </IDPSSODescriptor>
</EntityDescriptor>"""
    return xml.encode("utf-8")


def build_signed_assertion(
    key_pem: bytes,
    cert_pem: bytes,
    tenant_id: str = "tenant-a",
    user_identity: str = "alice@tenant-a.com",
    not_before_delta: datetime.timedelta = datetime.timedelta(minutes=-5),
    not_after_delta: datetime.timedelta = datetime.timedelta(minutes=5),
    assertion_id: str = "_test-assertion-001",
) -> bytes:
    """Build and sign a minimal SAML assertion. Returns the signed XML bytes."""
    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = (now + not_before_delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    not_after = (now + not_after_delta).strftime("%Y-%m-%dT%H:%M:%SZ")

    xml_str = f"""<saml:Assertion xmlns:saml="{SAML_NS}" ID="{assertion_id}" Version="2.0" IssueInstant="{now.strftime('%Y-%m-%dT%H:%M:%SZ')}">
  <saml:Issuer>https://ise.example.com</saml:Issuer>
  <saml:Subject>
    <saml:NameID>{user_identity}</saml:NameID>
  </saml:Subject>
  <saml:Conditions NotBefore="{not_before}" NotOnOrAfter="{not_after}"/>
  <saml:AttributeStatement>
    <saml:Attribute Name="tenant_id">
      <saml:AttributeValue>{tenant_id}</saml:AttributeValue>
    </saml:Attribute>
  </saml:AttributeStatement>
</saml:Assertion>"""

    root = etree.fromstring(xml_str.encode("utf-8"))
    signed_root = XMLSigner(
        method=methods.enveloped,
        signature_algorithm="rsa-sha256",
        digest_algorithm="sha256",
    ).sign(root, key=key_pem, cert=cert_pem)
    return etree.tostring(signed_root)


def tamper_assertion(signed_xml: bytes, new_tenant_id: str = "tenant-hacked") -> bytes:
    """Modify a signed assertion's tenant_id attribute value without re-signing."""
    root = etree.fromstring(signed_xml)
    ns = {"saml": SAML_NS}
    value_el = root.find(
        ".//saml:AttributeStatement/saml:Attribute[@Name='tenant_id']/saml:AttributeValue", ns
    )
    value_el.text = new_tenant_id
    return etree.tostring(root)


def as_bearer_token(signed_xml: bytes) -> str:
    """Base64-encode signed assertion XML the way a real bearer token would be presented."""
    return base64.b64encode(signed_xml).decode("ascii")
