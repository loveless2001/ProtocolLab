"""Subprocess bridge communicating with verified kernel runner via stdio."""

from __future__ import annotations
import atexit
import json
import os
from pathlib import Path
import subprocess
import threading
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

BUN_BIN = Path(os.environ.get("BUN_PATH", "/home/lenovo/.bun/bin/bun"))
BEND_APP = Path(os.environ.get("BEND_APP", "/home/lenovo/.bend/app/2.0.5/AdMsHi/bend2/main.ts"))
RUNNER_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "verified_kernel" / "runner.mjs"


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
            if not BUN_BIN.exists():
                raise RuntimeError(f"Bun binary not found at {BUN_BIN}")
            if not self.runner_path.exists():
                raise RuntimeError(f"Runner script not found at {self.runner_path}")

            env = os.environ.copy()
            env["HOME"] = str(Path.home())
            env["BEND_NO_TELEMETRY"] = "1"

            self._proc = subprocess.Popen(
                [str(BUN_BIN), "--preload", str(BEND_APP), str(self.runner_path)],
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
        limits_raw = [
            sl.model_dump() if isinstance(sl, StageLimit) else sl
            for sl in stage_limits
        ]
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
