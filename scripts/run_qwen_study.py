"""Launch a fresh Qwen smoke study with automated lifecycle management for Ollama and audited port adapter."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from managed_ollama import managed_ollama_port  # noqa: E402

UV_BIN = shutil.which("uv") or str(Path.home() / ".local/bin/uv")


def setup_fresh_run(target_dir: Path, base_dir: Path):
    target_dir = target_dir.resolve()
    base_dir = base_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. Copy required files if not present
    for fname in ("manifest.yaml", "port.lock.json", "ollama_port.py"):
        src = base_dir / fname
        dst = target_dir / fname
        if not dst.exists():
            shutil.copy2(src, dst)
            print(f"[setup] Copied {fname} to {target_dir.name}")

    # 2. Symlink models
    models_target = target_dir / "ollama-models"
    if not models_target.exists():
        src_models = base_dir / "ollama-models"
        if src_models.exists():
            models_target.symlink_to(src_models.resolve())
            print("[setup] Symlinked ollama-models")

    # 3. Lock experiment if not locked
    lock_file = target_dir / "experiment.lock.json"
    if not lock_file.exists():
        print("[setup] Locking experiment configuration...")
        subprocess.run(
            [
                UV_BIN, "run", "--no-sync", "protocollab", "lock",
                str(target_dir / "manifest.yaml"),
                "experiments/actor-smoke/development.json",
                "--output", str(lock_file),
            ],
            cwd=str(ROOT),
            check=True,
        )
        print(f"[setup] Created {lock_file.name}")


def main():
    parser = argparse.ArgumentParser(description="Run Qwen study with automatic Ollama lifecycle management")
    parser.add_argument("--run-dir", type=Path, default=ROOT / "runs/actor-smoke-qwen35-4b-20260918-run2")
    parser.add_argument("--base-dir", type=Path, default=ROOT / "runs/actor-smoke-qwen35-4b-20260918")
    parser.add_argument("--split", default="development")
    parser.add_argument("--scenarios", default="experiments/actor-smoke/development.json")
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    setup_fresh_run(run_dir, args.base_dir)

    print("\n==========================================")
    print(f"Launching Qwen Study in: {run_dir.name}")
    print("==========================================\n")

    with managed_ollama_port(run_dir):
        print("[study] Starting study execution with protocollab run...")
        study_output = run_dir / "study"
        cmd = [
            UV_BIN, "run", "--no-sync", "protocollab", "run",
            str(run_dir / "experiment.lock.json"),
            str(args.scenarios),
            "--split", args.split,
            "--output", str(study_output),
        ]
        result = subprocess.run(cmd, cwd=str(ROOT))
        if result.returncode != 0:
            print(f"[study] Execution finished with return code {result.returncode}")
            sys.exit(result.returncode)

    print(f"\n[study] Study finished. Report saved in {study_output}")


if __name__ == "__main__":
    main()
