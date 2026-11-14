"""serve: standalone HTTP service transforming one record per request.

The scalar transformation here is implemented independently of ``batch.py``
(which this module never imports): no NumPy, no per-row loop over a batch
function. Both paths load the same frozen artifact and apply the same
contract, so the output column names, ordering and values are identical.

Startup contract
----------------
The artifact is loaded and fully validated (structure, finite parameters,
column layout, digest) before the socket is opened. A corrupt or invalid
artifact makes the process exit non-zero; no port ever reports ready.
Afterwards exactly one frozen artifact is held for the process lifetime:
requests can neither extend the vocabulary nor mutate any shared state.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import contract
from .artifact import ArtifactError, load_artifact
from .contract import (
    CATEGORY_CODE_COLUMN,
    CATEGORY_KNOWN_COLUMN,
    CATEGORY_MISSING_COLUMN,
    INDICATOR_SUFFIX,
    NUMERIC_SCALED_SUFFIX,
    RecordError,
)

MAX_REQUEST_BYTES = 1 * 1024 * 1024


class ScalarTransformer:
    """Per-request scalar transform over an immutable frozen artifact."""

    def __init__(self, artifact: dict[str, Any]):
        # Read-only references. Nothing here is ever mutated while serving,
        # and each request builds its own fresh feature dict, so concurrent
        # requests cannot borrow each other's values.
        self.artifact = artifact
        spec = artifact["spec"]
        self.rid_field = spec["record_id"]
        self.numeric_fields = list(spec["numeric"])
        self.cat_field = spec["categorical"]
        self.numeric_stats = artifact["numeric_stats"]
        cat = artifact["categorical"]
        self.codes = cat["codes"]
        self.code_null = cat["code_null"]
        self.code_unknown = cat["code_unknown"]
        self.vocab_offset = cat["vocab_index_offset"]
        self.columns = list(artifact["output_layout"]["columns"])

    def transform(self, record: Any) -> dict[str, Any]:
        contract.validate_raw_record(
            record,
            record_id_field=self.rid_field,
            numeric_fields=self.numeric_fields,
            categorical_field=self.cat_field,
        )

        features: dict[str, Any] = {}
        for field in self.numeric_fields:
            stats = self.numeric_stats[field]
            mean = float(stats["mean"])
            std = float(stats["std"])
            value = record[field]
            if value is None:
                missing = 1
                filled = mean
            else:
                missing = 0
                filled = float(value)
            if std > 0.0:
                scaled = (filled - mean) / std
            else:
                # Constant / all-missing column: deterministic zero, no
                # division by zero; identical rule to the batch path.
                scaled = 0.0
            if not math.isfinite(scaled):
                # Artifact parameters are finite and validated; this is a
                # defensive guard, not an expected code path.
                raise RecordError(field, "transform produced non-finite value")
            features[f"{field}{NUMERIC_SCALED_SUFFIX}"] = scaled
            features[f"{field}{INDICATOR_SUFFIX}"] = missing

        cat_value = record[self.cat_field]
        if cat_value is None:
            code = self.code_null
        else:
            code = self.codes.get(cat_value, self.code_unknown)
        features[CATEGORY_MISSING_COLUMN] = 1 if code == self.code_null else 0
        features[CATEGORY_KNOWN_COLUMN] = 1 if code >= self.vocab_offset else 0
        features[CATEGORY_CODE_COLUMN] = code

        # Emit in the frozen column order rather than insertion order.
        return {name: features[name] for name in self.columns}


def build_handler(transformer: ScalarTransformer) -> type[BaseHTTPRequestHandler]:
    artifact = transformer.artifact
    digest = artifact["digest"]
    columns = list(artifact["output_layout"]["columns"])

    class TransformHandler(BaseHTTPRequestHandler):
        server_version = "FrozenFeaturizer/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
            pass

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status: int, path: str, message: str) -> None:
            self._send_json(
                status,
                {
                    "error": {
                        "status": status,
                        "path": path,
                        "message": message,
                    }
                },
            )

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            if self.path.rstrip("/") == "/health":
                self._send_json(
                    200,
                    {
                        "status": "ready",
                        "artifact_digest": digest,
                        "columns": columns,
                    },
                )
                return
            if self.path.rstrip("/") == "/transform":
                self._send_error(405, "", "POST /transform is required, GET is not supported")
                return
            self._send_error(404, self.path, "unknown path; use /health or /transform")

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/transform":
                self._send_error(404, self.path, "unknown path; use /transform")
                return

            length_header = self.headers.get("Content-Length")
            if length_header is None:
                self._send_error(411, "", "Content-Length header required")
                return
            try:
                length = int(length_header)
            except ValueError:
                self._send_error(400, "", "invalid Content-Length header")
                return
            if length < 0:
                self._send_error(400, "", "invalid Content-Length header")
                return
            if length > MAX_REQUEST_BYTES:
                self._send_error(413, "", f"request body exceeds {MAX_REQUEST_BYTES} bytes")
                return

            raw = self.rfile.read(length)
            try:
                record = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send_error(400, "", f"request body must be a single JSON object: {exc}")
                return
            if not isinstance(record, dict):
                self._send_error(400, "", "request body must be a JSON object representing one record")
                return

            try:
                # Fresh local dict per request; the transformer and artifact
                # stay immutable, so concurrent calls stay isolated.
                features = transformer.transform(record)
            except RecordError as exc:
                self._send_error(400, exc.path, exc.message)
                return

            self._send_json(
                200,
                {
                    "record_id": record[transformer.rid_field],
                    "artifact_digest": digest,
                    "columns": columns,
                    "features": features,
                },
            )

    return TransformHandler


def create_server(artifact_path: str, host: str, port: int) -> ThreadingHTTPServer:
    try:
        artifact = load_artifact(artifact_path)
    except (ArtifactError, OSError, json.JSONDecodeError) as exc:
        print(f"artifact validation failed, refusing to start: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    transformer = ScalarTransformer(artifact)
    httpd = ThreadingHTTPServer((host, port), build_handler(transformer))
    httpd.daemon_threads = True
    # Expose the single frozen artifact identity for operational checks.
    httpd.artifact_digest = artifact["digest"]  # type: ignore[attr-defined]
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the frozen featurizer over HTTP")
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    # Validation happens inside create_server before bind(); a bad artifact
    # exits the process without ever accepting traffic.
    httpd = create_server(args.artifact, args.host, args.port)
    print(
        json.dumps(
            {
                "status": "ready",
                "listen": f"http://{args.host}:{args.port}",
                "artifact_digest": httpd.artifact_digest,  # type: ignore[attr-defined]
                "endpoints": ["GET /health", "POST /transform"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
