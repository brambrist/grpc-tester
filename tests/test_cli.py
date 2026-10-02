import io
import json
from concurrent import futures

import grpc
import pytest
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection

from grpc_call_tester.cli import main

ENDPOINT = "grpc.health.v1.Health/Check"


def _start_server(credentials=None):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    health_pb2_grpc.add_HealthServicer_to_server(health.HealthServicer(), server)
    reflection.enable_server_reflection(
        (health_pb2.DESCRIPTOR.services_by_name["Health"].full_name, reflection.SERVICE_NAME), server
    )
    if credentials:
        port = server.add_secure_port("127.0.0.1:0", credentials)
    else:
        port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return server, f"127.0.0.1:{port}"


@pytest.fixture
def plain_server():
    server, address = _start_server()
    yield address
    server.stop(None)


@pytest.fixture
def mtls_server(pki):
    paths, files = pki
    creds = grpc.ssl_server_credentials(
        [(files["server_key"], files["server_cert"])],
        root_certificates=files["ca"],
        require_client_auth=True,
    )
    server, address = _start_server(creds)
    yield address, paths
    server.stop(None)


@pytest.fixture
def payload_file(tmp_path):
    path = tmp_path / "payload.json"
    path.write_text('{"service": ""}')
    return str(path)


def test_plaintext_separate_options(plain_server, payload_file, capsys):
    rc = main(["--plaintext", "--server", plain_server, "--endpoint", ENDPOINT, "--payload-file", payload_file])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"status": "SERVING"}


def test_properties_file_and_output(plain_server, tmp_path, capsys):
    props = tmp_path / "props.json"
    props.write_text(json.dumps({"server": plain_server, "endpoint": ENDPOINT, "payload": {"service": ""}}))
    out = tmp_path / "out.json"
    rc = main(["--plaintext", "--properties-file", str(props), "-o", str(out)])
    assert rc == 0
    assert json.loads(out.read_text()) == {"status": "SERVING"}
    assert capsys.readouterr().out == ""


def test_call_timeout(plain_server, tmp_path, capsys):
    props = tmp_path / "props.json"
    props.write_text(
        json.dumps({"server": plain_server, "endpoint": "grpc.health.v1.Health/Watch", "payload": {"service": ""}})
    )
    # Watch never ends on its own, so the deadline is what stops it.
    rc = main(["--plaintext", "--properties-file", str(props), "--timeout", "1"])
    assert rc == 1
    assert "DEADLINE_EXCEEDED" in capsys.readouterr().err


def test_grpc_error_is_reported(plain_server, tmp_path, capsys):
    payload = tmp_path / "payload.json"
    payload.write_text('{"service": "no.such.Service"}')
    rc = main(["--plaintext", "--server", plain_server, "--endpoint", ENDPOINT, "--payload-file", str(payload)])
    assert rc == 1
    assert "NOT_FOUND" in capsys.readouterr().err


@pytest.mark.parametrize(
    "endpoint, payload, expected",
    [
        ("grpc.health.v1.Health/Nope", '{"service": ""}', "no method"),
        ("no.such.Service/Check", '{"service": ""}', "not found"),
        (ENDPOINT, '{"bogus": 1}', "does not match"),
    ],
)
def test_bad_input(plain_server, tmp_path, capsys, endpoint, payload, expected):
    path = tmp_path / "payload.json"
    path.write_text(payload)
    rc = main(["--plaintext", "--server", plain_server, "--endpoint", endpoint, "--payload-file", str(path)])
    assert rc == 2
    assert expected in capsys.readouterr().err


def test_properties_file_excludes_separate_options(tmp_path, capsys):
    props = tmp_path / "props.json"
    props.write_text("{}")
    assert main(["--properties-file", str(props), "--server", "x:1"]) == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_connect_timeout(payload_file, capsys):
    rc = main(
        ["--plaintext", "--server", "127.0.0.1:1", "--endpoint", ENDPOINT, "--payload-file", payload_file]
        + ["--connect-timeout", "0.5"]
    )
    assert rc == 1
    assert "could not connect" in capsys.readouterr().err


def test_mtls(mtls_server, payload_file, capsys):
    address, paths = mtls_server
    rc = main(
        ["--ca-cert", str(paths["ca"]), "--client-cert", str(paths["client_cert"])]
        + ["--client-key", str(paths["client_key"])]
        + ["--server", address, "--endpoint", ENDPOINT, "--payload-file", payload_file]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"status": "SERVING"}


def test_tls_without_ca_fails_verification(mtls_server, payload_file, capsys):
    address, paths = mtls_server
    rc = main(
        ["--client-cert", str(paths["client_cert"]), "--client-key", str(paths["client_key"])]
        + ["--server", address, "--endpoint", ENDPOINT, "--payload-file", payload_file, "--connect-timeout", "1"]
    )
    assert rc == 1


def test_skip_verify(mtls_server, payload_file, capsys):
    address, paths = mtls_server
    rc = main(
        ["--insecure-skip-verify", "--client-cert", str(paths["client_cert"])]
        + ["--client-key", str(paths["client_key"])]
        + ["--server", address, "--endpoint", ENDPOINT, "--payload-file", payload_file]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"status": "SERVING"}


def test_certs_from_environment(mtls_server, payload_file, capsys, monkeypatch):
    address, paths = mtls_server
    monkeypatch.setenv("GRPC_CALL_CA_CERT", str(paths["ca"]))
    monkeypatch.setenv("GRPC_CALL_CLIENT_CERT", str(paths["client_cert"]))
    monkeypatch.setenv("GRPC_CALL_CLIENT_KEY", str(paths["client_key"]))
    rc = main(["--server", address, "--endpoint", ENDPOINT, "--payload-file", payload_file])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"status": "SERVING"}


def test_properties_from_stdin(plain_server, capsys, monkeypatch):
    props = json.dumps({"server": plain_server, "endpoint": ENDPOINT, "payload": {"service": ""}})
    monkeypatch.setattr("sys.stdin", io.StringIO(props))
    rc = main(["--plaintext", "--properties-file", "-"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"status": "SERVING"}
