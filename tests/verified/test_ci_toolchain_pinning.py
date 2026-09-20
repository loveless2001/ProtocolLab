import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_ci_installs_hash_pinned_locked_toolchains():
    lock = json.loads(
        (ROOT / "verified/lifecycle/toolchain.lock.json").read_text()
    )
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    bend = lock["bend"]
    bun = lock["runtimes"]["bun"]
    for tool in (bend, bun):
        assert tool["version"] in workflow
        assert tool["release_archive"]["sha256"] in workflow
        assert tool["binary_sha256"] in workflow

    assert "bend-lang.com/install.sh" not in workflow
    assert "bun.sh/install" not in workflow
