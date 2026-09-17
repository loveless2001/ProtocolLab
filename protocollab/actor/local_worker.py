"""Optional Transformers runner; hash-checked local files, no updates or remote code."""

from __future__ import annotations

import json
import sys


def main():
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    request = json.load(sys.stdin)
    tokenizer = AutoTokenizer.from_pretrained(request["checkpoint"], local_files_only=True, trust_remote_code=False)
    prompt = request["prompt"]
    if tokenizer.chat_template:
        prompt = tokenizer.apply_chat_template([
            {"role": "system", "content": "Use the public ProtocolLab packet and return one JSON proposal matching its schema."},
            {"role": "user", "content": prompt}], tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(prompt, return_tensors="pt")
    count = inputs["input_ids"].shape[1]
    if count > request["max_input_tokens"]:
        raise ValueError("INPUT_TOKEN_CAP")
    torch.manual_seed(request["seed"])
    torch.use_deterministic_algorithms(True)
    model = AutoModelForCausalLM.from_pretrained(request["checkpoint"], local_files_only=True,
                                                trust_remote_code=False, use_safetensors=True)
    model.eval()
    model.requires_grad_(False)

    with torch.inference_mode():
        print(json.dumps({"input_evidence": {"prompt": prompt,
              "input_ids": inputs["input_ids"][0].tolist()}}), flush=True)

        op = request.get("op")
        if op == "score_candidates":
            candidates = request.get("candidates", [])
            evaluations = []
            total_cand_tokens = 0
            for idx, cand in enumerate(candidates):
                full_text = prompt + cand
                full_inputs = tokenizer(full_text, return_tensors="pt")
                full_ids = full_inputs["input_ids"]
                cand_ids = full_ids[0, count:]
                cand_len = cand_ids.shape[0]
                total_cand_tokens += cand_len
                if cand_len == 0:
                    score = 0.0
                else:
                    logits = model(**full_inputs).logits
                    score = 0.0
                    for j in range(cand_len):
                        token_id = cand_ids[j]
                        logit_pos = count - 1 + j
                        log_probs = torch.log_softmax(logits[0, logit_pos], dim=-1)
                        score += log_probs[token_id].item()
                evaluations.append({
                    "candidate": cand,
                    "index": idx,
                    "score": score,
                    "token_count": cand_len,
                })
            best = max(evaluations, key=lambda e: (e["score"], -e["index"]))
            print(json.dumps({
                "model_id": request["model_id"],
                "text": best["candidate"],
                "input_tokens": count,
                "output_tokens": total_cand_tokens,
                "evaluations": evaluations,
                "winning_candidate": best["candidate"],
                "fingerprint": None,
            }))
            return

        decision_mode = request.get("decision_mode", "free_json")
        if decision_mode == "constrained_json" and request.get("candidates"):
            candidates = request["candidates"]
            cand_token_seqs = [tokenizer.encode(c, add_special_tokens=False) for c in candidates]
            eos_id = tokenizer.eos_token_id if tokenizer.eos_token_id is not None else 0

            def prefix_allowed_tokens_fn(batch_id, input_ids):
                gen_ids = input_ids[count:].tolist()
                cur_len = len(gen_ids)
                allowed = set()
                for seq in cand_token_seqs:
                    if len(seq) >= cur_len and seq[:cur_len] == gen_ids:
                        if len(seq) > cur_len:
                            allowed.add(seq[cur_len])
                        else:
                            allowed.add(eos_id)
                return list(allowed) if allowed else [eos_id]

            output = model.generate(
                **inputs,
                max_new_tokens=request["max_output_tokens"],
                prefix_allowed_tokens_fn=prefix_allowed_tokens_fn,
                do_sample=False,
            )
        else:
            output = model.generate(**inputs, max_new_tokens=request["max_output_tokens"], do_sample=False)

        generated = output[0, count:]
        print(json.dumps({
            "model_id": request["model_id"],
            "text": tokenizer.decode(generated, skip_special_tokens=True),
            "input_tokens": count,
            "output_tokens": len(generated),
            "fingerprint": None,
        }))


if __name__ == "__main__":
    main()
