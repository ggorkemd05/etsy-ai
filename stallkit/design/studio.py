"""Generating designs into the drop workspace, and recording where they came from.

Two things make this more than a loop around a provider.

**Filenames are the interface.** `drop` reads the product concept off the filename —
that is its documented weak point and the reason `IMG_2043.png` is refused. A design
generated here is named from the concept that produced it, so the title and tags the
pipeline writes are about the thing in the picture. Getting that wrong would produce a
folder of files the next command politely declines.

**Every image costs money.** So the count is stated before anything is sent, a dry run
writes the prompts without generating, and one failure does not abandon the images
already paid for.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..drop import mockup
from ..errors import ValidationError
from . import cutout, prompts
from .providers import DesignRequest, Provider

# Written beside the workspace folders. Provenance, not bookkeeping: months later the
# only record of what produced a design is this file, and a marketplace dispute asks
# exactly that question.
MANIFEST_FILE = "ai-designs.json"

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slug(concept: str) -> str:
    """A filename stem `drop` can read the concept back out of.

    ASCII and hyphens only, because this name travels through Explorer, a zip, OneDrive
    and someone's Windows shell before `seeds.derive()` sees it. Turkish and other
    accented characters are folded rather than dropped, so 'çiçek deseni' stays
    'cicek-deseni' instead of collapsing to 'deseni'.
    """
    folded = unicodedata.normalize("NFKD", concept.lower())
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    stem = _SLUG_STRIP.sub("-", ascii_only).strip("-")
    if not stem or sum(ch.isalpha() for ch in stem) < 3:
        raise ValidationError(
            f"{concept!r} does not survive as a filename, and `drop` reads the product "
            "concept from the filename. Give the concept some words."
        )
    return stem


@dataclass
class DesignResult:
    concept: str
    prompt: str
    path: Path | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.path is not None and not self.error


@dataclass
class DesignBatch:
    out_dir: Path
    provider: str
    results: list[DesignResult] = field(default_factory=list)
    manifest_path: Path | None = None

    @property
    def made(self) -> list[DesignResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[DesignResult]:
        return [r for r in self.results if r.error]


def _next_path(out_dir: Path, stem: str, taken: set[str]) -> Path:
    """`concept-1.png`, `concept-2.png`, … skipping anything already on disk.

    A trailing counter is safe here for once: `seeds` drops a bare number at the end of
    a name, so all of these read back as the same concept — which is exactly right, they
    are variants of it.
    """
    index = 1
    while True:
        name = f"{stem}-{index}.png"
        if name.casefold() not in taken and not (out_dir / name).exists():
            taken.add(name.casefold())
            return out_dir / name
        index += 1


def plan(
    concepts: list[str],
    *,
    variants: int = 1,
    style: str = "",
    with_text: bool = False,
    extra: str = "",
) -> list[tuple[str, str]]:
    """(concept, prompt) for every image a run would generate. No side effects."""
    if variants < 1:
        raise ValidationError("--variants must be at least 1.")
    planned: list[tuple[str, str]] = []
    for concept in concepts:
        prompt = prompts.build(concept, style=style, with_text=with_text, extra=extra)
        planned.extend([(concept, prompt)] * variants)
    return planned


def generate(
    provider: Provider,
    concepts: list[str],
    out_dir: Path,
    *,
    variants: int = 1,
    style: str = "",
    shape: str = "square",
    with_text: bool = False,
    extra: str = "",
    transparent: bool = True,
    cut_out: bool = False,
    manifest_dir: Path | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> DesignBatch:
    """Generate every planned design, writing each file as soon as it arrives.

    Written one at a time on purpose: a batch that dies on image seven keeps six, and
    an image already paid for must not be lost to an exception further down the loop.
    """
    if not concepts:
        raise ValidationError("No concepts to draw.")

    planned = plan(
        concepts, variants=variants, style=style, with_text=with_text, extra=extra
    )
    # Every filename is worked out before the first request. A concept that cannot
    # become a filename has to fail while it is still free: discovering it after the
    # image arrived would throw away something already billed.
    stems = {concept: slug(concept) for concept in concepts}

    out_dir.mkdir(parents=True, exist_ok=True)
    batch = DesignBatch(out_dir=out_dir, provider=provider.name)
    taken: set[str] = set()

    for concept, prompt in planned:
        result = DesignResult(concept=concept, prompt=prompt)
        batch.results.append(result)
        try:
            data = provider.generate(
                DesignRequest(
                    prompt=prompt,
                    shape=shape,
                    transparent=transparent,
                    negative_prompt=prompts.NEGATIVE_PROMPT,
                )
            )
        except Exception as exc:  # noqa: BLE001 — one refusal must not lose the batch
            result.error = str(exc)
            if on_progress:
                on_progress(f"failed {concept!r}: {exc}")
            continue

        path = _next_path(out_dir, stems[concept], taken)
        try:
            path.write_bytes(data)
        except OSError as exc:
            result.error = f"could not be written to {path}: {exc}"
            continue
        result.path = path

        if transparent:
            _check_transparency(result, cut_out=cut_out)
        if on_progress:
            on_progress(f"drew {path.name}")

    batch.manifest_path = _write_manifest(batch, manifest_dir or out_dir)
    return batch


def _check_transparency(result: DesignResult, *, cut_out: bool) -> None:
    """Verify the file will be routed as artwork, and say so plainly when it will not.

    A design without see-through pixels is not a smaller problem than a failed
    generation — `drop` will treat it as a finished product photo and upload it as a
    listing image instead of compositing it onto a mockup. The seller has to know
    before they run `drop auto` on forty of them.
    """
    path = result.path
    if path is None:
        return
    if mockup.looks_like_artwork(path):
        return

    if cut_out:
        try:
            if cutout.remove_flat_background(path):
                result.warnings.append(
                    "came back opaque; its flat background was cut out locally — open it "
                    "and check the edges before listing"
                )
                if mockup.looks_like_artwork(path):
                    return
        except ValidationError as exc:
            result.warnings.append(f"background could not be cut out: {exc}")

    result.warnings.append(
        "no transparent pixels, so `drop` will treat this as a finished product photo "
        "and upload it as it is, not composite it onto a mockup. Re-run with --cutout, "
        "or use it as a ready photo on purpose."
    )


def _write_manifest(batch: DesignBatch, directory: Path) -> Path | None:
    """Append what this run produced to ai-designs.json, keeping earlier runs.

    Written whole and replaced atomically, like the token and the upload history: a
    half-written provenance file is worse than none, because it looks authoritative.
    """
    made = batch.made
    if not made:
        return None

    path = directory / MANIFEST_FILE
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            # An unreadable manifest is not worth stopping a paid-for batch over, but it
            # must not be overwritten either — the earlier records may be recoverable.
            return None

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for result in made:
        assert result.path is not None
        existing[result.path.name] = {
            "concept": result.concept,
            "prompt": result.prompt,
            "provider": batch.provider,
            "created_at": stamp,
            "warnings": result.warnings,
        }

    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        temporary.replace(path)
    except OSError:
        return None
    return path
