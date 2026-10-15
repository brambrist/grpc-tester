import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))

import app as yamlmap  # noqa: E402
import make_demo  # noqa: E402

SERVICE = "dc-moscow/prod/app/service.yml"


@pytest.fixture()
def env(tmp_path):
    repo = tmp_path / "repo"
    make_demo.main(str(repo))
    settings = yamlmap.load_settings(str(repo / "yamlmap.yml"), {"base_dir": str(repo)})
    client = TestClient(yamlmap.create_app(settings, warm_up=False))
    return repo, client


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def open_file(client, rel):
    r = client.get("/api/file", params={"path": rel})
    assert r.status_code == 200, r.text
    return r.json()


def test_tree_follows_layout(env):
    _, client = env
    data = client.get("/api/tree").json()
    ids = {n["id"]: n for n in data["nodes"]}
    # only the configured subgroup is shown, 'stage' is ignored
    assert "dc-ekb/prod" in ids and "dc-ekb/stage" not in ids
    assert "dc-moscow/prod/app/features/flags.yaml" in ids
    assert ids["dc-moscow/prod/hosts.txt"]["kind"] == "hosts"
    assert ids["dc-moscow/prod/hosts.txt"]["count"] == 4
    assert ids["dc-moscow/prod/hosts.txt#msk-db-01"]["parent"] == "dc-moscow/prod/hosts.txt"
    assert ids["dc-ekb/prod/app/broken.yml"]["error"]
    assert data["stats"]["groups"] == 3 and data["stats"]["errors"] == 1
    # every non-root node has an existing parent
    assert all(n["parent"] in ids for n in data["nodes"] if n["kind"] != "root")


def test_model_and_lossless_roundtrip(env):
    _, client = env
    f = open_file(client, SERVICE)
    assert f["lossy"] is False and f["error"] is None
    keys = [i["key"] for i in f["model"]["items"]]
    assert keys == ["server", "limits", "features", "cache"]


def test_edit_keeps_comments_and_commits(env):
    repo, client = env
    f = open_file(client, SERVICE)
    ops = [
        {"op": "set", "path": ["server", "workers"], "value": {"type": "int", "value": "16"}},
        {"op": "set", "path": ["server", "tls", "cert"], "value": {"type": "str", "value": "/etc/ssl/new.crt"}},
        {"op": "set", "path": ["features", 1, "enabled"], "value": {"type": "bool", "value": True}},
        {"op": "add", "path": ["server"], "key": "timeout", "value": {"type": "float", "value": "2.5"}},
    ]
    body = {"path": SERVICE, "base_hash": f["hash"], "ops": ops}
    prev = client.post("/api/file/preview", json=body).json()
    assert prev["changed"] and "-  workers: 8" in prev["diff"] and "+  workers: 16" in prev["diff"]

    saved = client.post("/api/file/save", json=dict(body, message="raise workers")).json()
    assert saved["commit"]
    text = (repo / SERVICE).read_text()
    assert "# main service settings" in text
    assert "enabled: true   # waiting" not in text  # value changed ...
    assert "# waiting for the new backend" in text  # ... comment kept
    assert 'cert: "/etc/ssl/new.crt"' in text       # quote style kept
    assert "&limits" in text and "<<: *limits" in text
    assert "timeout: 2.5" in text
    assert git(repo, "log", "-1", "--format=%s").strip() == "raise workers"
    assert git(repo, "status", "--porcelain").strip() == ""
    hist = client.get("/api/history", params={"path": SERVICE}).json()["commits"]
    assert hist[0]["subject"] == "raise workers" and len(hist) == 2


def test_list_ops_and_delete(env):
    repo, client = env
    rel = "dc-moscow/prod/app/features/flags.yaml"
    f = open_file(client, rel)
    ops = [
        {"op": "add", "path": ["beta_users"], "value": {"type": "str", "value": "carol"}},
        {"op": "delete", "path": ["beta_users", 0]},
        {"op": "delete", "path": ["rollout_percent"]},
    ]
    r = client.post("/api/file/save", json={"path": rel, "base_hash": f["hash"], "ops": ops})
    assert r.status_code == 200, r.text
    assert (repo / rel).read_text() == "new_ui: true\nbeta_users:\n  - bob\n  - carol\n"


