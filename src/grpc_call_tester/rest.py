"""rest-call: send a JSON payload to a REST endpoint and print the reply."""

import argparse
import contextlib
import http.client
import json
import ssl
import sys
from urllib.parse import urlsplit

from grpc_call_tester.common import (
    EXIT_CALL_FAILED,
    EXIT_USAGE,
    UsageError,
    add_call_arguments,
    add_run_arguments,
    add_tls_arguments,
    check_client_cert_pair,
    resolve_call,
    write_reply,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rest-call",
        description="Send a JSON payload to a REST endpoint and print the reply.",
    )
    add_tls_arguments(p)
    call = add_call_arguments(
        p,
        title="call (either --server and --endpoint with optional --payload-file, or --properties-file)",
        endpoint_metavar="/PATH",
        properties_help='JSON file of the form {"server": ..., "endpoint": ..., "payload": {...}}, '
        'optionally with "method" and "headers"',
    )
    call.add_argument("-X", "--method", help="HTTP method (default: POST with a payload, GET without)")
    call.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        metavar="'NAME: VALUE'",
        help="extra request header, repeatable",
    )
    add_run_arguments(p)
    return p


def _parse_headers(call: dict, cli_headers: list[str]) -> dict:
    headers = call.get("headers") or {}
    if not isinstance(headers, dict):
        raise UsageError('"headers" in the properties file must be an object')
    headers = {str(k): str(v) for k, v in headers.items()}
    for item in cli_headers:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise UsageError(f"header must look like 'Name: value', got {item!r}")
        headers[name.strip()] = value.strip()
    return headers


def build_ssl_context(args) -> ssl.SSLContext:
    check_client_cert_pair(args)
    try:
        ctx = ssl.create_default_context(cafile=args.ca_cert)
        # Python 3.13 turned on strict RFC 5280 checks, which reject many in-house CAs
        # (e.g. no Authority Key Identifier) that grpc-call and curl accept.
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        if args.client_cert:
            ctx.load_cert_chain(args.client_cert, args.client_key)
    except OSError as e:  # includes ssl.SSLError
        raise UsageError(f"cannot load certificates: {e}") from e
    if args.insecure_skip_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def open_connection(server: str, args) -> tuple[http.client.HTTPConnection, str]:
    """Return (connection, base path). The server may carry an http:// or https:// prefix."""
    plaintext, base_path = args.plaintext, ""
    if "://" in server:
        url = urlsplit(server)
        if url.scheme not in ("http", "https"):
            raise UsageError(f"unsupported scheme in server {server!r}")
        plaintext = plaintext or url.scheme == "http"
        server, base_path = url.netloc, url.path.rstrip("/")
    try:
        if plaintext:
            return http.client.HTTPConnection(server, timeout=args.connect_timeout), base_path
        context = build_ssl_context(args)
        return http.client.HTTPSConnection(server, timeout=args.connect_timeout, context=context), base_path
    except http.client.InvalidURL as e:
        raise UsageError(f"bad server {server!r}: {e}") from e


def format_body(response: http.client.HTTPResponse, data: bytes) -> str:
    """Pretty-print JSON replies; pass anything else through as text."""
    text = data.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n"
    except ValueError:
        return text if not text or text.endswith("\n") else text + "\n"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        call = resolve_call(args, payload_required=False)
        payload = call["payload"]
        method = (args.method or call.get("method") or ("GET" if payload is None else "POST")).upper()
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        headers.update(_parse_headers(call, args.header))
        connection, base_path = open_connection(call["server"], args)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE

    path = base_path + "/" + str(call["endpoint"]).lstrip("/")
    with contextlib.closing(connection):
        try:
            connection.connect()
        except OSError as e:
            print(f"error: could not connect to {call['server']}: {e}", file=sys.stderr)
            return EXIT_CALL_FAILED
        try:
            connection.sock.settimeout(args.timeout)
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            text = format_body(response, response.read())
        except (OSError, http.client.HTTPException) as e:
            print(f"error: {method} {path} failed: {e or type(e).__name__}", file=sys.stderr)
            return EXIT_CALL_FAILED

    if not 200 <= response.status < 300:
        print(f"HTTP error: {response.status} {response.reason}", file=sys.stderr)
        sys.stderr.write(text)
        return EXIT_CALL_FAILED
    return write_reply(text, args.output)


if __name__ == "__main__":
    sys.exit(main())
