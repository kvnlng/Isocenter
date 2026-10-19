"""The heading slug the documentation site uses: Python-Markdown's.

One slug function for the site and for `tests/test_doc_anchors.py`,
which checks every `#fragment` link against the ids Python-Markdown's
`toc` extension assigns. That is the #152 lesson: a stdlib slugifier
written during that review disagreed with Python-Markdown's on
underscores, so it graded links against ids the site never emitted, and
five links in docs/configuration.md had been dead for months. The
MkDocs site published these ids, and every deep link into it names one,
so the Sphinx site keeps them: `docs/conf.py` sets
`myst_heading_slug_func` to `pm_slug` and makes the slug each section's
first HTML id.
"""
from markdown.extensions.toc import slugify


def pm_slug(text):
    """Python-Markdown's id for a heading whose text is `text`."""
    return slugify(text, "-")
