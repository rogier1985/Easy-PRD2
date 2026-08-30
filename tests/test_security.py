import pytest

from easy_prd2.security import normalize_api_base, redact


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("demo.rossum.app", "https://demo.rossum.app/api/v1"),
        ("https://demo.rossum.app/", "https://demo.rossum.app/api/v1"),
        ("https://demo.rossum.app/api/v1/", "https://demo.rossum.app/api/v1"),
        ("http://localhost:8000/api", "http://localhost:8000/api/v1"),
    ],
)
def test_normalize_api_base(value, expected):
    assert normalize_api_base(value) == expected


def test_normalize_rejects_bad_urls():
    with pytest.raises(ValueError):
        normalize_api_base("not a url / bad")


def test_redact_removes_known_and_labelled_tokens():
    secret = "super-secret-value"
    value = f"Authorization: Bearer abc123 token={secret} repeated {secret}"
    result = redact(value, (secret,))
    assert secret not in result
    assert "abc123" not in result
    assert result.count("[REDACTED]") >= 2

