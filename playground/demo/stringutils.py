"""Tiny string utilities — the lean-loop demo target.

Three functions. Only one is correct out of the box; the other two are the
work lean-loop is going to do.
"""


def truncate(s, max_len, ellipsis="..."):
    """Truncate s to max_len chars, appending ellipsis if truncated."""
    if len(s) <= max_len:
        return s
    return s[: max_len - len(ellipsis)] + ellipsis


def slugify(s):
    """Convert s to a URL slug.

    Lowercase, replace non-alphanumeric runs with a single hyphen, strip
    leading/trailing hyphens.

    Examples:
      "Hello World"   -> "hello-world"
      "Hello, World!" -> "hello-world"
    """
    raise NotImplementedError("TODO: implement slugify")


def count_words(s):
    """Count whitespace-separated words. Empty string should return 0."""
    return len(s.split(" "))
