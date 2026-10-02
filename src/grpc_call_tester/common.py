"""Options and input handling shared by the grpc-call and rest-call commands."""

import argparse
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


def load_json(path: str):
    try:
        if path == "-":
            path = "<stdin>"
            return json.loads(sys.stdin.read())
        return json.loads(Path(path).read_text(encoding="utf-8"))
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


def resolve_call(args, payload_required: bool = True) -> dict:
    """Return the call as a dict with server, endpoint and payload, taken from either
    the properties file (any extra keys in it are kept) or the separate options."""
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
    payload = load_json(args.payload_file) if args.payload_file else None
    return {"server": args.server, "endpoint": args.endpoint, "payload": payload}


def check_client_cert_pair(args) -> None:
    if bool(args.client_cert) != bool(args.client_key):
        raise UsageError("--client-cert and --client-key must be given together")


def write_reply(text: str, output: str | None) -> int:
    if output:
        try:
            Path(output).write_text(text, encoding="utf-8")
        except OSError as e:
            print(f"error: cannot write {output}: {e.strerror}", file=sys.stderr)
            return EXIT_USAGE
    else:
        sys.stdout.write(text)
    return 0
