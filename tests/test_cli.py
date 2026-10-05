import io
import json
from concurrent import futures

import grpc
import pytest
from google.protobuf import descriptor_pb2, json_format, struct_pb2
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


def _protoset_without_imports():
    fds = descriptor_pb2.FileDescriptorSet()
    fds.file.add(name="a.proto", package="x", dependency=["google/protobuf/empty.proto"])
    return fds.SerializeToString()


@pytest.mark.parametrize(
    "content, expected",
    [
        (b'syntax = "proto3";\npackage x;\n', "not a binary descriptor set"),
        (_protoset_without_imports(), "--include_imports"),
    ],
)
def test_bad_protoset(plain_server, payload_file, tmp_path, capsys, content, expected):
    protoset = tmp_path / "api.protoset"
    protoset.write_bytes(content)
    rc = main(
        ["--plaintext", "--server", plain_server, "--endpoint", ENDPOINT, "--payload-file", payload_file]
        + ["--protoset", str(protoset)]
    )
    assert rc == 2
    assert expected in capsys.readouterr().err


HEALTH_PROTO = """
syntax = "proto3";
package grpc.health.v1;
import "google/protobuf/empty.proto";
import "messages.proto";
service Health {
  rpc Check(HealthCheckRequest) returns (HealthCheckResponse);
  rpc Unused(google.protobuf.Empty) returns (google.protobuf.Empty);
}
"""

MESSAGES_PROTO = """
syntax = "proto3";
package grpc.health.v1;
message HealthCheckRequest { string service = 1; }
message HealthCheckResponse {
  enum ServingStatus { UNKNOWN = 0; SERVING = 1; NOT_SERVING = 2; SERVICE_UNKNOWN = 3; }
  ServingStatus status = 1;
}
"""


@pytest.fixture
def proto_dir(tmp_path):
    """health.proto in api/, importing messages.proto from shared/."""
    (tmp_path / "api").mkdir()
    (tmp_path / "shared").mkdir()
    (tmp_path / "api" / "health.proto").write_text(HEALTH_PROTO)
    (tmp_path / "shared" / "messages.proto").write_text(MESSAGES_PROTO)
    return tmp_path


def _call_with(plain_server, payload_file, *extra):
    return main(
        ["--plaintext", "--server", plain_server, "--endpoint", ENDPOINT, "--payload-file", payload_file, *extra]
    )


def test_proto_source(plain_server, payload_file, proto_dir, capsys):
    api, shared = proto_dir / "api", proto_dir / "shared"
    rc = _call_with(plain_server, payload_file, "--proto", str(api / "health.proto"), "-I", str(api), "-I", str(shared))
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"status": "SERVING"}


def test_proto_default_import_path(plain_server, payload_file, tmp_path, capsys):
    (tmp_path / "health.proto").write_text(HEALTH_PROTO)
    (tmp_path / "messages.proto").write_text(MESSAGES_PROTO)
    rc = _call_with(plain_server, payload_file, "--proto", str(tmp_path / "health.proto"))
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"status": "SERVING"}


@pytest.mark.parametrize(
    "proto, extra, expected",
    [
        ('syntax = "proto3";\nmessage {', [], "cannot compile"),
        (HEALTH_PROTO, [], "cannot compile"),  # messages.proto is not on the import path
        (None, [], "no such file"),
    ],
)
def test_bad_proto(plain_server, payload_file, tmp_path, capsys, proto, extra, expected):
    path = tmp_path / "api.proto"
    if proto is not None:
        path.write_text(proto)
    rc = _call_with(plain_server, payload_file, "--proto", str(path), *extra)
    assert rc == 2
    assert expected in capsys.readouterr().err


def test_import_path_needs_proto(plain_server, payload_file, tmp_path, capsys):
    assert _call_with(plain_server, payload_file, "-I", str(tmp_path)) == 2
    assert "only applies to --proto" in capsys.readouterr().err


def test_proto_and_protoset_exclusive(plain_server, payload_file, tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        _call_with(plain_server, payload_file, "--proto", "a.proto", "--protoset", "a.protoset")
    assert e.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


COLLECT_PROTO = """
syntax = "proto3";
package test;
import "google/protobuf/struct.proto";
service Collect {
  rpc Collect(stream google.protobuf.Struct) returns (google.protobuf.Struct);
}
"""


@pytest.fixture
def collect_server(tmp_path):
    """A client-streaming service that replies with every message it got, as {"messages": [...]}."""

    def collect(requests, context):
        reply = struct_pb2.Struct()
        reply.get_or_create_list("messages").extend([json_format.MessageToDict(r) for r in requests])
        return reply

    handler = grpc.method_handlers_generic_handler(
        "test.Collect",
        {
            "Collect": grpc.stream_unary_rpc_method_handler(
                collect,
                request_deserializer=struct_pb2.Struct.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            )
        },
    )
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4), handlers=[handler])
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    proto = tmp_path / "collect.proto"
    proto.write_text(COLLECT_PROTO)
    yield f"127.0.0.1:{port}", str(proto)
    server.stop(None)


def test_payload_with_several_messages(collect_server, tmp_path, capsys):
    address, proto = collect_server
    payload = tmp_path / "payload.json"
    payload.write_text('{ "options": {"o1": "test"} }\n{"data": {"text1": "testtt"}}\n')
    rc = main(
        ["--plaintext", "--server", address, "--endpoint", "test.Collect/Collect"]
        + ["--payload-file", str(payload), "--proto", proto]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "messages": [{"options": {"o1": "test"}}, {"data": {"text1": "testtt"}}]
    }


@pytest.mark.parametrize(
    "payload, expected",
    [
        ('{"service": ""}\n{"service": ""}', "takes one request message, payload has 2"),
        ('{"service": ""}\n{"service": ', "not valid JSON"),
        ("", "not valid JSON"),
    ],
)
def test_bad_message_sequence(plain_server, tmp_path, capsys, payload, expected):
    path = tmp_path / "payload.json"
    path.write_text(payload)
    assert _call_with(plain_server, str(path)) == 2
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
