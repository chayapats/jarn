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
    raise NotImplementedError
