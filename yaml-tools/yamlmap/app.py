#!/usr/bin/env python3
"""yamlmap - mind-map style web editor for a directory tree of YAML configs.

Layout of the scanned tree (names of <subgroup> and <hosts_file> come from the config):

    <base_dir>/<group>/<subgroup>/<sub directories with *.yml|*.yaml>
    <base_dir>/<group>/<subgroup>/<hosts_file>       one host per line, '#' starts a comment

Run it from inside the git repository that holds the configs:

    python app.py --config yamlmap.yml
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarbool import ScalarBoolean
from ruamel.yaml.scalarstring import ScalarString

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_FILE_BYTES = 5 * 1024 * 1024
SEARCH_LIMIT = 500


# --------------------------------------------------------------------------- settings


@dataclass
class Settings:
    base_dir: Path
    subgroup: str
    hosts_file: str
    extensions: Tuple[str, ...] = (".yml", ".yaml")
    ignore_dirs: Tuple[str, ...] = (".git", "node_modules", "__pycache__")
    git_commit: bool = True
    host: str = "127.0.0.1"
    port: int = 8080


def load_settings(config_path: Optional[str], overrides: Dict[str, Any]) -> Settings:
    raw: Dict[str, Any] = {}
    if config_path:
        p = Path(config_path)
        if not p.is_file():
            raise SystemExit("config file not found: %s" % p)
        loaded = YAML(typ="safe").load(p.read_text(encoding="utf-8"))
        if loaded is not None and not isinstance(loaded, dict):
            raise SystemExit("config must be a YAML mapping")
        raw = dict(loaded or {})
    raw.update({k: v for k, v in overrides.items() if v is not None})

    missing = [k for k in ("subgroup", "hosts_file") if not raw.get(k)]
    if missing:
        raise SystemExit("config is missing required keys: %s" % ", ".join(missing))

    subgroup = str(raw["subgroup"])
    hosts_file = str(raw["hosts_file"])
    for label, value in (("subgroup", subgroup), ("hosts_file", hosts_file)):
        if "/" in value or "\\" in value or value in (".", ".."):
            raise SystemExit("%s must be a plain name, not a path: %r" % (label, value))

    exts = tuple(str(e).lower() if str(e).startswith(".") else "." + str(e).lower()
                 for e in raw.get("extensions", (".yml", ".yaml")))
    return Settings(
        base_dir=Path(raw.get("base_dir", ".")).expanduser().resolve(),
        subgroup=subgroup,
        hosts_file=hosts_file,
        extensions=exts,
        ignore_dirs=tuple(raw.get("ignore_dirs", (".git", "node_modules", "__pycache__"))),
        git_commit=bool(raw.get("git_commit", True)),
        host=str(raw.get("host", "127.0.0.1")),
        port=int(raw.get("port", 8080)),
    )


# --------------------------------------------------------------------------- yaml helpers


def new_yaml() -> YAML:
    y = YAML()  # round-trip: keeps comments, anchors, ordering
    y.preserve_quotes = True
    y.width = 4096  # never re-wrap long lines
    return y


def _leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def guess_indent(text: str) -> Tuple[int, int, int]:
    """Return (mapping indent, sequence indent, dash offset) used by the document."""
    map_indent: Optional[int] = None
    seq: Optional[Tuple[int, int]] = None
    prev_key_indent: Optional[int] = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lead = _leading_spaces(line)
        if prev_key_indent is not None:
            if stripped.startswith("- ") and seq is None:
                offset = lead - prev_key_indent
                if offset >= 0:
                    content = len(line[lead + 1:]) - len(line[lead + 1:].lstrip(" "))
                    seq = (offset + 1 + content, offset)
            elif not stripped.startswith("-") and map_indent is None and lead > prev_key_indent:
                map_indent = lead - prev_key_indent
        prev_key_indent = lead if stripped.endswith(":") and not stripped.startswith("- ") else None
        if map_indent is not None and seq is not None:
            break
    seq_indent, offset = seq if seq else (4, 2)
    return map_indent or 2, seq_indent, offset


def _detect_eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _has_explicit_start(text: str) -> bool:
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        return s.startswith("---")
    return False


def _single_document(text: str) -> bool:
    docs = list(new_yaml().load_all(text))
    return len(docs) <= 1


class Doc:
    """A round-trip loaded YAML document plus the formatting needed to dump it back."""

    def __init__(self, text: str):
        self.eol = _detect_eol(text)
        self.explicit_start = _has_explicit_start(text)
        self.indent = guess_indent(text)
        self.data = new_yaml().load(text)

    def dump(self) -> str:
        y = new_yaml()
        y.indent(mapping=self.indent[0], sequence=self.indent[1], offset=self.indent[2])
        y.explicit_start = self.explicit_start
        buf = io.StringIO()
        y.dump(self.data, buf)
        out = buf.getvalue()
        return out.replace("\n", self.eol) if self.eol != "\n" else out


def yaml_error_text(exc: Exception) -> str:
    return re.sub(r"\n{3,}", "\n\n", str(exc)).strip()[:600]


def scalar_type(obj: Any) -> str:
    if obj is None:
        return "null"
    if isinstance(obj, (bool, ScalarBoolean)):
        return "bool"
    if isinstance(obj, int):
        return "int"
    if isinstance(obj, float):
        return "float"
    if isinstance(obj, str):
        return "str"
    return "other"


def _own_keys(obj: Any) -> Optional[set]:
    """Keys defined directly in a mapping, or None when the mapping has no merge keys."""
    if isinstance(obj, CommentedMap) and getattr(obj, "merge", None):
        own = getattr(obj, "_ok", None)
        return set(own) if own is not None else None
    return None


def to_model(obj: Any) -> Dict[str, Any]:
    """JSON-friendly typed tree for the key editor."""
    if isinstance(obj, dict):
        own = _own_keys(obj)
        items = []
        for k, v in obj.items():
            key = k if isinstance(k, (str, int, float, bool)) or k is None else str(k)
            item = {"key": key, "node": to_model(v)}
            if own is not None and k not in own:
                item["inherited"] = True  # comes from a YAML merge key (<<: *anchor)
            items.append(item)
        return {"type": "map", "items": items}
    if isinstance(obj, list):
        return {"type": "seq", "items": [to_model(v) for v in obj]}
    t = scalar_type(obj)
    if t == "null":
        return {"type": "null"}
    if t == "bool":
        return {"type": "bool", "value": bool(obj)}
    if t in ("int", "float"):
        return {"type": t, "value": str(obj)}
    if t == "str":
        return {"type": "str", "value": str(obj)}
    return {"type": "other", "value": str(obj)}


def build_value(spec: Any, old: Any = None) -> Any:
    if not isinstance(spec, dict) or "type" not in spec:
        raise HTTPException(400, "value must be an object with a 'type'")
    t, v = spec["type"], spec.get("value")
    try:
        if t == "str":
            s = "" if v is None else str(v)
            if isinstance(old, ScalarString):
                return type(old)(s)  # keep quoting / block style
            return s
        if t == "int":
            s = str(v).strip()
            return int(s, 0) if re.fullmatch(r"[-+]?0([xX][0-9a-fA-F]+|[oO][0-7]+|[bB][01]+)", s) else int(s)
        if t == "float":
            f = float(str(v).strip())
            if f != f or f in (float("inf"), float("-inf")):
                raise ValueError("nan/inf not allowed")
            return f
        if t == "bool":
            if isinstance(v, bool):
                return v
            if str(v).strip().lower() in ("true", "yes", "1"):
                return True
            if str(v).strip().lower() in ("false", "no", "0"):
                return False
            raise ValueError("expected true or false")
        if t == "null":
            return None
        if t == "map":
            return CommentedMap()
        if t == "seq":
            return CommentedSeq()
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "invalid %s value %r: %s" % (t, v, exc))
    raise HTTPException(400, "unsupported value type %r" % t)


def _nav(root: Any, path: List[Any]) -> Any:
    cur = root
    for part in path:
        try:
            cur = cur[part]
        except (KeyError, IndexError, TypeError):
            raise HTTPException(400, "path not found: %s" % format_path(path))
    return cur


def format_path(path: List[Any]) -> str:
    out = ""
    for p in path:
        out += "[%d]" % p if isinstance(p, int) and not isinstance(p, bool) else ("." if out else "") + str(p)
    return out or "(root)"


def apply_ops(root: Any, ops: List[Dict[str, Any]]) -> None:
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or not isinstance(op.get("path"), list):
            raise HTTPException(400, "op #%d is malformed" % i)
        kind, path = op.get("op"), op["path"]
        if kind == "set":
            if not path:
                raise HTTPException(400, "cannot replace the document root")
            parent, key = _nav(root, path[:-1]), path[-1]
            old = _nav(parent, [key])
            parent[key] = build_value(op.get("value"), old)
        elif kind == "delete":
            if not path:
                raise HTTPException(400, "cannot delete the document root")
            parent, key = _nav(root, path[:-1]), path[-1]
            _nav(parent, [key])
            own = _own_keys(parent)
            if own is not None and key not in own:
                raise HTTPException(400, "%s is inherited via a merge key (<<); delete it in the anchor "
                                         "or override it instead" % format_path(path))
            del parent[key]
        elif kind == "add":
            container = _nav(root, path)
            value = build_value(op.get("value"))
            if isinstance(container, dict):
                key = op.get("key")
                if not isinstance(key, str) or not key:
                    raise HTTPException(400, "new key must be a non-empty string")
                if key in container:
                    raise HTTPException(400, "key already exists: %s" % format_path(path + [key]))
                container[key] = value
            elif isinstance(container, list):
                container.append(value)
            else:
                raise HTTPException(400, "cannot add into a scalar at %s" % format_path(path))
        else:
            raise HTTPException(400, "unknown op %r" % kind)


def parse_hosts(text: str) -> List[str]:
    seen, hosts = set(), []
    for line in text.splitlines():
        s = line.split("#", 1)[0].strip()
        if s:
            name = s.split()[0]
            if name not in seen:
                seen.add(name)
                hosts.append(name)
    return hosts


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(path: Path) -> str:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise HTTPException(413, "file is larger than %d MB" % (MAX_FILE_BYTES // 1024 // 1024))
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except UnicodeDecodeError:
        raise HTTPException(415, "file is not valid UTF-8")


def write_atomic(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o7777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".yamlmap-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------- index (search cache)


class FileInfo:
    __slots__ = ("stamp", "error", "top_keys", "entries")

    def __init__(self, stamp, error, top_keys, entries):
        self.stamp = stamp
        self.error = error
        self.top_keys = top_keys
        # entries: (path list, last key (str), value text or None for containers)
        self.entries = entries


def _flatten(obj: Any, path: List[Any], out: list) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = path + [k if isinstance(k, (str, int, float, bool)) or k is None else str(k)]
            out.append((p, str(k), None if isinstance(v, (dict, list)) else _scalar_text(v)))
            _flatten(v, p, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = path + [i]
            out.append((p, "[%d]" % i, None if isinstance(v, (dict, list)) else _scalar_text(v)))
            _flatten(v, p, out)


def _scalar_text(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


class Index:
    def __init__(self) -> None:
        self._cache: Dict[Path, FileInfo] = {}
        self._lock = threading.Lock()

    def get(self, path: Path) -> FileInfo:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._cache.get(path)
        if hit and hit.stamp == stamp:
            return hit
        error, top, entries = None, 0, []
        try:
            data = YAML(typ="safe").load(read_text(path))
            if isinstance(data, dict):
                top = len(data)
            elif isinstance(data, list):
                top = len(data)
            _flatten(data, [], entries)
        except HTTPException as exc:
            error = str(exc.detail)
        except (YAMLError, ValueError) as exc:
            error = yaml_error_text(exc)
        info = FileInfo(stamp, error, top, entries)
        with self._lock:
            self._cache[path] = info
        return info


# --------------------------------------------------------------------------- git


class Git:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.root: Optional[Path] = None
        out = self._run(["rev-parse", "--show-toplevel"], cwd=base_dir)
        if out.returncode == 0:
            self.root = Path(out.stdout.strip()).resolve()

    @staticmethod
    def _run(args: List[str], cwd: Path, timeout: int = 60) -> "subprocess.CompletedProcess[str]":
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C.UTF-8")
        try:
            return subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True,
                                  timeout=timeout, env=env, encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return subprocess.CompletedProcess(args, 127, "", "git executable not found")
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 124, "", "git timed out")

    @property
    def available(self) -> bool:
        return self.root is not None

    def identity_ok(self) -> bool:
        if not self.root:
            return False
        name = self._run(["config", "user.name"], cwd=self.root).stdout.strip()
        mail = self._run(["config", "user.email"], cwd=self.root).stdout.strip()
        return bool(name and mail)

    def rel(self, path: Path) -> str:
        assert self.root
        return os.path.relpath(str(path), str(self.root))

    def is_dirty(self, path: Path) -> bool:
        if not self.root:
            return False
        out = self._run(["status", "--porcelain", "--", self.rel(path)], cwd=self.root)
        return out.returncode == 0 and bool(out.stdout.strip())

    def commit(self, path: Path, message: str) -> Tuple[bool, str]:
        assert self.root
        rel = self.rel(path)
        add = self._run(["add", "--", rel], cwd=self.root)
        if add.returncode != 0:
            return False, add.stderr.strip() or add.stdout.strip()
        res = self._run(["commit", "-m", message, "--", rel], cwd=self.root)
        if res.returncode != 0:
            self._run(["reset", "-q", "--", rel], cwd=self.root)
            return False, (res.stderr.strip() or res.stdout.strip())
        head = self._run(["rev-parse", "--short", "HEAD"], cwd=self.root).stdout.strip()
        return True, head

    def history(self, path: Path, limit: int = 30) -> List[Dict[str, str]]:
        if not self.root:
            return []
        fmt = "%h%x1f%an%x1f%ad%x1f%s"
        out = self._run(["log", "-n", str(limit), "--date=format:%Y-%m-%d %H:%M", "--format=" + fmt,
                         "--", self.rel(path)], cwd=self.root)
        rows = []
        for line in out.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                rows.append(dict(zip(("hash", "author", "date", "subject"), parts)))
        return rows


# --------------------------------------------------------------------------- app


class EditRequest(BaseModel):
    path: str
    base_hash: str
    ops: Optional[List[Dict[str, Any]]] = None
    text: Optional[str] = None
    message: Optional[str] = None


def create_app(settings: Settings, warm_up: bool = True) -> FastAPI:
    app = FastAPI(title="yamlmap", docs_url=None, redoc_url=None, openapi_url=None)
    base = settings.base_dir
    index = Index()
    git = Git(base)
    write_lock = threading.Lock()

    # ---- guards

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host", ""):
                return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def kind_of(rel: str) -> str:
        """Validate that rel is an editable file and return 'yaml' or 'hosts'."""
        pure = PurePosixPath(rel)
        parts = pure.parts
        if not rel or pure.is_absolute() or ".." in parts or "\\" in rel or len(parts) < 3:
            raise HTTPException(400, "invalid path")
        if parts[1] != settings.subgroup or any(p.startswith(".") for p in parts) \
                or any(p in settings.ignore_dirs for p in parts):
            raise HTTPException(400, "path is outside the configured subgroup")
        full = (base / rel).resolve()
        if base not in full.parents or not full.is_file():
            raise HTTPException(404, "file not found")
        if len(parts) == 3 and parts[2] == settings.hosts_file:
            return "hosts"
        if full.suffix.lower() in settings.extensions:
            return "yaml"
        raise HTTPException(400, "not an editable file type")

    def full_path(rel: str) -> Path:
        return (base / rel).resolve()

    # ---- tree

    def visible_dirs(parent: Path) -> List[Path]:
        try:
            entries = sorted(parent.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return []
        return [p for p in entries if p.is_dir() and not p.name.startswith(".")
                and p.name not in settings.ignore_dirs]

    def scan() -> Dict[str, Any]:
        nodes: List[Dict[str, Any]] = [{"id": "", "label": base.name or str(base), "kind": "root", "parent": None}]
        skipped: List[str] = []
        stats = {"groups": 0, "files": 0, "hosts": 0, "errors": 0}

        def walk(directory: Path, rel: str, is_sub_root: bool) -> None:
            try:
                entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                return
            for entry in entries:
                name = entry.name
                if name.startswith(".") or name in settings.ignore_dirs:
                    continue
                child = rel + "/" + name
                if entry.is_dir():
                    nodes.append({"id": child, "label": name, "kind": "dir", "parent": rel})
                    walk(entry, child, False)
                elif is_sub_root and name == settings.hosts_file:
                    try:
                        hosts = parse_hosts(read_text(entry))
                        err = None
                    except HTTPException as exc:
                        hosts, err = [], str(exc.detail)
                    nodes.append({"id": child, "label": name, "kind": "hosts", "parent": rel,
                                  "count": len(hosts), "error": err})
                    for h in hosts:
                        nodes.append({"id": child + "#" + h, "label": h, "kind": "host", "parent": child})
                    stats["hosts"] += len(hosts)
                elif entry.is_file() and entry.suffix.lower() in settings.extensions:
                    try:
                        info = index.get(entry)
                        node = {"id": child, "label": name, "kind": "file", "parent": rel,
                                "keys": info.top_keys, "error": info.error}
                    except OSError as exc:
                        node = {"id": child, "label": name, "kind": "file", "parent": rel,
                                "keys": 0, "error": str(exc)}
                    stats["files"] += 1
                    stats["errors"] += 1 if node["error"] else 0
                    nodes.append(node)

        for group in visible_dirs(base):
            sub = group / settings.subgroup
            if not sub.is_dir():
                skipped.append(group.name)
                continue
            stats["groups"] += 1
            nodes.append({"id": group.name, "label": group.name, "kind": "group", "parent": ""})
            sid = group.name + "/" + settings.subgroup
            nodes.append({"id": sid, "label": settings.subgroup, "kind": "subgroup", "parent": group.name})
            walk(sub, sid, True)
        return {"nodes": nodes, "skipped_groups": skipped, "stats": stats}

    def warm_index() -> None:
        """Parse every YAML file once in the background so the first tree/search request is fast."""
        try:
            for group in visible_dirs(base):
                sub = group / settings.subgroup
                if not sub.is_dir():
                    continue
                for path in sorted(sub.rglob("*")):
                    rel_parts = path.relative_to(sub).parts
                    if any(p.startswith(".") or p in settings.ignore_dirs for p in rel_parts):
                        continue
                    if path.is_file() and path.suffix.lower() in settings.extensions:
                        index.get(path)
        except Exception:  # warm-up is best effort
            pass

    if warm_up:
        threading.Thread(target=warm_index, name="yamlmap-warmup", daemon=True).start()

    # ---- routes

    @app.get("/")
    def home():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    def api_config():
        return {"base_dir": str(base), "subgroup": settings.subgroup, "hosts_file": settings.hosts_file,
                "git": git.available, "git_commit": settings.git_commit and git.available}

    @app.get("/api/tree")
    def api_tree():
        return scan()

    @app.get("/api/file")
    def api_file(path: str):
        kind = kind_of(path)
        fp = full_path(path)
        text = read_text(fp)
        resp: Dict[str, Any] = {"path": path, "kind": kind, "text": text, "hash": sha(text),
                                "git_dirty": git.is_dirty(fp), "model": None, "error": None,
                                "lossy": False, "multi_doc": False}
        if kind == "hosts":
            resp["hosts"] = parse_hosts(text)
            return resp
        try:
            if not _single_document(text):
                resp["multi_doc"] = True
                return resp
            doc = Doc(text)
            resp["model"] = to_model(doc.data)
            resp["lossy"] = doc.dump() != text
        except YAMLError as exc:
            resp["error"] = yaml_error_text(exc)
        return resp

    def compute_new_text(req: EditRequest) -> Tuple[str, str, Path, str]:
        kind = kind_of(req.path)
        fp = full_path(req.path)
        old = read_text(fp)
        if sha(old) != req.base_hash:
            raise HTTPException(409, "file changed on disk since it was opened; reload it")
        if req.text is not None and req.ops is not None:
            raise HTTPException(400, "send either 'text' or 'ops'")
        if req.text is not None:
            eol = _detect_eol(old)
            new = req.text.replace("\r\n", "\n").replace("\n", eol)
        elif req.ops is not None:
            if kind != "yaml":
                raise HTTPException(400, "key edits are only available for YAML files")
            try:
                doc = Doc(old)
            except YAMLError as exc:
                raise HTTPException(400, "file is not valid YAML: " + yaml_error_text(exc))
            apply_ops(doc.data, req.ops)
            new = doc.dump()
        else:
            raise HTTPException(400, "nothing to apply")
        if kind == "yaml":
            try:
                if not _single_document(new):
                    raise HTTPException(400, "multi-document YAML can only be edited as text without adding documents")
            except YAMLError as exc:
                raise HTTPException(400, "result is not valid YAML: " + yaml_error_text(exc))
        return old, new, fp, kind

    @app.post("/api/file/preview")
    def api_preview(req: EditRequest):
        old, new, _, _ = compute_new_text(req)
        diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                            "a/" + req.path, "b/" + req.path, n=3))
        return {"changed": old != new, "diff": diff, "new_text": new, "new_hash": sha(new)}

    @app.post("/api/file/save")
    def api_save(req: EditRequest):
        with write_lock:
            old, new, fp, _ = compute_new_text(req)
            if old == new:
                raise HTTPException(400, "no changes to save")
            message = (req.message or "").strip() or "yamlmap: update %s" % req.path
            if settings.git_commit and git.available and not git.identity_ok():
                raise HTTPException(500, "git user.name / user.email are not configured; commit refused")
            write_atomic(fp, new)
            commit_id = None
            if settings.git_commit and git.available:
                ok, info = git.commit(fp, message)
                if not ok:
                    write_atomic(fp, old)  # keep disk and history consistent
                    raise HTTPException(500, "git commit failed, file restored: " + info)
                commit_id = info
        return {"hash": sha(new), "commit": commit_id, "message": message}

    @app.get("/api/history")
    def api_history(path: str):
        kind_of(path)
        return {"commits": git.history(full_path(path))}

    @app.get("/api/search")
    def api_search(q: str, keys: bool = True, values: bool = True, names: bool = True,
                   regex: bool = False, case: bool = False):
        q = q.strip()
        if not q:
            return {"hits": [], "truncated": False}
        if regex:
            try:
                pattern = re.compile(q, 0 if case else re.IGNORECASE)
            except re.error as exc:
                raise HTTPException(400, "bad regular expression: %s" % exc)
            match = lambda s: pattern.search(s) is not None  # noqa: E731
        else:
            needle = q if case else q.lower()
            match = (lambda s: needle in s) if case else (lambda s: needle in s.lower())  # noqa: E731

        hits: List[Dict[str, Any]] = []
        truncated = False
        tree = scan()
        for node in tree["nodes"]:
            if node["kind"] == "root":
                continue
            if names and match(node["label"]):
                hits.append({"node": node["id"], "kind": node["kind"], "label": node["label"],
                             "where": "name", "path": None, "value": None})
            if node["kind"] != "file" or node.get("error"):
                continue
            if not (keys or values):
                continue
            info = index.get(base / node["id"])
            per_file = 0
            for path, last_key, value in info.entries:
                key_hit = keys and (match(last_key) or
                                    (("." in q or "/" in q) and match(format_path(path))))
                value_hit = values and value is not None and match(value)
                if key_hit or value_hit:
                    hits.append({"node": node["id"], "kind": "file", "label": node["label"],
                                 "where": "key" if key_hit else "value", "path": path,
                                 "path_text": format_path(path),
                                 "value": None if value is None else value[:160]})
                    per_file += 1
                    if per_file >= 50:
                        break
            if len(hits) >= SEARCH_LIMIT:
                truncated = True
                break
        return {"hits": hits[:SEARCH_LIMIT], "truncated": truncated}

    return app


# --------------------------------------------------------------------------- cli


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Mind-map editor for a tree of YAML configs")
    ap.add_argument("--config", default=None, help="config file (default: ./yamlmap.yml if it exists)")
    ap.add_argument("--base-dir", default=None)
    ap.add_argument("--subgroup", default=None)
    ap.add_argument("--hosts-file", default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    config = args.config or ("yamlmap.yml" if Path("yamlmap.yml").is_file() else None)
    settings = load_settings(config, {"base_dir": args.base_dir, "subgroup": args.subgroup,
                                      "hosts_file": args.hosts_file, "host": args.host, "port": args.port})
    if not settings.base_dir.is_dir():
        raise SystemExit("base_dir is not a directory: %s" % settings.base_dir)

    git = Git(settings.base_dir)
    if settings.git_commit and not git.available:
        raise SystemExit("base_dir is not inside a git repository (set git_commit: false to edit without commits)")
    if settings.git_commit and not git.identity_ok():
        raise SystemExit("git user.name / user.email are not configured in this repository")

    import uvicorn

    print("yamlmap: base_dir=%s subgroup=%s hosts_file=%s git=%s" % (
        settings.base_dir, settings.subgroup, settings.hosts_file, "commit" if settings.git_commit else "off"))
    print("yamlmap: http://%s:%d/" % (settings.host, settings.port))
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
