from __future__ import annotations

import pytest

from cli.capability import CapabilityToken, mint_capability_token, require_capability_token


def test_mint_returns_capability_token_instance():
    token = mint_capability_token()
    assert isinstance(token, CapabilityToken)


def test_two_minted_tokens_are_distinct():
    assert mint_capability_token() != mint_capability_token()


def test_require_accepts_genuine_token():
    require_capability_token(mint_capability_token())  # must not raise


@pytest.mark.parametrize("forged", ["a-string-token", 12345, None, {"nonce": "x"}, ("a",)])
def test_require_rejects_anything_that_is_not_a_capability_token(forged):
    with pytest.raises(TypeError):
        require_capability_token(forged)
