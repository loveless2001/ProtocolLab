"""Observed facts survive model mismatch, rollback, and authorized goal changes."""

from protocollab.contracts import BeliefSnapshot


class BeliefService:
    def __init__(self, store, resource="R"):
        self.store, self.resource = store, resource
        if store.get("belief", resource) is None:
            store.set("belief", resource, BeliefSnapshot().model_dump())

    @property
    def snapshot(self):
        return BeliefSnapshot.model_validate(self.store.get("belief", self.resource))

    def update(self, observation, model):
        belief = self.snapshot.model_dump()
        belief["revision"] += 1
        belief["observation_refs"].append(observation.observation_id)
        if model:
            states = model.filter(belief["possible_model_states"], observation.input_symbol, observation.domain_output)
            belief["possible_model_states"] = states
            if not states and "MODEL_MISMATCH" not in belief["flags"]:
                belief["flags"].append("MODEL_MISMATCH")
        if observation.input_symbol == "INSPECT":
            _, artifact, health = observation.domain_output.split(":")
            fact = {"artifact": artifact, "health": health, "tick": observation.logical_tick,
                    "observation_ref": observation.observation_id, "source": "effect_sensor"}
            belief["facts"].append(fact)
            claims = self.store.get("belief", "claims", [])
            if any(c.get("artifact") and c["artifact"] != artifact for c in claims):
                if "EVIDENCE_CONFLICT" not in belief["flags"]:
                    belief["flags"].append("EVIDENCE_CONFLICT")
        self.store.set("belief", self.resource, belief, "belief.updated")
        return self.snapshot

    def claim(self, claim, source="actor"):
        claims = self.store.get("belief", "claims", [])
        claims.append({**claim, "source": source, "status": "UNVERIFIED"})
        self.store.set("belief", "claims", claims, "epistemic.claim", source)

    def rebase(self, model, observations, complete_history=True):
        previous = self.snapshot
        states = [model.artifact.initial_state] if complete_history else list(model.artifact.states)
        flags = [] if complete_history else ["STATE_UNRESOLVED"]
        for observation in observations:
            states = model.filter(states, observation.input_symbol, observation.domain_output)
        if not states:
            flags.append("MODEL_MISMATCH")
        if "EVIDENCE_CONFLICT" in previous.flags:
            flags.append("EVIDENCE_CONFLICT")
        snapshot = previous.model_copy(update={"revision": previous.revision + 1,
            "model_rev": model.artifact.revision, "possible_model_states": states,
            "complete_history": complete_history, "flags": flags})
        self.store.set("belief", self.resource, snapshot.model_dump(), "belief.rebased")
        return snapshot

    def completion(self, task, since_seq=0):
        eligible = []
        refs = set()
        for event in self.store.events(after=since_seq):
            if event["kind"] == "epistemic.observation":
                obs = event["payload"]
                if obs["resource_id"] == task.resource_id and obs["input_symbol"] == "INSPECT" and obs["source_principal_ref"] == "effect_sensor" and obs["domain_output"] == f"INSPECT:{task.artifact}:{task.health}":
                    if obs["observation_id"] not in refs:
                        refs.add(obs["observation_id"])
                        eligible.append(obs)
        for left in eligible:
            for right in eligible:
                if right["logical_tick"] - left["logical_tick"] >= task.minimum_tick_gap:
                    return {"status": "PUBLIC_CONTRACT_SATISFIED", "observation_refs": [left["observation_id"], right["observation_id"]],
                            "limitation": "Confirms only the two observed instants; pending work may remain."}
        return {"status": "INSUFFICIENT_EVIDENCE", "observation_refs": []}
