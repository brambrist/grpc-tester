import io
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from grpc_call_tester.rest import main


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else None
        if self.path == "/missing":
            self._reply(404, {"error": "no such thing"})
        elif self.path == "/text":
            self._reply(200, b"plain reply", "text/plain")
        elif self.path == "/slow":
            time.sleep(2)
            self._reply(200, {})
        else:
            self._reply(
                200,
                {
                    "method": self.command,
                    "path": self.path,
                    "body": body,
                    "token": self.headers.get("X-Token"),
                },
            )

    do_GET = do_POST = do_PUT = do_DELETE = _handle


def _serve(ssl_context=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    if ssl_context:
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def plain_server():
    server, address = _serve()
    yield address
    server.shutdown()


@pytest.fixture
def mtls_server(pki):
    paths, _ = pki
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(paths["server_cert"], paths["server_key"])
    ctx.load_verify_locations(paths["ca"])
    ctx.verify_mode = ssl.CERT_REQUIRED
    server, address = _serve(ctx)
    yield address, paths
    server.shutdown()


@pytest.fixture
def payload_file(tmp_path):
    path = tmp_path / "payload.json"
    path.write_text('{"name": "x"}')
    return str(path)


def test_get_without_payload(plain_server, capsys):
    rc = main(["--plaintext", "--server", plain_server, "--endpoint", "/items?limit=1"])
    assert rc == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply == {"method": "GET", "path": "/items?limit=1", "body": None, "token": None}


def test_post_payload_with_header(plain_server, payload_file, capsys):
    rc = main(
        ["--plaintext", "--server", plain_server, "--endpoint", "/items", "--payload-file", payload_file]
        + ["-H", "X-Token: secret"]
    )
    assert rc == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply == {"method": "POST", "path": "/items", "body": {"name": "x"}, "token": "secret"}


def test_properties_file_with_method_headers_and_output(plain_server, tmp_path, capsys):
    props = tmp_path / "props.json"
    props.write_text(
        json.dumps(
            {
                "server": plain_server,
                "endpoint": "/items/1",
                "method": "put",
                "headers": {"X-Token": "from-file"},
                "payload": {"name": "y"},
            }
        )
    )
    out = tmp_path / "out.json"
    rc = main(["--plaintext", "--properties-file", str(props), "-o", str(out)])
    assert rc == 0
    assert json.loads(out.read_text()) == {
        "method": "PUT",
        "path": "/items/1",
        "body": {"name": "y"},
        "token": "from-file",
    }
    assert capsys.readouterr().out == ""


def test_properties_from_stdin_with_url_server(plain_server, capsys, monkeypatch):
    props = json.dumps({"server": f"http://{plain_server}/api/", "endpoint": "items"})
    monkeypatch.setattr("sys.stdin", io.StringIO(props))
    rc = main(["--properties-file", "-"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["path"] == "/api/items"


def test_non_json_reply_passes_through(plain_server, capsys):
    rc = main(["--plaintext", "--server", plain_server, "--endpoint", "/text"])
    assert rc == 0
    assert capsys.readouterr().out == "plain reply\n"


def test_http_error_is_reported(plain_server, capsys):
    rc = main(["--plaintext", "--server", plain_server, "--endpoint", "/missing"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "HTTP error: 404" in captured.err
    assert "no such thing" in captured.err


def test_call_timeout(plain_server, capsys):
    rc = main(["--plaintext", "--server", plain_server, "--endpoint", "/slow", "--timeout", "0.3"])
    assert rc == 1
    assert "timed out" in capsys.readouterr().err


def test_connect_failure(capsys):
    rc = main(["--plaintext", "--server", "127.0.0.1:1", "--endpoint", "/", "--connect-timeout", "0.5"])
    assert rc == 1
    assert "could not connect" in capsys.readouterr().err


def test_bad_input(tmp_path, capsys):
    assert main(["--plaintext", "--server", "x:1"]) == 2
    assert "missing: --endpoint" in capsys.readouterr().err
    assert main(["--plaintext", "--server", "x:1", "--endpoint", "/", "-H", "nocolon"]) == 2
    assert "header must look like" in capsys.readouterr().err


def test_mtls(mtls_server, payload_file, capsys):
    address, paths = mtls_server
    rc = main(
        ["--ca-cert", str(paths["ca"]), "--client-cert", str(paths["client_cert"])]
        + ["--client-key", str(paths["client_key"])]
        + ["--server", address, "--endpoint", "/items", "--payload-file", payload_file]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["body"] == {"name": "x"}


def test_tls_without_ca_fails_verification(mtls_server, capsys):
    address, paths = mtls_server
    rc = main(
        ["--client-cert", str(paths["client_cert"]), "--client-key", str(paths["client_key"])]
        + ["--server", address, "--endpoint", "/items"]
    )
    assert rc == 1
    assert "CERTIFICATE_VERIFY_FAILED" in capsys.readouterr().err


def test_skip_verify(mtls_server, capsys):
    address, paths = mtls_server
    rc = main(
        ["--insecure-skip-verify", "--client-cert", str(paths["client_cert"])]
        + ["--client-key", str(paths["client_key"])]
        + ["--server", address, "--endpoint", "/items"]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["method"] == "GET"
