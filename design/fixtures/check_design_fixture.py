#!/usr/bin/env python3
"""Check examples used by the spec, not the unimplemented ProtocolLab system.

This small simulator is deliberately visible to the test author. A real learner
must only use the public I/O port and must not import this private implementation.
The toy gateway below has no cryptography, persistence, IPC, or security isolation.
Run: python fixtures/check_design_fixture.py
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from itertools import product
from pathlib import Path

ALPHABET = ('SUBMIT_A','SUBMIT_B','SIGNAL_X','SIGNAL_Y','CANCEL','STATUS','INSPECT','TICK')

@dataclass
class FixtureWorld:
    served: str = 'BASE'
    pending: str | None = None
    armed: bool = False
    remaining: int = 0

    def step(self, symbol: str) -> str:
        if symbol not in ALPHABET:
            raise ValueError(f'Unregistered input: {symbol}')
        if symbol.startswith('SUBMIT_'):
            self.pending = symbol[-1]
            self.armed = False
            self.remaining = 0
            return 'ACCEPTED'
        if symbol == 'SIGNAL_X':
            if self.pending is None:
                return 'NOOP'
            self.armed = True
            self.remaining = 2
            return 'ARMED'
        if symbol == 'SIGNAL_Y':
            self.armed = False
            self.remaining = 0
            return 'OK'
        if symbol == 'CANCEL':
            self.pending = None
            self.armed = False
            self.remaining = 0
            return 'CANCELLED'
        if symbol == 'STATUS':
            return 'READY'
        if symbol == 'INSPECT':
            return f'INSPECT|{self.served}|HEALTHY'
        if self.armed:
            self.remaining -= 1
            if self.remaining == 0:
                assert self.pending in ('A','B')
                self.served = self.pending
                self.pending = None
                self.armed = False
        return 'OK'


def public_query(word: tuple[str, ...] | list[str]) -> list[str]:
    """A reset query returns only public outputs, not internal state."""
    world = FixtureWorld()
    return [world.step(symbol) for symbol in word]


def suffix_output(history: tuple[str, ...], suffix: tuple[str, ...]) -> list[str]:
    return public_query(history + suffix)[len(history):]


class ToyFence:
    """Only a sequential example for epoch semantics; not a secure broker."""
    def __init__(self) -> None:
        self.world = FixtureWorld()
        self.epoch = 0
        self.goal_rev = 0
        self.paused = False
        self.applied: dict[str, tuple[str, str]] = {}

    def dispatch(self, command_id: str, op: str, epoch: int, goal_rev: int) -> str:
        if command_id in self.applied:
            original_op, result = self.applied[command_id]
            if original_op != op:
                raise ValueError('Idempotency key reused with a different payload')
            return result  # reconciliation, no new application
        if self.paused or epoch != self.epoch or goal_rev != self.goal_rev:
            return 'STALE_OR_PAUSED'
        result = self.world.step(op)
        self.applied[command_id] = (op, result)
        return result

    def pause(self) -> None:
        self.epoch += 1
        self.paused = True

    def redirect(self) -> None:
        self.epoch += 1
        self.goal_rev += 1


def main() -> None:
    h1 = ('SUBMIT_A','TICK','STATUS')
    h2 = ('SUBMIT_A','TICK','SIGNAL_X','TICK','STATUS')
    assert public_query(h1)[-1] == public_query(h2)[-1] == 'READY'
    assert suffix_output(h1, ('INSPECT',)) == suffix_output(h2, ('INSPECT',))
    witness: tuple[str, ...] | None = None
    for length in (1,2):
        for suffix in product(ALPHABET, repeat=length):
            if suffix_output(h1, suffix) != suffix_output(h2, suffix):
                witness = suffix
                break
        if witness is not None:
            break
    assert witness is not None and len(witness) == 2
    first_found_witness = witness
    witness = ('TICK', 'INSPECT')  # readable witness used in the design document
    assert suffix_output(h1,witness)[-1] == 'INSPECT|BASE|HEALTHY'
    assert suffix_output(h2,witness)[-1] == 'INSPECT|A|HEALTHY'

    belief = {'served':'BASE','phase_hypothesis':'waiting'}
    branch = deepcopy(belief)
    branch['served'] = 'A'
    branch['kind'] = 'HYPOTHETICAL'
    assert belief == {'served':'BASE','phase_hypothesis':'waiting'}

    gate = ToyFence()
    planned_epoch = gate.epoch
    gate.pause()
    assert gate.dispatch('cmd-1','SUBMIT_A',planned_epoch,0) == 'STALE_OR_PAUSED'
    assert gate.world.pending is None

    gate = ToyFence()
    gate.dispatch('cmd-1','SUBMIT_A',0,0)
    gate.world.step('TICK')
    gate.dispatch('cmd-2','SIGNAL_X',0,0)
    gate.world.step('TICK')
    gate.pause()
    gate.world.step('TICK')  # external time continues during pause
    assert gate.world.step('INSPECT') == 'INSPECT|A|HEALTHY'
    assert gate.dispatch('cmd-3','SUBMIT_B',0,0) == 'STALE_OR_PAUSED'

    gate = ToyFence()
    gate.dispatch('s1','SUBMIT_A',0,0)
    gate.dispatch('x1','SIGNAL_X',0,0)
    gate.world.step('TICK')
    before = deepcopy(gate.world)
    gate.dispatch('s1','SUBMIT_A',0,0)  # same ID: no duplicate effect
    assert gate.world == before
    gate.dispatch('s2','SUBMIT_A',0,0)  # new ID: real repeated domain action
    gate.world.step('TICK')
    assert gate.world.step('INSPECT') == 'INSPECT|BASE|HEALTHY'

    gate = ToyFence()
    gate.redirect()
    assert gate.dispatch('old-plan','SUBMIT_A',0,0) == 'STALE_OR_PAUSED'

    result = {
        'status':'illustrative_fixture_checks_passed',
        'checks':[
            'public_history_aliasing_and_distinguishing_suffix',
            'copy_on_write_hypothetical_example',
            'pause_before_dispatch_rejects_stale_proposal',
            'inflight_effect_can_finish_after_pause',
            'transport_dedup_is_not_domain_retry_semantics',
            'redirect_invalidates_old_goal_plan',
        ],
        'distinguishing_suffix':list(witness),
        'first_witness_found_by_enumeration':list(first_found_witness),
        'h1_suffix_output':suffix_output(h1,witness),
        'h2_suffix_output':suffix_output(h2,witness),
        'not_tested':['AALpy_learning','LLM_actor','process_isolation','cryptography',
                      'durable_recovery','full_benchmark','research_hypotheses']
    }
    out = Path(__file__).with_name('fixture_check_result.json')
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__ == '__main__':
    main()
