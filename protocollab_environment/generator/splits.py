"""Private topology splits: hashes, aliases, seeds and solutions never enter actor packets."""

from __future__ import annotations

from protocollab.contracts import digest
from protocollab_environment.generator import FAMILIES, generate


def make_archive(seed=0, development=6, validation=6, sealed_per_stratum=16):
    splits, used = {}, set()
    specifications = [("development", development, False, FAMILIES[:5]),
                      ("validation", validation, False, FAMILIES[:5]),
                      ("sealed_familiar_size", sealed_per_stratum, False, FAMILIES[:5]),
                      ("sealed_larger", sealed_per_stratum, True, FAMILIES[:5]),
                      ("sealed_composition", sealed_per_stratum, False, ("held_out_composition",))]
    candidate_seed = seed
    for split, count, larger, families in specifications:
        scenarios = []
        attempts = 0
        while len(scenarios) < count:
            if attempts >= 20000:
                raise ValueError(f"TOPOLOGY_DIVERSITY_EXHAUSTED:{split}; reduce sizing or preregister a richer generator")
            family = families[attempts % len(families)]
            _, private = generate(candidate_seed, family, larger)
            candidate_seed += 1
            attempts += 1
            if private["topology_hash"] in used:
                continue
            used.add(private["topology_hash"])
            scenarios.append({"scenario_id": f"scenario-{len(used):04d}", "split": split, **private})
        splits[split] = scenarios
    return {"schema_version": "0.1", "generator_seed": seed, "splits": splits,
            "split_unit": "protocol_topology", "archive_hash": digest(splits)}


def validate_archive(archive):
    all_ids, hashes = set(), set()
    for split, scenarios in archive["splits"].items():
        for scenario in scenarios:
            if scenario["scenario_id"] in all_ids or scenario["topology_hash"] in hashes:
                raise ValueError("TOPOLOGY_LEAKAGE_BETWEEN_SPLITS")
            if scenario["split"] != split:
                raise ValueError("SPLIT_LABEL_MISMATCH")
            all_ids.add(scenario["scenario_id"])
            hashes.add(scenario["topology_hash"])
    if digest(archive["splits"]) != archive["archive_hash"]:
        raise ValueError("SCENARIO_ARCHIVE_TAMPERED")
    return True
