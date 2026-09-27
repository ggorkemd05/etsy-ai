"""Deciding *what* to draw, from the listings Etsy actually ranks.

This is the one place market data belongs in the design flow. A keyword like
"botanical wall art" describes a shelf, not a picture; the subjects that sell inside it
— eucalyptus, monstera, pressed fern — are in the titles of the listings ranking for
it, and those are drawable.

So the recurring phrases are filtered down to the ones that name a *subject* rather
than a *product or an offer*. Nothing is invented: a keyword with no usable phrases
returns none, and says so, rather than padding a batch with concepts nobody searched.
"""

from __future__ import annotations

from ..seo import MarketReport, content_words

# Words that describe the product, the format or the sales pitch, never the picture.
# A phrase containing any of them is not a subject: "instant download wall art" is a
# shelf label, and an image model asked to draw it writes the words onto the artwork.
COMMERCE_WORDS = {
    # format and product
    "art", "print", "prints", "printable", "printables", "poster", "posters", "wall",
    "decor", "decoration", "download", "downloadable", "digital", "file", "files",
    "pdf", "jpg", "png", "svg", "sublimation", "clipart", "bundle", "set", "pack",
    "shirt", "tshirt", "tee", "sweatshirt", "hoodie", "mug", "tumbler", "tote", "bag",
    "pillow", "cushion", "sticker", "stickers", "canvas", "frame", "framed", "mockup",
    "template", "design", "designs", "artwork",
    # the offer
    "gift", "gifts", "custom", "customized", "customised", "personalized",
    "personalised", "handmade", "vintage", "instant", "free", "shipping", "sale",
    "cheap", "best", "seller", "trending", "unique", "premium", "quality", "new",
    # who it is for, which is targeting rather than subject matter
    "her", "him", "mom", "mum", "dad", "kids", "women", "men", "womens", "mens",
    "boys", "girls", "teacher", "nurse", "friend", "wife", "husband",
    # sizes and places on a wall
    "inch", "inches", "cm", "size", "sizes", "large", "small", "a4", "a3", "a2",
    "living", "room", "bedroom", "nursery", "kitchen", "office", "home",
}

# A subject needs at least two words to be worth drawing on its own: "leaf" is not a
# design, "pressed eucalyptus leaf" is. Three is the longest n-gram research produces.
MIN_WORDS = 2


def _is_subject(phrase: str) -> bool:
    words = content_words(phrase)
    if len(words) < MIN_WORDS:
        return False
    return not any(word in COMMERCE_WORDS for word in words)


def _overlaps(candidate: str, chosen: list[str]) -> bool:
    """Two concepts sharing every word but one produce two near-identical designs."""
    words = set(content_words(candidate))
    for other in chosen:
        existing = set(content_words(other))
        if words <= existing or existing <= words:
            return True
    return False


def from_research(report: MarketReport, *, limit: int = 10) -> tuple[list[str], list[str]]:
    """Subjects worth drawing for this keyword, plus what to tell the seller.

    Returns (concepts, warnings). The warnings are the honest half: a thin sample or a
    keyword whose titles are all product words yields fewer concepts than asked for,
    and the caller says so instead of making some up.
    """
    warnings: list[str] = []
    if report.empty:
        return [], [
            f"Etsy returned no listings for {report.keyword!r}, so there is nothing to "
            "read subjects from. Check the spelling, or name the concepts yourself."
        ]
    if report.sampled < 20:
        warnings.append(
            f"only {report.sampled} listing(s) rank for {report.keyword!r} — too thin a "
            "sample to read subjects from with any confidence"
        )

    concepts: list[str] = []
    for phrase, _count in report.phrases:
        if len(concepts) >= limit:
            break
        if not _is_subject(phrase):
            continue
        if _overlaps(phrase, concepts):
            continue
        concepts.append(phrase)

    if not concepts:
        warnings.append(
            f"every recurring phrase in the {report.sampled} listing(s) ranking for "
            f"{report.keyword!r} names a product or an offer rather than a subject to "
            "draw. Name the concepts yourself instead."
        )
    elif len(concepts) < limit:
        warnings.append(
            f"{len(concepts)} of the {limit} concept(s) asked for could be read from the "
            "market; the rest would have been invented, so they are not here"
        )
    return concepts, warnings
