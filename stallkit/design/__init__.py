"""Generating the artwork itself, so a seller with no design skills still has designs.

Optional and off by default, in the same way the Pinterest side is: nothing here runs
and nothing is billed unless an image-provider key is in `.env`. The rest of stallkit
does not import this package, and the test suite never reaches a network.

The flow is one step in front of `drop`, not a replacement for it:

    stallkit design new "pressed eucalyptus leaf" --variants 4
    stallkit drop auto

`design` writes transparent PNGs into `2-PRODUCTS`, named from the concept that drew
them, and `drop` then does exactly what it already did with a folder of designs. That
seam is the whole architecture: the generated file is not special, so nothing
downstream had to learn about AI.
"""

from __future__ import annotations

from .providers import DesignConfig, DesignError, DesignRequest, Provider
from .studio import DesignBatch, DesignResult, generate, plan, slug

__all__ = [
    "DesignBatch",
    "DesignConfig",
    "DesignError",
    "DesignRequest",
    "DesignResult",
    "Provider",
    "generate",
    "plan",
    "slug",
]
