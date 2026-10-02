# grpc-call-tester

Two small CLIs for call testing: `grpc-call` sends a JSON payload to a gRPC endpoint and
prints the reply as JSON, and `rest-call` does the same for REST APIs (see [REST calls](#rest-calls)).

Message types are discovered through **server reflection**, so no `.proto` files are needed.
If the server has reflection disabled, pass a compiled descriptor set with `--protoset`.

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -e .          # add '.[dev]' for the tests
```

## Usage

Separate options:

```sh
grpc-call \
  --ca-cert ca.pem --client-cert client.pem --client-key client.key \
  --server myhost:443 \
  --endpoint my.package.MyService/MyMethod \
  --payload-file payload.json
```

Or one properties file holding server, endpoint and payload:

```sh
grpc-call --ca-cert ca.pem --client-cert client.pem --client-key client.key \
  --properties-file examples/properties.json
```

```json
{
  "server": "localhost:50051",
  "endpoint": "grpc.health.v1.Health/Check",
  "payload": { "service": "" }
}
```

`--properties-file` and `--server` / `--endpoint` / `--payload-file` are mutually exclusive.

### Options

| Option | Meaning |
| --- | --- |
| `--ca-cert FILE` | CA certificate (PEM) to verify the server. Without it the system trust store is used. |
| `--client-cert FILE`, `--client-key FILE` | Client certificate and key (PEM) for mTLS. Must be given together. |
| `-k`, `--insecure-skip-verify` | Skip server certificate checks; the connection is still encrypted. |
| `--plaintext` | No TLS at all. |
| `--timeout SEC` | Deadline for the call (default 30). |
| `--connect-timeout SEC` | How long to wait for the connection (default 10). |
| `-o`, `--output FILE` | Write the reply to a file instead of stdout. |
| `--protoset FILE` | Take message types from a descriptor set instead of reflection. |

A descriptor set is built with `protoc --include_imports --descriptor_set_out=api.protoset api.proto`.

### Streaming

- Server-streaming methods: the output is a JSON array of all replies.
- Client-streaming methods: make the payload a JSON array, one element per request message.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Call succeeded, reply written. |
| 1 | Connection failed or the server returned a gRPC error (status and details go to stderr). |
| 2 | Bad arguments, unreadable files, unknown service/method, payload not matching the request type. |

## REST calls

`rest-call` does the same for REST/JSON APIs. The TLS, timeout, output and properties-file
options are identical to `grpc-call`; the differences are:

- `--endpoint` is the request path (query string included), e.g. `/api/v1/items?limit=10`.
- `--payload-file` is optional. The method defaults to `POST` with a payload and `GET`
  without; override it with `-X` / `--method`.
- `-H 'Name: value'` adds a request header (repeatable).
- `--server` may be `host:port` or a URL such as `https://host/base`; `http://` implies `--plaintext`.

```sh
rest-call --ca-cert ca.pem --client-cert client.pem --client-key client.key \
  --server myhost:8443 --endpoint /api/v1/items --payload-file payload.json \
  -H 'Authorization: Bearer TOKEN'
```

The properties file takes two extra optional keys, `method` and `headers`
(see `examples/rest-properties.json`):

```json
{
  "server": "localhost:8443",
  "endpoint": "/api/v1/items",
  "method": "POST",
  "headers": { "Authorization": "Bearer TOKEN" },
  "payload": { "name": "example" }
}
```

A JSON reply is pretty-printed, anything else is passed through unchanged. Any status
outside 2xx is a failure: exit code 1, with the status line and the reply body on stderr.
Redirects are not followed.

## Pipelines

The tool never prompts, writes only the reply to stdout (errors go to stderr) and reports
the result through its exit code, so a failed call fails the pipeline step.

- **Certificates from CI secrets:** `GRPC_CALL_CA_CERT`, `GRPC_CALL_CLIENT_CERT` and
  `GRPC_CALL_CLIENT_KEY` hold file paths and are used when the matching option is absent.
- **Input from stdin:** `--payload-file -` or `--properties-file -`.

  ```sh
  envsubst < call.template.json | grpc-call --properties-file - | jq -e '.status == "SERVING"'
  ```

- **Container image:**

  ```sh
  docker build -t grpc-call-tester .
  docker run --rm -i -v "$PWD/certs:/certs:ro" \
    -e GRPC_CALL_CA_CERT=/certs/ca.pem \
    -e GRPC_CALL_CLIENT_CERT=/certs/client.pem \
    -e GRPC_CALL_CLIENT_KEY=/certs/client.key \
    grpc-call-tester --properties-file - < examples/properties.json
  ```

  The image runs as `nobody`, so mounted certificates must be readable by that user.
  For REST calls add `--entrypoint rest-call`.

- **Without a container:** `pip install .` (or build a wheel with `pip wheel . -w dist` and
  install that in the job).

## Tests

```sh
.venv/bin/pytest
```

The tests start a local gRPC health service and a local HTTP server (plaintext and mTLS
with throwaway certificates).
