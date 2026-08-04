from wecom_ai_gateway.security import decrypt_secret, encrypt_secret, external_id_hash


def test_identity_is_deterministic_and_not_plaintext():
    value = "wmSyntheticUser123"
    digest = external_id_hash(value)
    assert digest == external_id_hash(value)
    assert value not in digest


def test_secret_roundtrip():
    value = "wmSyntheticUser123"
    encrypted = encrypt_secret(value)
    assert value not in encrypted
    assert decrypt_secret(encrypted) == value
