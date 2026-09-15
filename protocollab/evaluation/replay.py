"""Reconcile retained evidence and replay predictions without new world actions."""

from __future__ import annotations

from pathlib import Path

from protocollab.contracts import ModelArtifact
from protocollab.modeling import MealyModel
from protocollab.storage import Store


def replay_run(run):
    root = Path(run)
    if not (root / "owner" / "owner.sqlite").is_file():
        raise FileNotFoundError("MISSING_OWNER_STORE")
    store = Store(root / "owner" / "owner.sqlite", "episode", readonly=True)
    try:
        anchor = store.verify()
        active = store.get("model", "active")
        model = MealyModel(ModelArtifact.model_validate(store.blob(active["hash"]))) if active else None
        word, outputs = [], []
        for event in store.events():
            if event["kind"] == "epistemic.observation" and event["payload"]["resource_id"] == "R":
                observation = event["payload"]
                store.blob(observation["raw_hash"])
                word.append(observation["input_symbol"])
                outputs.append(observation["domain_output"])
        expected = model.replay(word)[1] if model else []
        return {"status": "REPLAYED", "world_actions_performed": 0, "journal_anchor": anchor,
                "model_hash": model.hash if model else None, "transitions": len(word),
                "model_matches_retained_live_trace": expected == outputs if model else None,
                "limitations": "Replay consistency, not proof of untested dynamics or an independent experiment."}
    finally:
        store.close()
