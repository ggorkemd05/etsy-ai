"""Turning a product concept into an image prompt, and nothing more than that.

WHY MARKET DATA IS NOT IN THE PROMPT
------------------------------------
`seo keywords` measures what ranking listings have in common, and it is tempting to
paste those phrases in here. They are the wrong kind of sentence. "gift for her",
"instant download" and "free shipping" are how a listing is *sold*; an image model
reads them as things to draw, and puts lettering on the artwork. So research decides
*which concepts are worth drawing* — see `concepts.py` — and this module only ever
describes the picture.

The constant clauses below are not decoration either. Every one of them exists because
its absence produces a file the `drop` pipeline cannot use: a model asked for "a
mountain sunset shirt" returns a photograph of a shirt, which then gets composited onto
a mockup of a shirt.
"""

from __future__ import annotations

import re

# Asked for a product, an image model draws the product. The pipeline needs the
# artwork *alone*, because the mockup supplies the product.
_ARTWORK_CLAUSE = (
    "standalone print-ready artwork only, isolated on a fully transparent background, "
    "not a product photo, no t-shirt, no mug, no poster frame, no mockup, no room, "
    "no shadow, no border, no watermark, no signature, centred with even margins, "
    "crisp clean edges suitable for printing"
)

# Text is where generated artwork fails most visibly and most expensively: the letters
# come out misspelled, and a misspelled print is a refund. Asked for separately with
# --with-text when a seller actually wants typography.
_NO_TEXT_CLAUSE = "no text, no words, no letters, no numbers, no typography"

# Stability takes a negative prompt; OpenAI does not, which is why the same facts are
# stated positively above rather than only here.
NEGATIVE_PROMPT = (
    "photograph of a product, t-shirt, mug, poster frame, mockup, room interior, "
    "hanger, model, mannequin, drop shadow, border, frame, watermark, signature, "
    "jpeg artifacts, blurry, cropped, cut off"
)

# Named styles, so `--style` offers something real rather than inviting a paragraph.
# Each is a visual instruction only: no style here names a living artist, because
# imitating one is both a policy refusal at most providers and a legal problem at Etsy.
STYLES = {
    "flat": "flat vector illustration, bold clean shapes, limited flat colour palette",
    "line": "single-weight line art, minimal monoline drawing, no shading",
    "vintage": "vintage screen-print look, muted retro palette, subtle halftone texture, "
               "slight distressed edges",
    "watercolour": "loose watercolour painting, soft bleeding edges, paper texture, "
                   "translucent washes",
    "boho": "boho illustration, earthy terracotta and sand palette, organic hand-drawn shapes",
    "botanical": "detailed botanical illustration, fine ink linework, muted natural tones",
    "kawaii": "cute kawaii illustration, rounded shapes, soft pastel palette, simple faces",
    "geometric": "geometric abstract composition, precise shapes, mid-century palette",
    "photoreal": "photorealistic rendering, natural lighting, fine detail",
}

DEFAULT_STYLE = "flat"

_WHITESPACE = re.compile(r"\s+")


def style_clause(style: str) -> str:
    """A named preset, or the seller's own words passed through unchanged."""
    key = (style or "").strip().lower()
    if not key:
        return STYLES[DEFAULT_STYLE]
    return STYLES.get(key, style.strip())


def build(
    concept: str,
    *,
    style: str = "",
    with_text: bool = False,
    extra: str = "",
) -> str:
    """The prompt for one design.

    `concept` leads, because every provider weights the opening of a prompt most
    heavily and the concept is the only part that differs between two designs.
    """
    concept = _WHITESPACE.sub(" ", (concept or "").strip())
    if not concept:
        raise ValueError("a design needs a concept to draw")

    parts = [concept, style_clause(style), _ARTWORK_CLAUSE]
    if not with_text:
        parts.append(_NO_TEXT_CLAUSE)
    if extra.strip():
        parts.append(extra.strip())
    return ", ".join(parts)
