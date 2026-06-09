import re


def slugify(s):
    """Return a URL-friendly slug from *s*.

    Rules:
    - Lowercase the string.
    - Replace spaces and underscores with a single hyphen.
    - Drop every character that is not alphanumeric or a hyphen.
    - Collapse consecutive hyphens into one.
    - Strip leading and trailing hyphens.
    """
    s = s.lower()
    s = re.sub(r'[ _]+', '-', s)
    s = re.sub(r'[^a-z0-9-]', '', s)
    s = re.sub(r'-+', '-', s)
    s = s.strip('-')
    return s
