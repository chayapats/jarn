from strutil import slugify


def test_basic():
    assert slugify("Hello World") == "hello-world"


def test_underscores_and_spaces():
    assert slugify("  A__B  ") == "a-b"


def test_special_chars():
    assert slugify("x!@#y") == "xy"


def test_mixed():
    assert slugify("Python 3.12 -- Release!") == "python-312-release"
