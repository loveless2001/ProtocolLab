"""Unified decision input pipeline for evidence rendering, trimming, and admission."""

from __future__ import annotations

import json
from typing import Any

from protocollab.actor import ModelPortConfig, render_formatted_input
from protocollab.contracts import canonical
from protocollab.learning import BudgetExhausted


def render_packet_with_renderer(packet: dict[str, Any], renderer: str = "demarcated") -> str:
    if renderer == "standard":
        return canonical(packet).decode("utf-8")
    elif renderer == "demarcated":
        from protocollab.actor.diagnostic import format_diagnostic_prompt
        return format_diagnostic_prompt(packet, wording="demarcated")
    else:
        raise ValueError(f"UNKNOWN_RENDERER: {renderer}")


def compose_and_admit_input(
    packet: dict[str, Any],
    port_config: ModelPortConfig,
    renderer: str = "demarcated",
    store: Any = None,
    phase: str = "suffix",
) -> tuple[str, str, dict[str, Any]]:
    """Compose input through:

    evidence packet -> selected renderer -> chat formatting
                    -> admission/trimming -> retained final-input evidence.

    Returns:
        (unformatted_prompt, formatted_prompt, retained_evidence)
    """
    # Clone packet for safe history trimming
    packet_copy = json.loads(canonical(packet))
    overhead = port_config.formatting_overhead_bytes

    def render_and_format(pkt: dict[str, Any]) -> tuple[str, str, int]:
        unformatted = render_packet_with_renderer(pkt, renderer)
        formatted = render_formatted_input(unformatted, port_config)
        formatted_bytes = len(formatted.encode("utf-8")) + overhead
        return unformatted, formatted, formatted_bytes

    unformatted, formatted, formatted_bytes = render_and_format(packet_copy)

    # Trim permitted historical pages only; incoming claims and live feedback are NEVER trimmed
    if formatted_bytes > port_config.max_input_tokens and "history" in packet_copy:
        history = packet_copy["history"]
        while history.get("events") and formatted_bytes > port_config.max_input_tokens:
            history["events"].pop()
            history["has_more"] = True
            history["next_cursor"] = history["events"][-1]["seq"] if history["events"] else 0
            packet_copy["packet_format"] = (
                "compact model edges and bounded history page; all raw history remains retrievable"
            )
            unformatted, formatted, formatted_bytes = render_and_format(packet_copy)

    # If still oversized after all permitted history events are trimmed:
    if formatted_bytes > port_config.max_input_tokens:
        claim_refs = [
            {"seq": c["seq"], "payload_hash": c["payload_hash"]}
            for c in packet_copy.get("incoming_claims", [])
        ]
        if store is not None:
            store.append("model_port", "llm.input_admission_rejected", {
                "reason": "FORMATTED_INPUT_BYTE_BOUND",
                "packet_bytes": len(unformatted.encode("utf-8")),
                "formatted_bytes": formatted_bytes,
                "limit_bytes": port_config.max_input_tokens,
                "phase": phase,
                "claims": claim_refs,
            })
        raise BudgetExhausted("formatted_input_byte_bound")

    retained_evidence = {
        "prompt": formatted,
        "unformatted_prompt": unformatted,
        "renderer": renderer,
        "formatted_bytes": formatted_bytes,
        "max_input_tokens": port_config.max_input_tokens,
    }
    return unformatted, formatted, retained_evidence

