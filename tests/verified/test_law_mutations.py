"""Test that all 11 semantic mutations to Kernel.bend are caught by machine-checked proofs."""

from pathlib import Path
import pytest
from scripts.verified_kernel.mutate import MUTATIONS, check_mutation_breaks_proof

KERNEL_DIR = Path(__file__).resolve().parent.parent.parent / "verified" / "lifecycle"


@pytest.mark.parametrize("mutation_name", list(MUTATIONS.keys()))
def test_law_mutation_breaks_proof(mutation_name: str):
    """Verify that applying mutation_name causes bend PROOF.bend to fail."""
    caught, output = check_mutation_breaks_proof(mutation_name, KERNEL_DIR)
    assert caught, f"Mutation '{mutation_name}' escaped machine checking!\nOutput:\n{output}"
