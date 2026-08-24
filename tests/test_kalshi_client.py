import asyncio
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.clients.kalshi import KalshiClient, TokenBucket, sign_request


@pytest.fixture(scope="module")
def keypair(tmp_path_factory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    path = tmp_path_factory.mktemp("keys") / "kalshi.pem"
    path.write_bytes(pem)
    return key, path


def test_signature_verifies_with_pss_sha256(keypair):
    key, _ = keypair
    ts = 1724500000000
    sig_b64 = sign_request(key, ts, "GET", "/trade-api/v2/portfolio/balance")
    import base64
    key.public_key().verify(
        base64.b64decode(sig_b64),
        f"{ts}GET/trade-api/v2/portfolio/balance".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256())  # raises InvalidSignature on mismatch


def test_auth_headers_shape_and_sign_path(keypair):
    _, path = keypair
    client = KalshiClient("https://demo-api.kalshi.co/trade-api/v2",
                          api_key_id="key-123", private_key_path=str(path))
    assert client.path_prefix == "/trade-api/v2"
    headers = client.auth_headers("GET", "/trade-api/v2/markets")
    assert headers["KALSHI-ACCESS-KEY"] == "key-123"
    assert set(headers) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-SIGNATURE",
                            "KALSHI-ACCESS-TIMESTAMP"}
    assert abs(int(headers["KALSHI-ACCESS-TIMESTAMP"]) - time.time() * 1000) < 5000


def test_unauthenticated_client_sends_no_auth_headers():
    client = KalshiClient("https://api.elections.kalshi.com/trade-api/v2")
    assert client.auth_headers("GET", "/trade-api/v2/markets") == {}


async def test_token_bucket_paces_requests():
    bucket = TokenBucket(rate_per_s=50, capacity=1)
    start = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 4 / 50 * 0.8  # ~4 refills needed after the first token
