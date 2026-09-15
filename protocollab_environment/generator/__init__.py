"""Private finite protocol generation and privileged feasibility checks."""

from __future__ import annotations

import random
from collections import deque
from dataclasses import asdict, dataclass, replace

from protocollab.contracts import ALPHABET, digest


@dataclass(frozen=True)
class ProtocolConfig:
    family: str = "history_aliasing"
    signals: tuple[str, ...] = ("SIGNAL_X",)
    delay: int = 2
    retry_resets: bool = True
    cancel_armed: bool = True
    wrong_signal_resets: bool = True
    status_alias: bool = True
    impossible: bool = False

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**{**data, "signals": tuple(data["signals"])})


@dataclass(frozen=True)
class World:
    served: str = "BASE"
    pending: str = "NONE"
    progress: int = 0
    remaining: int = 0


def transition(config: ProtocolConfig, state: World, symbol: str):
    """Private dynamics. Never called or imported by learner/actor."""
    if symbol not in ALPHABET:
        raise ValueError("UNKNOWN_OPERATION")
    if symbol == "INSPECT":
        return state, f"INSPECT:{state.served}:HEALTHY"
    if symbol == "STATUS":
        return state, "READY" if config.status_alias or state.pending == "NONE" else "ACCEPTED"
    if symbol.startswith("SUBMIT_"):
        artifact = symbol[-1]
        if config.retry_resets or state.pending != artifact:
            state = replace(state, pending=artifact, progress=0, remaining=0)
        return state, "ACCEPTED"
    if symbol == "CANCEL":
        if config.cancel_armed or not state.remaining:
            state = replace(state, pending="NONE", progress=0, remaining=0)
        return state, "CANCELLED"
    if symbol == "TICK":
        if state.remaining:
            if state.remaining == 1 and not config.impossible:
                state = World(served=state.pending)
            else:
                state = replace(state, remaining=max(1, state.remaining - 1))
        return state, "TICKED"
    if state.pending != "NONE":
        if not state.remaining and symbol == config.signals[state.progress]:
            progress = state.progress + 1
            state = replace(state, progress=progress if progress < len(config.signals) else 0,
                            remaining=config.delay if progress == len(config.signals) else 0)
        elif config.wrong_signal_resets:
            state = replace(state, progress=0, remaining=0)
    return state, "OK"


def replay(config, word, initial=None):
    state, outputs = initial or World(), []
    for symbol in word:
        state, output = transition(config, state, symbol)
        outputs.append(output)
    return state, outputs


def reachable(config):
    histories = {World(): ()}
    queue = deque(histories)
    while queue:
        state = queue.popleft()
        for symbol in ALPHABET:
            target, _ = transition(config, state, symbol)
            if target not in histories:
                histories[target] = histories[state] + (symbol,)
                queue.append(target)
    return histories


def solve(config, artifact="A", max_turns=80, initial=None, allowed=None):
    # Completion requires two observations with >=2 intervening logical ticks.
    initial = initial or World()
    start = (initial, -1, 0)
    queue = deque([(start, [])])
    seen = {start}
    while queue:
        (state, age, count), plan = queue.popleft()
        if count == 2:
            return plan
        if len(plan) >= max_turns:
            continue
        for command in (*(ALPHABET[:-1] if allowed is None else allowed), "WAIT"):
            after, output = (state, None) if command == "WAIT" else transition(config, state, command)
            next_age, next_count = age, count
            if command == "INSPECT":
                if output == f"INSPECT:{artifact}:HEALTHY":
                    if count == 0:
                        next_count, next_age = 1, 0
                    elif age >= 2:
                        next_count = 2
            after, _ = transition(config, after, "TICK")
            if next_age >= 0:
                next_age = min(2, next_age + 1)
            key = (after, next_age, next_count)
            if key not in seen:
                seen.add(key)
                queue.append((key, plan + [command]))
    return None


def distinguishing_suffix(config, left, right, bound=12):
    queue = deque([(left, right, ())])
    seen = {(left, right)}
    while queue:
        a, b, word = queue.popleft()
        if len(word) >= bound:
            continue
        for symbol in ALPHABET:
            a1, oa = transition(config, a, symbol)
            b1, ob = transition(config, b, symbol)
            if oa != ob:
                return word + (symbol,)
            if (a1, b1) not in seen:
                seen.add((a1, b1))
                queue.append((a1, b1, word + (symbol,)))
    return None


def alias_pairs(config, limit=8):
    histories = reachable(config)
    pairs = []
    states = list(histories)
    for index, left in enumerate(states):
        for right in states[index + 1:]:
            if any(transition(config, left, s)[1] != transition(config, right, s)[1]
                   for s in ("STATUS", "INSPECT")):
                continue
            suffix = distinguishing_suffix(config, left, right)
            if suffix:
                pairs.append({"left": list(histories[left]), "right": list(histories[right]),
                              "suffix": list(suffix)})
                if len(pairs) == limit:
                    return pairs
    return pairs


FAMILIES = ("delayed_completion", "history_aliasing", "order_dependence", "retry_sensitive",
            "context_dependent_cancel", "held_out_composition")


def generate(seed: int, family: str, larger=False):
    rng = random.Random(seed)
    if family not in FAMILIES:
        raise ValueError("UNKNOWN_FAMILY")
    signal = rng.choice(("SIGNAL_X", "SIGNAL_Y"))
    other = "SIGNAL_Y" if signal == "SIGNAL_X" else "SIGNAL_X"
    length = rng.randint(3, 4) if family == "held_out_composition" else rng.randint(2, 3) if larger else (2 if family == "order_dependence" else rng.randint(1, 2))
    signals = tuple(signal if i % 2 == 0 else other for i in range(length))
    config = ProtocolConfig(family=family, signals=signals, delay=rng.randint(4, 6) if larger else rng.randint(1, 3),
                            retry_resets=True if family in ("retry_sensitive", "held_out_composition") else bool(rng.randrange(2)),
                            cancel_armed=False if family in ("context_dependent_cancel", "held_out_composition") else bool(rng.randrange(2)),
                            wrong_signal_resets=bool(rng.randrange(2)))
    solutions = {a: solve(config, a) for a in ("A", "B")}
    if any(p is None for p in solutions.values()):
        raise ValueError("CLEAN_TASK_UNSOLVABLE")
    graph = reachable(config)
    topology = [[asdict(q), s, asdict(transition(config, q, s)[0]), transition(config, q, s)[1]]
                for q in graph for s in ALPHABET]
    private = {"seed": seed, "config": config.to_dict(), "topology_hash": digest(topology),
               "state_count": len(graph), "solutions": solutions, "alias_pairs": alias_pairs(config)}
    return config, private
