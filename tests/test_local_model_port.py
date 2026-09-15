"""Optional CPU inference plumbing check; the tiny checkpoint is untrained test data."""

import pytest

from protocollab.actor import FrozenModelPort, ModelPortConfig, checkpoint_hashes


def test_frozen_local_checkpoint_executes_and_preserves_hashes(runtime, tmp_path):
    torch = pytest.importorskip("torch", reason="Install the local-model extra for this optional port test")
    transformers = pytest.importorskip("transformers")
    from tokenizers import Tokenizer, models, pre_tokenizers

    directory = tmp_path / "tiny-test-checkpoint"
    directory.mkdir()
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "[EOS]": 1, "hello": 2, "world": 3}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    wrapped = transformers.PreTrainedTokenizerFast(tokenizer_object=tokenizer, unk_token="[UNK]", eos_token="[EOS]", pad_token="[EOS]")
    wrapped.save_pretrained(directory)
    torch.manual_seed(17)
    config = transformers.GPT2Config(vocab_size=4, n_positions=128, n_ctx=128, n_embd=16, n_layer=1, n_head=2,
                                     bos_token_id=1, eos_token_id=1, pad_token_id=1)
    model = transformers.GPT2LMHeadModel(config)
    model.save_pretrained(directory, safe_serialization=True)
    before = checkpoint_hashes(directory)
    port = FrozenModelPort(ModelPortConfig(backend="local_frozen_checkpoint", model_id="untrained-engineering-fixture",
        checkpoint=str(directory), **before, max_input_tokens=64, max_output_tokens=4, deadline_seconds=60), runtime.store)
    result = port.generate({"task": "hello world"}, seed=17)
    assert isinstance(result, str)
    assert 0 < port.input_tokens <= 64
    assert 0 < port.output_tokens <= 4
    assert checkpoint_hashes(directory) == before
    assert torch.cuda.is_available() is False