def test_text_edit_validates_yaml(env):
    _, client = env
    f = open_file(client, SERVICE)
    bad = client.post("/api/file/preview", json={"path": SERVICE, "base_hash": f["hash"], "text": "a: [oops\n"})
    assert bad.status_code == 400 and "valid YAML" in bad.json()["detail"]
    dup = client.post("/api/file/preview", json={"path": SERVICE, "base_hash": f["hash"], "text": "a: 1\na: 2\n"})
    assert dup.status_code == 400


def test_hosts_file_text_edit(env):
    repo, client = env
    rel = "dc-spb/prod/hosts.txt"
    f = open_file(client, rel)
    assert f["hosts"] == ["spb-app-01", "spb-app-02"]
    r = client.post("/api/file/save", json={"path": rel, "base_hash": f["hash"],
                                            "text": "spb-app-01\nspb-app-02\nspb-app-03\n"})
    assert r.status_code == 200
    tree = client.get("/api/tree").json()["nodes"]
    assert any(n["id"] == rel + "#spb-app-03" for n in tree)


def test_conflict_when_file_changed_on_disk(env):
    repo, client = env
    f = open_file(client, SERVICE)
    (repo / SERVICE).write_text((repo / SERVICE).read_text() + "extra: 1\n")
    r = client.post("/api/file/preview", json={
        "path": SERVICE, "base_hash": f["hash"],
        "ops": [{"op": "set", "path": ["server", "workers"], "value": {"type": "int", "value": "1"}}]})
    assert r.status_code == 409


def test_git_dirty_flag(env):
    repo, client = env
    assert open_file(client, SERVICE)["git_dirty"] is False
    (repo / SERVICE).write_text((repo / SERVICE).read_text() + "extra: 1\n")
    assert open_file(client, SERVICE)["git_dirty"] is True


def test_invalid_ops_rejected_and_nothing_written(env):
    repo, client = env
    f = open_file(client, SERVICE)
    before = (repo / SERVICE).read_text()
    for op in (
        {"op": "set", "path": ["server", "workers"], "value": {"type": "int", "value": "many"}},
        {"op": "set", "path": ["nope", "x"], "value": {"type": "int", "value": "1"}},
        {"op": "add", "path": ["server"], "key": "workers", "value": {"type": "int", "value": "1"}},
        {"op": "delete", "path": []},
    ):
        r = client.post("/api/file/save", json={"path": SERVICE, "base_hash": f["hash"], "ops": [op]})
        assert r.status_code == 400, (op, r.text)
    assert (repo / SERVICE).read_text() == before


def test_path_traversal_and_scope(env):
    _, client = env
    for bad in ("../../etc/passwd", "/etc/passwd", "dc-ekb/stage/app/service.yml",
                "yamlmap.yml", "dc-moscow/prod/../../yamlmap.yml", "dc-moscow/.git/config"):
        assert client.get("/api/file", params={"path": bad}).status_code in (400, 404), bad


def test_symlink_escape_blocked(env, tmp_path):
    repo, client = env
    outside = tmp_path / "secret.yml"
    outside.write_text("token: abc\n")
    (repo / "dc-spb/prod/app/link.yml").symlink_to(outside)
    assert client.get("/api/file", params={"path": "dc-spb/prod/app/link.yml"}).status_code in (400, 404)


def test_cross_origin_write_rejected(env):
    _, client = env
    f = open_file(client, SERVICE)
    r = client.post("/api/file/save", headers={"Origin": "http://evil.example"},
                    json={"path": SERVICE, "base_hash": f["hash"], "text": "a: 1\n"})
    assert r.status_code == 403


