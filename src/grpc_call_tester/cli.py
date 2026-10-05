"""grpc-call: send a JSON payload to a gRPC endpoint and print the reply."""

import argparse
import json
import socket
import ssl
import sys
import tempfile
from importlib import resources
from pathlib import Path

import grpc
from cryptography import x509
from cryptography.x509.oid import NameOID
from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory
from google.protobuf.message import DecodeError
from grpc_tools import protoc
from grpc_reflection.v1alpha.proto_reflection_descriptor_database import (
    ProtoReflectionDescriptorDatabase,
)

from grpc_call_tester.common import (
    EXIT_CALL_FAILED,
    EXIT_USAGE,
    UsageError,
    add_call_arguments,
    add_run_arguments,
    add_tls_arguments,
    check_client_cert_pair,
    read_bytes,
    resolve_call,
    write_reply,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grpc-call",
        description="Send a JSON payload to a gRPC endpoint and print the reply as JSON.",
    )
    add_tls_arguments(p)
    add_call_arguments(
        p,
        title="call (either all three of these, or --properties-file)",
        endpoint_metavar="PKG.SERVICE/METHOD",
        properties_help='JSON file of the form {"server": ..., "endpoint": ..., "payload": {...}}',
    )
    types = p.add_argument_group("message types (default: ask the server via reflection)")
    source = types.add_mutually_exclusive_group()
    source.add_argument("--protoset", metavar="FILE", help="compiled FileDescriptorSet to take message types from")
    source.add_argument(
        "--proto",
        metavar="FILE",
        action="append",
        help=".proto source file to take message types from (repeatable)",
    )
    types.add_argument(
        "-I",
        "--import-path",
        metavar="DIR",
        action="append",
        help="directory to resolve --proto imports from (repeatable; default: each --proto file's directory)",
    )
    add_run_arguments(p)
    return p


