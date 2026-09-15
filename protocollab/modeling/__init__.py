"""Canonical Mealy replay, public state inference and hypothetical branches."""

from __future__ import annotations

from protocollab.contracts import ModelArtifact, PredictionRecord, digest, uid


class MealyModel:
    def __init__(self, artifact: ModelArtifact):
        # Revalidate even a previously constructed object at the admission/use boundary.
        self.artifact = ModelArtifact.model_validate(artifact.model_dump())
        self.edges = {(t.from_state, t.input): (t.to_state, t.output) for t in artifact.transitions}
        self.hash = digest(artifact)

    def step(self, state, symbol):
        return self.edges[state, symbol]

    def replay(self, word, initial=None):
        state, outputs = initial or self.artifact.initial_state, []
        for symbol in word:
            state, output = self.step(state, symbol)
            outputs.append(output)
        return state, outputs

    def filter(self, states, symbol, observed):
        return sorted({self.step(q, symbol)[0] for q in states if self.step(q, symbol)[1] == observed})

    def predict(self, states, symbol):
        return sorted({self.step(q, symbol)[1] for q in states})


def prediction(store, belief, model, symbol):
    expected = model.predict(belief.possible_model_states, symbol) if model else []
    record = PredictionRecord(
        prediction_id=uid("pred"), namespace=store.namespace, belief_rev=belief.revision,
        model_rev=belief.model_rev, commit_seq=store.tail[0] + 1, input_symbol=symbol,
        status="UNPREDICTED" if not expected else "PREDICTED" if len(expected) == 1 else "PREDICTED_SET",
        expected_outputs=expected, assumption_refs=["finite-deterministic", "stable-namespace"],
    )
    store.append("model", "prediction.committed", record.model_dump())
    return record


def simulate(belief, model, symbols):
    states = list(belief.possible_model_states)
    steps = []
    for symbol in symbols:
        branches = {}
        for state in states:
            target, output = model.step(state, symbol)
            branches.setdefault(output, set()).add(target)
        steps.append({"input": symbol, "outputs": sorted(branches)})
        states = sorted({q for group in branches.values() for q in group})
    return {"kind": "HYPOTHETICAL", "branch_id": uid("branch"),
            "base_belief_rev": belief.revision, "base_model_hash": model.hash,
            "assumptions": ["active model may be incomplete or wrong", "branch-local clock"],
            "steps": steps, "possible_model_states": states}
