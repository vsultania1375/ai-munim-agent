"""Stored secrets: encrypted, readable after rotation, re-sealed with the newest key, legacy Razorpay rows still open."""
from __future__ import annotations

import pytest

from munim.core.secretbox import SecretBox
from munim.payments.razorpay import PaymentAccounts


def test_seal_open_and_rotate(monkeypatch):
    old = SecretBox(["key-one"])
    token = old.seal("p@ss")
    assert "p@ss" not in token and old.open(token) == "p@ss"
    rotated = SecretBox(["key-two", "key-one"])  # new key in front, old still listed
    assert rotated.open(token) == "p@ss"
    fresh = rotated.reseal(token)
    assert SecretBox(["key-two"]).open(fresh) == "p@ss"  # the old key can now be dropped
    assert SecretBox(["key-three"]).open(token) is None and SecretBox(["x"]).open(None) is None
    with pytest.raises(ValueError):
        SecretBox(["", " "])
    monkeypatch.setenv("MUNIM_DATA_KEYS", "key-two")
    assert SecretBox.from_env(fallback="key-one").open(token) == "p@ss"  # turning rotation on loses nothing


def test_razorpay_rows_survive_rotation(tmp_path):
    legacy = PaymentAccounts(tmp_path / "p.db", "server-secret")  # written the way the live server did before
    legacy.connect("tenant-1", "rzp_test_ABCD1234567890", "s3cretS3cretS3cret123456", by="x")
    rotated = PaymentAccounts(tmp_path / "p.db", SecretBox(["new-key", "server-secret"]))
    assert rotated.get("tenant-1").key_secret == "s3cretS3cretS3cret123456"
    assert rotated.reseal_all() == 1
    assert PaymentAccounts(tmp_path / "p.db", SecretBox(["new-key"])).get("tenant-1").key_secret == "s3cretS3cretS3cret123456"