def test_search(env):
    _, client = env

    def search(q, **kw):
        r = client.get("/api/search", params=dict(q=q, **kw))
        assert r.status_code == 200, r.text
        return r.json()["hits"]

    hits = search("workers", values="false", names="false")
    assert {h["node"] for h in hits} == {SERVICE, "dc-spb/prod/app/service.yml"}
    assert all(h["where"] == "key" for h in hits)

    hits = search("msk-db-02", keys="false", names="false")
    assert [h["node"] for h in hits] == ["dc-moscow/prod/postgres/pool.yml"]
    assert hits[0]["path_text"] == "replicas[0].host"

    hits = search("msk-app", keys="false", values="false")
    assert {h["kind"] for h in hits} == {"host"} and len(hits) == 3

    assert search("server.tls.cert")[0]["path"] == ["server", "tls", "cert"]
    assert search(r"^pool$", regex="true", values="false", names="false")
    assert client.get("/api/search", params={"q": "(", "regex": "true"}).status_code == 400


def test_crlf_and_indent_preserved(env):
    repo, client = env
    rel = "dc-spb/prod/app/crlf.yml"
    (repo / rel).write_bytes(b"a:\r\n    b:\r\n        - 1\r\n        - 2\r\n")
    f = open_file(client, rel)
    assert f["lossy"] is False
    r = client.post("/api/file/save", json={
        "path": rel, "base_hash": f["hash"],
        "ops": [{"op": "set", "path": ["a", "b", 0], "value": {"type": "int", "value": "9"}}]})
    assert r.status_code == 200
    assert (repo / rel).read_bytes() == b"a:\r\n    b:\r\n        - 9\r\n        - 2\r\n"


@pytest.mark.parametrize("sample", [
    "a:\n  b: 1\n  l:\n  - x\n  - y\n",
    "a:\n  b: 1\n  l:\n    - x\n    - y\n",
    "---\na:\n    b: 1   # c\n    l:\n        - x\n",
    "a: 1\nb: [1, 2, 3]\nc: {x: 1}\n",
    "k: |\n  multi\n  line\nq: 'single'\n",
])
def test_roundtrip_is_lossless(sample):
    assert yamlmap.Doc(sample).dump() == sample


def test_commit_failure_restores_file(env, monkeypatch):
    repo, client = env
    f = open_file(client, SERVICE)
    before = (repo / SERVICE).read_text()
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho rejected by hook >&2\nexit 1\n")
    hook.chmod(0o755)
    r = client.post("/api/file/save", json={
        "path": SERVICE, "base_hash": f["hash"],
        "ops": [{"op": "set", "path": ["server", "workers"], "value": {"type": "int", "value": "2"}}]})
    assert r.status_code == 500 and "restored" in r.json()["detail"]
    assert (repo / SERVICE).read_text() == before
    assert git(repo, "status", "--porcelain").strip() == ""


def test_merge_key_inherited_values(env):
    repo, client = env
    f = open_file(client, SERVICE)
    cache = next(i for i in f["model"]["items"] if i["key"] == "cache")["node"]["items"]
    flags = {i["key"]: i.get("inherited", False) for i in cache}
    assert flags == {"ttl": False, "rps": True, "burst": True}
    # deleting an inherited key would be a silent no-op, so it is refused
    r = client.post("/api/file/preview", json={"path": SERVICE, "base_hash": f["hash"],
                                               "ops": [{"op": "delete", "path": ["cache", "rps"]}]})
    assert r.status_code == 400 and "inherited" in r.json()["detail"]
    # overriding creates a local value and keeps the merge
    r = client.post("/api/file/save", json={"path": SERVICE, "base_hash": f["hash"], "ops": [
        {"op": "set", "path": ["cache", "rps"], "value": {"type": "int", "value": "5"}}]})
    assert r.status_code == 200, r.text
    text = (repo / SERVICE).read_text()
    assert "<<: *limits" in text and "rps: 5" in text and "rps: 1500" in text
