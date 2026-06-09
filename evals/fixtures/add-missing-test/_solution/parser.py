def parse_kv(line):
    """Parse a 'key=value' line and return (key, value).

    Raises ValueError if '=' is not present in line.
    """
    if "=" not in line:
        raise ValueError(f"No '=' found in line: {line!r}")
    key, _, value = line.partition("=")
    return key, value
