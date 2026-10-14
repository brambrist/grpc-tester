"""Options and input handling shared by the grpc-call and rest-call commands."""

import argparse
import base64
import binascii
import json
import os
import sys
from pathlib import Path

EXIT_CALL_FAILED = 1
EXIT_USAGE = 2


class UsageError(Exception):
    """Bad arguments or input files."""


def add_tls_arguments(p: argparse.ArgumentParser) -> None:
    tls = p.add_argument_group("TLS")
    env = os.environ.get
    tls.add_argument(
        "--ca-cert",
        metavar="FILE",
        default=env("GRPC_CALL_CA_CERT"),
        help="CA certificate (PEM) used to verify the server [env: GRPC_CALL_CA_CERT]",
    )
    tls.add_argument(
        "--client-cert",
        metavar="FILE",
        default=env("GRPC_CALL_CLIENT_CERT"),
        help="client certificate (PEM) for mTLS [env: GRPC_CALL_CLIENT_CERT]",
    )
    tls.add_argument(
        "--client-key",
        metavar="FILE",
        default=env("GRPC_CALL_CLIENT_KEY"),
        help="client private key (PEM) for mTLS [env: GRPC_CALL_CLIENT_KEY]",
    )
    tls.add_argument(
        "-k",
        "--insecure-skip-verify",
        action="store_true",
        help="skip server certificate checks (still encrypted)",
    )
    tls.add_argument("--plaintext", action="store_true", help="no TLS at all")


def add_call_arguments(p: argparse.ArgumentParser, title: str, endpoint_metavar: str, properties_help: str):
    call = p.add_argument_group(title)
    call.add_argument("--server", metavar="HOST:PORT")
    call.add_argument("--endpoint", metavar=endpoint_metavar)
    call.add_argument("--payload-file", metavar="FILE", help="JSON file with the request body ('-' for stdin)")
    call.add_argument("--properties-file", metavar="FILE", help=f"{properties_help} ('-' for stdin)")
    return call


def add_run_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--timeout", type=float, default=30.0, metavar="SEC", help="call deadline (default: %(default)s)")
    p.add_argument(
        "--connect-timeout",
        type=float,
        default=10.0,
        metavar="SEC",
        help="time to wait for the connection (default: %(default)s)",
    )
    p.add_argument("-o", "--output", metavar="FILE", help="write the reply here instead of stdout")


def add_extract_arguments(p: argparse.ArgumentParser) -> None:
    extract = p.add_argument_group("field extraction")
    extract.add_argument(
        "--field",
        metavar="NAME",
        help="write only this field of the reply (a.b for nested fields); with several replies, "
        "its values from all of them are concatenated",
    )
    extract.add_argument("-b64", "--b64", action="store_true", help="base64-decode the --field values")


def check_extract_arguments(args) -> None:
    if args.b64 and not args.field:
        raise UsageError("--b64 only applies to --field")


def json_documents(text: str) -> list:
    """Parse whitespace-separated JSON documents, e.g. '{"a": 1}\\n{"b": 2}'."""
    decoder = json.JSONDecoder()
    docs = []
    pos = 0
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos == len(text) and docs:
            return docs
        doc, pos = decoder.raw_decode(text, pos)
        docs.append(doc)


def load_json(path: str, multiple: bool = False):
    """Load one JSON document; with multiple, the file may hold several back to back, and
    then a list of them is returned."""
    try:
        if path == "-":
            path = "<stdin>"
            text = sys.stdin.read()
        else:
            text = Path(path).read_text(encoding="utf-8")
        if not multiple:
            return json.loads(text)
        docs = json_documents(text)
        return docs[0] if len(docs) == 1 else docs
    except OSError as e:
        raise UsageError(f"cannot read {path}: {e.strerror}") from e
    except json.JSONDecodeError as e:
        raise UsageError(f"{path} is not valid JSON: {e}") from e


def read_bytes(path: str | None) -> bytes | None:
    if path is None:
        return None
    try:
        return Path(path).read_bytes()
    except OSError as e:
        raise UsageError(f"cannot read {path}: {e.strerror}") from e


def resolve_call(args, payload_required: bool = True, payload_stream: bool = False) -> dict:
    """Return the call as a dict with server, endpoint and payload, taken from either
    the properties file (any extra keys in it are kept) or the separate options.
    With payload_stream, a payload file holding several JSON documents gives a list of them."""
    required = ["server", "endpoint"] + (["payload"] if payload_required else [])
    separate = {"server": args.server, "endpoint": args.endpoint, "payload": args.payload_file}

    def flag(key):
        return "--payload-file" if key == "payload" else f"--{key}"

    if args.properties_file:
        given = [flag(k) for k, v in separate.items() if v is not None]
        if given:
            raise UsageError(f"--properties-file cannot be combined with {', '.join(given)}")
        props = load_json(args.properties_file)
        missing = [k for k in required if k not in props] if isinstance(props, dict) else required
        if missing:
            raise UsageError(f"{args.properties_file} is missing: {', '.join(missing)}")
        props.setdefault("payload", None)
        return props

    missing = [flag(k) for k in required if separate[k] is None]
    if missing:
        raise UsageError(
            f"need --properties-file, or all of {', '.join(flag(k) for k in required)} (missing: {', '.join(missing)})"
        )
    payload = load_json(args.payload_file, multiple=payload_stream) if args.payload_file else None
    return {"server": args.server, "endpoint": args.endpoint, "payload": payload}


def check_client_cert_pair(args) -> None:
    if bool(args.client_cert) != bool(args.client_key):
        raise UsageError("--client-cert and --client-key must be given together")


def _json_name(name: str) -> str:
    """lowerCamelCase name protobuf uses in JSON, e.g. chunk_data -> chunkData."""
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _lookup(message, path: list[str]):
    """Follow path into a reply dict; returns None when a field is absent."""
    value = message
    for name in path:
        if not isinstance(value, dict):
            return None
        value = value.get(name, value.get(_json_name(name)))
    return value


def _decode_b64(value: str, field: str) -> bytes:
    # protobuf writes standard base64 but also accepts URL-safe input, so do the same.
    value = value.replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
    except binascii.Error as e:
        raise UsageError(f"field {field} is not valid base64: {e}") from None


def extract_field(reply, field: str, b64: bool) -> bytes:
    """Concatenate the field's values from the reply (a dict, or a list of them, e.g. from a
    server stream). A repeated field contributes all its values. Replies without the field are
    skipped, since protobuf leaves out empty values."""
    messages = reply if isinstance(reply, list) else [reply]
    values = [v for v in (_lookup(m, field.split(".")) for m in messages) if v is not None]
    if not values:
        raise UsageError(f"no reply has a field {field!r}")
    out = bytearray()
    for value in values:
        for item in value if isinstance(value, list) else [value]:
            if b64:
                if not isinstance(item, str):
                    raise UsageError(f"field {field} is not a string, cannot base64-decode it")
                out += _decode_b64(item, field)
            elif isinstance(item, str):
                out += item.encode()
            else:
                out += json.dumps(item, ensure_ascii=False).encode()
    return bytes(out)


def write_reply(reply: str | bytes, output: str | None) -> int:
    """Write the reply to output, or stdout; bytes are written as they are."""
    data = reply if isinstance(reply, bytes) else reply.encode("utf-8")
    if output:
        try:
            Path(output).write_bytes(data)
        except OSError as e:
            print(f"error: cannot write {output}: {e.strerror}", file=sys.stderr)
            return EXIT_USAGE
    elif isinstance(reply, bytes):
        sys.stdout.flush()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(reply)
    return 0
