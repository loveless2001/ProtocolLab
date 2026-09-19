"""Mutation engine for the verified lifecycle kernel.

Systematically introduces semantic defects into the verified kernel
and verifies that machine-checked proofs fail for every mutation.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

MUTATIONS: dict[str, tuple[str, str, str]] = {
    "MUTATION_PERMISSIVE_RESERVATION": (
        "Removes call limit check during reservation",
        """calls_ok = Bool.and(
                Nat.is_le(1n+stage_calls, max_calls),
                Nat.is_le(1n+agg_calls, agg_max_calls)
              )""",
        "calls_ok = True{}",
    ),
    "MUTATION_CALL_DRIFT": (
        "Counts calls independently of request list (constant zero)",
        "stage_calls = D.count_stage_calls(requests, s_name)",
        "stage_calls = 0n",
    ),
    "MUTATION_TOKEN_DRIFT": (
        "Allows unreserved token allocation (bypasses token limit check)",
        """tokens_ok = Bool.and(
            Nat.is_le(Nat.add(stage_comm, req_tokens), max_tokens),
            Nat.is_le(Nat.add(agg_comm, req_tokens), agg_max_tokens)
          )""",
        "tokens_ok = True{}",
    ),
    "MUTATION_EARLY_RELEASE": (
        "Zeros reservation on timeout instead of preserving pending charge",
        """updated = {T.ReqRecord{
                    id, stage, basis_ref, config_hash, max_input, max_output,
                    T.OutcomeUnknown{},
                    charge
                  } : T.RequestRecord}""",
        """updated = {T.ReqRecord{
                    id, stage, basis_ref, config_hash, max_input, max_output,
                    T.OutcomeUnknown{},
                    T.ChargeReleased{""}
                  } : T.RequestRecord}""",
    ),
    "MUTATION_SILENT_OVERWRITE": (
        "Overwrites conflicting request identity without latching fault",
        """def check_existing_match(
  same: Bool,
  state: T.LedgerState
) -> T.TransitionResult:
  match same:
    case True{}:
      T.TransResult{state, T.DuplicateNoop{}}
    case False{}:
      match state:
        case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
          T.TransResult{
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, Some{"CONFLICTING_REQUEST_IDENTITY"}},
            T.ConflictFault{"CONFLICTING_REQUEST_IDENTITY"}
          }""",
        """def check_existing_match(
  same: Bool,
  state: T.LedgerState
) -> T.TransitionResult:
  T.TransResult{state, T.DuplicateNoop{}}""",
    ),
    "MUTATION_DOUBLE_DISPATCH": (
        "Creates duplicate accepted dispatch intent for an already dispatched request",
        """case T.DispatchedIntent{}:
              T.TransResult{state, T.DuplicateNoop{}}""",
        """case T.DispatchedIntent{}:
              T.TransResult{state, T.Accepted{None{}}}""",
    ),
    "MUTATION_UNCHECKED_OVERRUN": (
        "Settles without checking bound or latching fault",
        """is_overflow = Bool.or(
            Nat.is_gt(input_tokens, max_input),
            Nat.is_gt(output_tokens, max_output)
          )""",
        "is_overflow = False{}",
    ),
    "MUTATION_TERMINAL_RELEASE": (
        "Releases settled request as failed instead of rejecting",
        """case T.ChargeSettled{inp, out, r_hash}:
              T.TransResult{state, T.Rejected{"CANNOT_RELEASE_SETTLED_REQUEST"}}""",
        """case T.ChargeSettled{inp, out, r_hash}:
              match state:
                case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
                  updated = {T.ReqRecord{
                    id, stage, basis_ref, config_hash, max_input, max_output,
                    T.ProvenNotSent{},
                    T.ChargeReleased{evidence_hash}
                  } : T.RequestRecord}
                  T.TransResult{
                    T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, D.update_request(requests, updated), fault},
                    T.Accepted{None{}}
                  }""",
    ),
    "MUTATION_STATE_UNLATCHED": (
        "Allows state transitions to proceed after state fault is latched",
        """def apply_reserve(
  state: T.LedgerState,
  +req_id: String,
  stage: String,
  basis_ref: String,
  config_hash: String,
  max_input: Nat,
  max_output: Nat
) -> T.TransitionResult:
  match state:
    case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, +requests, fault}:
      match fault:
        case Some{reason}:
          T.TransResult{
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, Some{reason}},
            T.Rejected{"STATE_FAULT_LATCHED"}
          }
        case None{}:
          apply_reserve.check_existing(""",
        """def apply_reserve(
  state: T.LedgerState,
  +req_id: String,
  stage: String,
  basis_ref: String,
  config_hash: String,
  max_input: Nat,
  max_output: Nat
) -> T.TransitionResult:
  match state:
    case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, +requests, fault}:
      match fault:
        case Some{reason}:
          apply_reserve.check_existing(
            D.find_request(requests, req_id),
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, Some{reason}},
            req_id, stage, basis_ref, config_hash, max_input, max_output
          )
        case None{}:
          apply_reserve.check_existing(""",
    ),
    "MUTATION_CROSS_STAGE_LEAK": (
        "Records stage 1 request under stage 2, causing cross-stage leakage",
        """new_rec = {T.ReqRecord{
            req_id, stage, basis_ref, config_hash, max_input, max_output,""",
        """new_rec = {T.ReqRecord{
            req_id, "stage2", basis_ref, config_hash, max_input, max_output,""",
    ),
    "MUTATION_EVIDENCE_BYPASS": (
        "Allows repeated failure release transitions without idempotency guard",
        """case T.ChargeReleased{ex_hash}:
              T.TransResult{state, T.DuplicateNoop{}}""",
        """case T.ChargeReleased{ex_hash}:
              T.TransResult{state, T.Accepted{None{}}}""",
    ),
    "MUTATION_VALIDATION_ZEROES_USAGE": (
        "Zeroes confirmed usage during validation instead of preserving accounting",
        """def apply_validation.step(
  rec: Maybe<&2, T.RequestRecord>,
  state: T.LedgerState,
  outcome: String
) -> T.TransitionResult:
  match rec:
    case None{}:
      T.TransResult{state, T.Rejected{"REQUEST_NOT_FOUND"}}
    case Some{r}:
      T.TransResult{state, T.Accepted{None{}}}""",
        """def apply_validation.step(
  rec: Maybe<&2, T.RequestRecord>,
  state: T.LedgerState,
  outcome: String
) -> T.TransitionResult:
  match rec:
    case None{}:
      T.TransResult{state, T.Rejected{"REQUEST_NOT_FOUND"}}
    case Some{r}:
      match r:
        case T.ReqRecord{id, stage, basis_ref, config_hash, max_input, max_output, transport, charge}:
          match state:
            case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
              updated = {T.ReqRecord{
                id, stage, basis_ref, config_hash, max_input, max_output,
                transport,
                T.ChargeSettled{0n, 0n, "zeroed"}
              } : T.RequestRecord}
              T.TransResult{
                T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, D.update_request(requests, updated), fault},
                T.Accepted{None{}}
              }""",
    ),
    "MUTATION_VALIDATION_RELEASES_HELD": (
        "Releases held reservation during validation instead of preserving accounting",
        """def apply_validation.step(
  rec: Maybe<&2, T.RequestRecord>,
  state: T.LedgerState,
  outcome: String
) -> T.TransitionResult:
  match rec:
    case None{}:
      T.TransResult{state, T.Rejected{"REQUEST_NOT_FOUND"}}
    case Some{r}:
      T.TransResult{state, T.Accepted{None{}}}""",
        """def apply_validation.step(
  rec: Maybe<&2, T.RequestRecord>,
  state: T.LedgerState,
  outcome: String
) -> T.TransitionResult:
  match rec:
    case None{}:
      T.TransResult{state, T.Rejected{"REQUEST_NOT_FOUND"}}
    case Some{r}:
      match r:
        case T.ReqRecord{id, stage, basis_ref, config_hash, max_input, max_output, transport, charge}:
          match state:
            case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
              updated = {T.ReqRecord{
                id, stage, basis_ref, config_hash, max_input, max_output,
                transport,
                T.ChargeReleased{"val_released"}
              } : T.RequestRecord}
              T.TransResult{
                T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, D.update_request(requests, updated), fault},
                T.Accepted{None{}}
              }""",
    ),
    "MUTATION_VALIDATION_EMITS_PERMIT": (
        "Emits an execution permit during validation transition",
        """def apply_validation.step(
  rec: Maybe<&2, T.RequestRecord>,
  state: T.LedgerState,
  outcome: String
) -> T.TransitionResult:
  match rec:
    case None{}:
      T.TransResult{state, T.Rejected{"REQUEST_NOT_FOUND"}}
    case Some{r}:
      T.TransResult{state, T.Accepted{None{}}}""",
        """def apply_validation.step(
  rec: Maybe<&2, T.RequestRecord>,
  state: T.LedgerState,
  outcome: String
) -> T.TransitionResult:
  match rec:
    case None{}:
      T.TransResult{state, T.Rejected{"REQUEST_NOT_FOUND"}}
    case Some{r}:
      match r:
        case T.ReqRecord{id, stage, basis_ref, config_hash, max_input, max_output, transport, charge}:
          T.TransResult{state, T.Accepted{Some{T.InferenceDispatchIntent{id, stage, basis_ref, config_hash, max_input, max_output}}}}""",
    ),
}



def apply_mutation(content: str, mutation_name: str) -> str:
    """Apply a named mutation to Kernel.bend source code."""
    if mutation_name not in MUTATIONS:
        raise ValueError(f"Unknown mutation: {mutation_name}")
    desc, target, replacement = MUTATIONS[mutation_name]
    if target not in content:
        raise RuntimeError(f"Target pattern for {mutation_name} not found in source code!")
    return content.replace(target, replacement, 1)


def check_mutation_breaks_proof(mutation_name: str, kernel_dir: Path) -> tuple[bool, str]:
    """Test that applying mutation_name breaks bend PROOF.bend."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        # Copy all bend files
        for f in kernel_dir.glob("*.bend"):
            shutil.copy(f, tmp_path / f.name)

        # Mutate Kernel.bend
        orig_kernel = (tmp_path / "Kernel.bend").read_text()
        mutated_kernel = apply_mutation(orig_kernel, mutation_name)
        (tmp_path / "Kernel.bend").write_text(mutated_kernel)

        # Run bend PROOF.bend
        import os

        from protocollab.verified.bridge import find_bend

        bend_bin = find_bend()
        if bend_bin is None or not bend_bin.exists():
            return False, f"Bend binary not found at {bend_bin or 'default paths'}"

        env = dict(os.environ)
        env["HOME"] = str(Path.home())
        env["BEND_NO_TELEMETRY"] = "1"
        res = subprocess.run(
            [str(bend_bin), str(tmp_path / "PROOF.bend")],
            capture_output=True,
            text=True,
            env=env,
        )
        output = res.stdout + res.stderr
        # Check for toolchain crashes (segfault, abort)
        if res.returncode in (139, 134, -11, -6):
            return False, f"Toolchain crashed with returncode {res.returncode}:\n{output}"
        # Authentic catch requires clean exit code 1 with expected/observed type-checker mismatch
        # and rejects toolchain crashes, syntax errors, or unannotated import errors
        out_lower = output.lower()
        passed = (
            (res.returncode == 1)
            and ("All terms check." not in res.stdout)
            and ("expected" in out_lower and "observed" in out_lower)
        )
        return passed, output


if __name__ == "__main__":
    kernel_dir = Path("/home/lenovo/projects/ProtocolLab/verified/lifecycle")
    print(f"Testing {len(MUTATIONS)} semantic mutations against verified proofs...")
    all_caught = True
    for name in MUTATIONS:
        caught, out = check_mutation_breaks_proof(name, kernel_dir)
        if caught:
            print(f"  [CAUGHT] {name}")
        else:
            print(f"  [ESCAPED] {name} - Proof did not catch mutation!")
            all_caught = False
    if all_caught:
        print(f"\nAll {len(MUTATIONS)} mutations caught by proof verification.")
    else:
        print("\nSome mutations escaped!")
        exit(1)
