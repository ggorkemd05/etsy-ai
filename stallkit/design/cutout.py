"""Removing a flat background from generated artwork, when a model ignored the ask.

This is opt-in and it stays that way. Silently rewriting pixels is the wrong default:
the seller asked for a design, and an algorithm that guesses which parts are background
will sometimes guess a white dress, a cloud or a snow field. So the caller warns that a
design came back opaque and offers this, rather than quietly applying it.

The method is deliberately dumb enough to explain in one sentence: flood-fill inwards
from each corner, and whatever the fill reaches is background. That fails visibly on a
busy edge-to-edge composition — which is better than failing subtly in the middle.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

from ..errors import ValidationError

DEFAULT_TOLERANCE = 20

# Below this share of the image, whatever the fill reached is not a background — it is
# a corner pixel that happened to differ from its neighbour. Making those transparent
# would report success for an edge-to-edge composition that was never cut out.
MIN_BACKGROUND_SHARE = 0.02

# Sentinel colours for the fill. The first one absent from the image is used, because a
# fill value the artwork already contains would mark real pixels as background.
_SENTINELS = ((1, 254, 2), (254, 1, 253), (2, 253, 1), (253, 2, 254))


def remove_flat_background(path: Path, *, tolerance: int = DEFAULT_TOLERANCE) -> bool:
    """Make a uniform border transparent in place. True if anything changed.

    Returns False for an image that already has see-through pixels: re-cutting artwork
    that is already cut out can only damage it.
    """
    try:
        with Image.open(path) as opened:
            image = opened.convert("RGBA")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValidationError(f"Cannot open {path.name}: {exc}") from exc

    if image.getchannel("A").getextrema()[0] < 255:
        return False

    flat = image.convert("RGB")
    colours = {colour for _count, colour in (flat.getcolors(maxcolors=1 << 24) or [])}
    sentinel = next((s for s in _SENTINELS if s not in colours), None)
    if sentinel is None:
        raise ValidationError(
            f"{path.name} uses every colour stallkit can mark background with. "
            "Cut it out in an image editor instead."
        )

    width, height = flat.size
    for corner in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        if flat.getpixel(corner) == sentinel:
            continue  # Already reached by an earlier corner's fill.
        ImageDraw.floodfill(flat, corner, sentinel, thresh=tolerance)

    # Exact per-channel comparison, because averaging the three into one band would let
    # a pixel differing by (1, 0, 0) round to "identical" and be called background.
    difference = ImageChops.difference(flat, Image.new("RGB", flat.size, sentinel))
    red, green, blue = difference.split()
    widest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    mask = widest.point(lambda value: 255 if value else 0)

    background = mask.histogram()[0]
    if background / float(width * height) < MIN_BACKGROUND_SHARE:
        return False

    image.putalpha(mask)
    image.save(path, "PNG")
    return True
