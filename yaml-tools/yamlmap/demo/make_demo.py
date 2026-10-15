#!/usr/bin/env python3
"""Create a demo git repository with the directory layout yamlmap expects.

    python demo/make_demo.py /tmp/demo-configs
    cd /tmp/demo-configs && python /path/to/yamlmap/app.py --config yamlmap.yml
"""
import subprocess
import sys
from pathlib import Path

FILES = {
    "yamlmap.yml": """\
# yamlmap settings - run yamlmap from the repository root
base_dir: .
subgroup: prod          # the only subgroup shown for each group
hosts_file: hosts.txt   # list of hosts, lives in the subgroup directory
port: 8080
""",
    "dc-moscow/prod/hosts.txt": "# moscow production\nmsk-app-01\nmsk-app-02\nmsk-app-03\nmsk-db-01\n",
    "dc-moscow/prod/app/service.yml": """\
# main service settings
server:
  listen: 0.0.0.0:8443
  workers: 8
  tls:
    enabled: true
    cert: "/etc/ssl/app.crt"
    key: "/etc/ssl/app.key"
limits: &limits
  rps: 1500
  burst: 300
features:
  - name: search
    enabled: true
  - name: export
    enabled: false   # waiting for the new backend
cache:
  <<: *limits
  ttl: 30s
""",
    "dc-moscow/prod/app/features/flags.yaml": """\
new_ui: true
beta_users:
  - alice
  - bob
rollout_percent: 25
""",
    "dc-moscow/prod/postgres/pool.yml": """\
pool:
  size: 40
  timeout_seconds: 5.5
  idle: null
replicas:
  - host: msk-db-02
    weight: 1
  - host: msk-db-03
    weight: 2
""",
    "dc-moscow/prod/kafka/topics.yml": """\
topics:
  orders:
    partitions: 12
    retention_hours: 168
  audit:
    partitions: 3
    retention_hours: 720
""",
    "dc-spb/prod/hosts.txt": "spb-app-01\nspb-app-02\n",
    "dc-spb/prod/app/service.yml": """\
server:
  listen: 0.0.0.0:8443
  workers: 4
cache:
  ttl: 60s
""",
    "dc-spb/prod/postgres/pool.yml": "pool:\n  size: 20\n  timeout_seconds: 5\n",
    "dc-ekb/prod/hosts.txt": "ekb-app-01\n",
    "dc-ekb/prod/app/broken.yml": "server:\n  listen: [unclosed\n",
    "dc-ekb/stage/hosts.txt": "ekb-stage-01\n",
    "dc-ekb/stage/app/service.yml": "server:\n  workers: 1\n",
}


def main(target: str) -> None:
    root = Path(target)
    if root.exists() and any(root.iterdir()):
        raise SystemExit("target is not empty: %s" % root)
    for rel, body in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def git(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Demo User")
    git("config", "user.email", "demo@example.com")
    git("add", "-A")
    git("commit", "-q", "-m", "initial demo configs")
    print("demo repository created in", root)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo-configs")
