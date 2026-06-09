import pytest
from parser import parse_kv


def test_valid_kv():
    key, value = parse_kv("foo=bar")
    assert key == "foo"
    assert value == "bar"


def test_invalid_no_equals():
    with pytest.raises(ValueError):
        parse_kv("nodivider")
