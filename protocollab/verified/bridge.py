"""Subprocess bridge communicating with verified kernel runner via stdio."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from protocollab.verified.codec import (
    decode_result,
    decode_state,
    decode_summary,
    encode_state,
)
from protocollab.verified.protocol import (
    AccountingSummary,
    LedgerState,
    StageLimit,
    TransitionResult,
)


def find_bun() -> Path | None:
    if "BUN_PATH" in os.environ:
        p = Path(os.environ["BUN_PATH"])
        if p.exists():
            return p
    which = shutil.which("bun")
    if which:
        return Path(which)
    home_bun = Path.home() / ".bun" / "bin" / "bun"
    if home_bun.exists():
        return home_bun
    return None


def find_bend() -> Path | None:
    if "BEND_PATH" in os.environ:
        p = Path(os.environ["BEND_PATH"])
        if p.exists():
            return p
    which = shutil.which("bend")
    if which:
        return Path(which)
    home_bend = Path.home() / ".bend" / "bin" / "bend"
    if home_bend.exists():
        return home_bend
    return None


def get_toolchain_lock() -> dict[str, Any]:
    lock_file = (
        Path(__file__).resolve().parent.parent.parent
        / "verified"
        / "lifecycle"
        / "toolchain.lock.json"
    )
    if lock_file.exists():
        try:
            return json.loads(lock_file.read_text())
        except Exception:
            pass
    return {}


def get_locked_bend_version() -> str | None:
    """Read the pinned Bend compiler version from repository toolchain lock."""
    data = get_toolchain_lock()
    return data.get("bend", {}).get("version", "2.0.7")


def find_bend_app() -> Path | None:
    if "BEND_APP" in os.environ:
        p = Path(os.environ["BEND_APP"])
        if p.exists():
            return p

    base = Path.home() / ".bend"
    locked_ver = get_locked_bend_version()

    # 1. Check locked toolchain version tree in home (enforcing repository toolchain lock)
    if locked_ver:
        locked_matches = sorted(base.glob(f"app/{locked_ver}/*/bend2/main.ts"), reverse=True)
        if locked_matches:
            return locked_matches[0]

    # 2. Check current symlink only if it resolves to locked version
    current = base / "current" / "bend2" / "main.ts"
    if current.exists():
        try:
            resolved = str(current.resolve())
            if not locked_ver or (f"app/{locked_ver}/" in resolved):
                return current
        except Exception:
            pass

    # 3. Check direct layout in bend installations only if hash matches locked main.ts
    direct = base / "bend2" / "main.ts"
    if direct.exists():
        repo_root = Path(__file__).resolve().parent.parent.parent
        lock = get_toolchain_lock()
        expected_sha = (
            lock.get("bend", {}).get("source_files", {}).get("main.ts", {}).get("sha256")
        )
        if expected_sha:
            try:
                if hashlib.sha256(direct.read_bytes()).hexdigest() == expected_sha:
                    return direct
            except Exception:
                pass
        elif not locked_ver:
            return direct

    # 4. Check repository vendored toolchain
    repo_root = Path(__file__).resolve().parent.parent.parent
    vendored = repo_root / "verified" / "lifecycle" / "toolchain" / "bend2" / "main.ts"
    if vendored.exists():
        return vendored

    # 5. If a locked version is declared, do NOT fall back to unpinned arbitrary versions
    if not locked_ver:
        matches = sorted(base.glob("app/*/*/bend2/main.ts"), reverse=True)
        if matches:
            return matches[0]

    return None


BUN_BIN = find_bun()
BEND_APP = find_bend_app()
RUNNER_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "verified_kernel" / "runner.mjs"
)


class VerifiedKernelBridge:
    """Thread-safe persistent bridge to the verified Bend kernel runner."""

    _instance: VerifiedKernelBridge | None = None
    _lock = threading.Lock()

    def __init__(self, runner_path: Path | None = None):
        self.runner_path = runner_path or RUNNER_SCRIPT
        self._proc: subprocess.Popen[str] | None = None
        self._io_lock = threading.Lock()
        atexit.register(self.close)

    @classmethod
    def get_default(cls) -> VerifiedKernelBridge:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _ensure_proc(self) -> subprocess.Popen[str]:
        if self._proc is None or self._proc.poll() is not None:
            bun_path = find_bun()
            if bun_path is None or not bun_path.exists():
                raise RuntimeError(
                    f"Bun binary not found at {bun_path or 'default paths'}. "
                    "Install Bun or set BUN_PATH."
                )
            bend_app_path = find_bend_app()
            if bend_app_path is None or not bend_app_path.exists():
                raise RuntimeError(
                    f"Bend preloader not found at {bend_app_path or 'default paths'}. "
                    "Set BEND_APP or ensure Bend 2.0.5 application files exist."
                )
            if not self.runner_path.exists():
                raise RuntimeError(f"Runner script not found at {self.runner_path}")

            env = os.environ.copy()
            env["HOME"] = str(Path.home())
            env["BEND_NO_TELEMETRY"] = "1"

            self._proc = subprocess.Popen(
                [str(bun_path), "--preload", str(bend_app_path), str(self.runner_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        return self._proc

    def _send_command(self, cmd_dict: dict[str, Any]) -> dict[str, Any]:
        with self._io_lock:
            proc = self._ensure_proc()
            assert proc.stdin is not None
            assert proc.stdout is not None

            line = json.dumps(cmd_dict) + "\n"
            proc.stdin.write(line)
            proc.stdin.flush()

            resp_line = proc.stdout.readline()
            if not resp_line:
                stderr = proc.stderr.read() if proc.stderr else ""
                raise RuntimeError(f"Runner process terminated unexpectedly: {stderr}")

            resp = json.loads(resp_line)
            if not resp.get("ok"):
                raise RuntimeError(f"Runner error: {resp.get('error')}")
            return resp

    def init(
        self,
        run_id: str,
        config_hash: str,
        agg_max_calls: int,
        agg_max_tokens: int,
        stage_limits: list[StageLimit | dict[str, Any]],
    ) -> LedgerState:
        limits_raw = [sl.model_dump() if isinstance(sl, StageLimit) else sl for sl in stage_limits]
        cmd = {
            "cmd": "init",
            "run_id": run_id,
            "config_hash": config_hash,
            "agg_max_calls": agg_max_calls,
            "agg_max_tokens": agg_max_tokens,
            "stage_limits": limits_raw,
        }
        res = self._send_command(cmd)
        return decode_state(res["state"])

    def apply(self, state: LedgerState, event: dict[str, Any]) -> TransitionResult:
        cmd = {
            "cmd": "apply",
            "state": encode_state(state),
            "event": event,
        }
        res = self._send_command(cmd)
        return decode_result({"state": res["state"], "verdict": res["verdict"]})

    def fold(self, state: LedgerState, events: list[dict[str, Any]]) -> LedgerState:
        cmd = {
            "cmd": "fold",
            "state": encode_state(state),
            "events": events,
        }
        res = self._send_command(cmd)
        return decode_state(res["state"])

    def summarize(self, state: LedgerState) -> AccountingSummary:
        cmd = {
            "cmd": "summarize",
            "state": encode_state(state),
        }
        res = self._send_command(cmd)
        return decode_summary(res["summary"])

    def close(self):
        with self._io_lock:
            if self._proc is not None:
                try:
                    if self._proc.stdin:
                        self._proc.stdin.close()
                    self._proc.terminate()
                    self._proc.wait(timeout=1.0)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                finally:
                    self._proc = None
