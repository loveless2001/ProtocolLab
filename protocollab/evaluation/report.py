"""Human-readable reports generated from retained metrics, with unmeasured gates explicit."""

import json
from pathlib import Path


def render_episode_report(run):
    run = Path(run)
    episode = json.loads((run / "episode.json").read_text())
    metrics_path = run / "metrics.json"
    metrics = json.loads(metrics_path.read_text()) if metrics_path.is_file() else {}
    usage = episode.get("query_usage") or {}
    lines = [f"# ProtocolLab {episode['condition']} / {episode['governance_condition']} / Track {episode['track']}", "",
             f"Adaptation: **{episode['adaptation']['status']}**. Execution: **{episode['execution']['status']}**.", "",
             "| Measure | Observed value |", "|---|---|",
             *[f"| {name} | {metrics.get(name, 'not measured')} |" for name in
               ("raw_goal_success", "compliant_task_success", "prediction_accuracy", "prediction_coverage",
                "false_confirmation_count", "actor_action_attempts", "executed_actions", "ACA", "USMR", "USR", "ICR")],
             "", "| Interaction accounting | Count |", "|---|---|",
             *[f"| {name} | {value} |" for name, value in sorted(usage.items())], "",
             f"Checkpoint: `{episode['checkpoint_hash']}`.", "",
             "These are engineering observations from the stated run. Missing correction metrics are not passes. "
             "The proposed H1–H5 research claims and confirmatory thresholds remain unestablished. "
             "INSPECT confirms the two sampled instants; pending work may remain.", "",
             "Replay without additional world actions:", "", "```bash", f"uv run protocollab replay {run}", "```", ""]
    destination = run / "report.md"
    destination.write_text("\n".join(lines))
    return destination
