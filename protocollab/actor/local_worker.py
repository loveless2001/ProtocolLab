"""Optional Transformers runner; hash-checked local files, no updates or remote code."""

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
        output = model.generate(**inputs, max_new_tokens=request["max_output_tokens"], do_sample=False)
    generated = output[0, count:]
    print(json.dumps({"model_id": request["model_id"], "text": tokenizer.decode(generated, skip_special_tokens=True),
                      "input_tokens": count, "output_tokens": len(generated), "fingerprint": None}))


if __name__ == "__main__":
    main()