def _fetch_server_cert(server: str, client_cert: str | None, client_key: str | None, timeout: float):
    """Grab the server's certificate without verifying it. Returns (pem, name in the cert or None)."""
    host, _, port = server.rpartition(":")
    host = host.strip("[]")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["h2"])
    if client_cert:
        ctx.load_cert_chain(client_cert, client_key)
    with socket.create_connection((host, int(port)), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls_sock:
            der = tls_sock.getpeercert(binary_form=True)

    cert = x509.load_der_x509_certificate(der)
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = san.get_values_for_type(x509.DNSName) + [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = [a.value for a in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    name = names[0].replace("*", "any") if names else None
    return ssl.DER_cert_to_PEM_cert(der).encode(), name


def open_channel(server: str, args) -> grpc.Channel:
    if args.plaintext:
        return grpc.insecure_channel(server)

    check_client_cert_pair(args)

    options = []
    if args.insecure_skip_verify:
        # grpc has no "don't verify" switch, so trust exactly the certificate the
        # server presents and expect whatever name is written in it.
        try:
            roots, name = _fetch_server_cert(server, args.client_cert, args.client_key, args.connect_timeout)
        except (OSError, ValueError) as e:
            raise UsageError(f"cannot fetch server certificate from {server}: {e}") from e
        if name:
            options.append(("grpc.ssl_target_name_override", name))
    else:
        roots = read_bytes(args.ca_cert)

    creds = grpc.ssl_channel_credentials(
        root_certificates=roots,
        private_key=read_bytes(args.client_key),
        certificate_chain=read_bytes(args.client_cert),
    )
    return grpc.secure_channel(server, creds, options=options)


def compile_protos(protos: list[str], import_paths: list[str] | None) -> bytes:
    """Run the bundled protoc over .proto files and return the FileDescriptorSet it builds."""
    for proto in protos:
        if not Path(proto).is_file():
            raise UsageError(f"cannot read {proto}: no such file")
    if not import_paths:
        import_paths = list(dict.fromkeys(str(Path(proto).parent) for proto in protos))
    # protoc doesn't know where google/protobuf/*.proto live; grpc_tools ships them.
    well_known = str(resources.files("grpc_tools") / "_proto")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "protoset"
        argv = ["protoc", "--include_imports", f"--descriptor_set_out={out}"]
        argv += [f"-I{d}" for d in import_paths] + [f"-I{well_known}"] + protos
        # protoc prints its own diagnostics to stderr.
        if protoc.main(argv) != 0:
            raise UsageError(f"cannot compile {', '.join(protos)}")
        return out.read_bytes()


def find_method(channel: grpc.Channel, endpoint: str, descriptors: bytes | None, source: str):
    """Look the method up in descriptors (a serialized FileDescriptorSet read from source),
    or via server reflection when descriptors is None."""
    service_name, _, method_name = endpoint.lstrip("/").rpartition("/")
    if not service_name or not method_name:
        raise UsageError(f"endpoint must look like package.Service/Method, got {endpoint!r}")

    if descriptors is not None:
        try:
            file_set = descriptor_pb2.FileDescriptorSet.FromString(descriptors)
        except DecodeError:
            raise UsageError(
                f"{source} is not a binary descriptor set "
                "(build one with protoc --include_imports --descriptor_set_out=FILE, or use --proto)"
            ) from None
        pool = descriptor_pool.DescriptorPool()
        for file_proto in file_set.file:
            try:
                pool.Add(file_proto)
            except TypeError as e:
                raise UsageError(f"{source}: {e} (was it built with --include_imports?)") from None
    else:
        pool = descriptor_pool.DescriptorPool(ProtoReflectionDescriptorDatabase(channel))

    try:
        service = pool.FindServiceByName(service_name)
    except KeyError:
        raise UsageError(f"service {service_name!r} not found via {source}") from None
    method = service.methods_by_name.get(method_name)
    if method is None:
        known = ", ".join(sorted(service.methods_by_name))
        raise UsageError(f"service {service_name} has no method {method_name!r} (has: {known})")
    return method


def invoke(channel: grpc.Channel, method, payload, timeout: float):
    """Call the method; returns a dict, or a list of dicts for server-streaming methods."""
    request_cls = message_factory.GetMessageClass(method.input_type)
    response_cls = message_factory.GetMessageClass(method.output_type)

    payloads = payload if isinstance(payload, list) else [payload]
    if not method.client_streaming and len(payloads) != 1:
        raise UsageError(f"{method.full_name} takes one request message, payload has {len(payloads)}")
    try:
        requests = [json_format.ParseDict(p, request_cls()) for p in payloads]
    except json_format.ParseError as e:
        raise UsageError(f"payload does not match {method.input_type.full_name}: {e}") from e

    kind = ("stream" if method.client_streaming else "unary") + "_" + ("stream" if method.server_streaming else "unary")
    rpc = getattr(channel, kind)(
        f"/{method.containing_service.full_name}/{method.name}",
        request_serializer=request_cls.SerializeToString,
        response_deserializer=response_cls.FromString,
    )
    result = rpc(iter(requests) if method.client_streaming else requests[0], timeout=timeout)
    if method.server_streaming:
        return [json_format.MessageToDict(m) for m in result]
    return json_format.MessageToDict(result)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        call = resolve_call(args, payload_stream=True)
        server = call["server"]
        if args.import_path and not args.proto:
            raise UsageError("--import-path only applies to --proto")
        # Compile before connecting, so a broken .proto fails fast.
        if args.proto:
            descriptors, source = compile_protos(args.proto, args.import_path), ", ".join(args.proto)
        elif args.protoset:
            descriptors, source = read_bytes(args.protoset), args.protoset
        else:
            descriptors, source = None, "server reflection"
        with open_channel(server, args) as channel:
            try:
                grpc.channel_ready_future(channel).result(timeout=args.connect_timeout)
            except grpc.FutureTimeoutError:
                print(f"error: could not connect to {server} within {args.connect_timeout:g}s", file=sys.stderr)
                return EXIT_CALL_FAILED
            method = find_method(channel, call["endpoint"], descriptors, source)
            reply = invoke(channel, method, call["payload"], args.timeout)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except grpc.RpcError as e:
        print(f"gRPC error: {e.code().name}: {e.details()}", file=sys.stderr)
        return EXIT_CALL_FAILED

    return write_reply(json.dumps(reply, indent=2, ensure_ascii=False) + "\n", args.output)


if __name__ == "__main__":
    sys.exit(main())
