from __future__ import annotations

import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.request import urlopen

import pytest

from tracefix.docker_tls import write_test_tls_material


def test_test_tls_material_is_ephemeral_localhost_pair(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    certificate_path = tmp_path / "certs" / "test-ca.pem"
    key_path = tmp_path / "certs" / "test-key.pem"

    write_test_tls_material(certificate_path, key_path)

    certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
    private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    assert isinstance(private_key, rsa.RSAPrivateKey)
    assert certificate.public_key().public_numbers() == private_key.public_key().public_numbers()
    san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    assert str(san.get_values_for_type(x509.IPAddress)[0]) == "127.0.0.1"
    assert certificate_path.name == "test-ca.pem"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate_path, key_path)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client_context = ssl.create_default_context(cafile=str(certificate_path))
    try:
        with urlopen(
            f"https://127.0.0.1:{server.server_port}/", context=client_context, timeout=5
        ) as response:
            assert response.status == 200
            assert response.read() == b"ok"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
