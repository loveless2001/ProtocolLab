"""Automated, robust lifecycle manager for dedicated Ollama instance and audited port adapter."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

UV_BIN = shutil.which("uv") or str(Path.home() / ".local/bin/uv")


@contextmanager
def managed_ollama_port(
    run_dir: Path,
    port: int = 11436,
    ollama_port: int = 11435,
    models_dir: Path | None = None,
    startup_timeout: int = 45,
):
    run_dir = Path(run_dir).resolve()
    port_lock_path = run_dir / "port.lock.json"
    if not port_lock_path.exists():
        raise FileNotFoundError(f"Missing {port_lock_path}")
    port_lock = json.loads(port_lock_path.read_text())

    # Ensure models dir
    if models_dir is None:
        models_dir = run_dir / "ollama-models"
    if not models_dir.exists():
        fallback = run_dir.parent / "actor-smoke-qwen35-4b-20260915" / "ollama-models"
        if fallback.exists():
            models_dir.symlink_to(fallback)
        else:
            raise FileNotFoundError(f"Models directory not found: {models_dir}")

    # Build Ollama environment
    env = os.environ.copy()
    server_env = port_lock.get("server_environment", {})
    for k, v in server_env.items():
        env[k] = str(v)
    env["OLLAMA_HOST"] = f"127.0.0.1:{ollama_port}"
    env["OLLAMA_MODELS"] = str(models_dir.resolve())

    engine_binary = port_lock.get("engine_binary", "/usr/local/bin/ollama")

    ollama_log_path = run_dir / "ollama-server.log"
    port_log_path = run_dir / "port-server.log"

    ollama_log = open(ollama_log_path, "w")
    print(f"[lifecycle] Starting dedicated Ollama server on 127.0.0.1:{ollama_port}...", flush=True)
    ollama_proc = subprocess.Popen(
        [engine_binary, "serve"],
        env=env,
        stdout=ollama_log,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid if hasattr(os, "setsid") else None,
    )

    port_proc = None
    port_log = None

    try:
        # 1. Wait for Ollama server to be up
        ready = False
        t_start = time.time()
        while time.time() - t_start < startup_timeout:
            if ollama_proc.poll() is not None:
                raise RuntimeError(f"Ollama server exited prematurely with code {ollama_proc.returncode}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{ollama_port}/api/tags", timeout=1) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.5)

        if not ready:
            raise RuntimeError(f"Ollama server on port {ollama_port} did not become ready within {startup_timeout}s")
        print("[lifecycle] Dedicated Ollama server is UP.", flush=True)

        # 2. Start audited port adapter
        adapter_script = run_dir / "ollama_port.py"
        port_log = open(port_log_path, "w")
        print(f"[lifecycle] Starting audited adapter proxy on 127.0.0.1:{port}...", flush=True)
        port_proc = subprocess.Popen(
            [UV_BIN, "run", "--no-sync", "python", str(adapter_script), str(run_dir), "--port", str(port)],
            cwd=str(run_dir.parent.parent),
            stdout=port_log,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )

        # 3. Wait for adapter proxy to report READY
        ready = False
        t_start = time.time()
        while time.time() - t_start < startup_timeout:
            if port_proc.poll() is not None:
                raise RuntimeError(f"Adapter proxy exited prematurely with code {port_proc.returncode}")
            if port_log_path.exists():
                try:
                    with open(port_log_path) as f:
                        if '"status": "READY"' in f.read():
                            ready = True
                            break
                except Exception:
                    pass
            time.sleep(0.5)

        if not ready:
            raise RuntimeError(f"Adapter proxy on port {port} did not become ready within {startup_timeout}s")
        print("[lifecycle] Audited adapter proxy is READY.", flush=True)

        # 4. Optional empty-prompt load preflight if not done
        preflight_path = run_dir / "load-preflight.json"
        if not preflight_path.exists():
            print("[lifecycle] Executing empty-prompt load-only preflight...", flush=True)
            t0 = time.time()
            req_data = json.dumps({
                "model": port_lock.get("ollama_model", "qwen3.5:4b"),
                "prompt": "",
                "stream": False,
                "keep_alive": "60m",
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{ollama_port}/api/generate",
                data=req_data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                res = json.loads(resp.read().decode())
                elapsed = time.time() - t0
                preflight_record = {
                    "kind": "load_only_preflight",
                    "elapsed_seconds": elapsed,
                    "done_reason": res.get("done_reason", "load"),
                    "eval_count": res.get("eval_count", 0),
                    "prompt_eval_count": res.get("prompt_eval_count", 0),
                    "response_len": len(res.get("response", "")),
                }
                preflight_path.write_text(json.dumps(preflight_record, indent=2) + "\n")
            print(f"[lifecycle] Preflight completed in {elapsed:.2f}s.", flush=True)

        yield {
            "ollama_port": ollama_port,
            "port": port,
            "fingerprint": port_lock["fingerprint"],
        }

    finally:
        print("\n[lifecycle] Shutting down adapter proxy and dedicated Ollama server...", flush=True)

        # Unload model from VRAM
        try:
            unload_req = urllib.request.Request(
                f"http://127.0.0.1:{ollama_port}/api/generate",
                data=json.dumps({"model": port_lock.get("ollama_model", "qwen3.5:4b"), "keep_alive": 0}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(unload_req, timeout=3):
                pass
        except Exception:
            pass

        # Terminate port adapter process group
        if port_proc and port_proc.poll() is None:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(port_proc.pid), signal.SIGTERM)
                else:
                    port_proc.terminate()
                port_proc.wait(timeout=5)
            except Exception:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(port_proc.pid), signal.SIGKILL)
                else:
                    port_proc.kill()

        # Terminate Ollama server process group
        if ollama_proc and ollama_proc.poll() is None:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(ollama_proc.pid), signal.SIGTERM)
                else:
                    ollama_proc.terminate()
                ollama_proc.wait(timeout=5)
            except Exception:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(ollama_proc.pid), signal.SIGKILL)
                else:
                    ollama_proc.kill()

        if port_log and not port_log.closed:
            port_log.close()
        if ollama_log and not ollama_log.closed:
            ollama_log.close()
        print("[lifecycle] Process cleanup complete: Adapter and Ollama server ended; GPU VRAM released.", flush=True)
