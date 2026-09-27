"""Image generation providers, behind one small interface.

Every provider answers the same question — "give me one print-ready PNG for this
prompt" — and the rest of stallkit knows nothing else about them. That is deliberate:
image models are replaced far more often than Etsy endpoints are, and the drop
pipeline must not learn the vocabulary of any one vendor.

TRANSPARENCY IS LOAD-BEARING, NOT COSMETIC
------------------------------------------
`drop`'s whole routing decision is `mockup.looks_like_artwork()`, which asks whether a
file has see-through pixels. A design generated on an opaque white square is therefore
not "a design with a white background" — it is a *finished product photo* as far as
the pipeline is concerned, and it will be uploaded as a listing image rather than
composited onto a shirt. So each provider states whether it can return real alpha, the
caller warns when it cannot, and `--cutout` exists for the case where it cannot.

Nothing here is on by default and nothing ships a key: a seller who never sets
STALLKIT_IMAGE_PROVIDER never makes a single request to any of these.
"""

from __future__ import annotations

import base64
import os
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..errors import ConfigError, StallKitError

# Providers a seller can choose between. `stub` draws locally and calls nothing, which
# is what the test suite uses and what `--dry-run` reaches for.
PROVIDERS = ("openai", "stability", "stub")

DEFAULT_OPENAI_MODEL = "gpt-image-1"
OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"

STABILITY_GENERATE_URL = "https://api.stability.ai/v2beta/stable-image/generate/core"
STABILITY_CUTOUT_URL = "https://api.stability.ai/v2beta/stable-image/edit/remove-background"

# Generation is slow by API standards: a minute is a normal wait, not a fault.
TIMEOUT = httpx.Timeout(180.0, connect=15.0)
MAX_ATTEMPTS = 4


class DesignError(StallKitError):
    """A provider refused, or answered with something that was not an image."""


@dataclass
class DesignRequest:
    prompt: str
    # The shape the print wants, in the neutral vocabulary each provider maps itself:
    # 'square' for shirts and mugs, 'portrait'/'landscape' for wall art.
    shape: str = "square"
    transparent: bool = True
    negative_prompt: str = ""
    seed: int | None = None


SHAPES = ("square", "portrait", "landscape")

# OpenAI takes pixel sizes; Stability takes aspect ratios. Both are expressed here so
# neither vocabulary leaks into the command or the prompt builder.
_OPENAI_SIZE = {"square": "1024x1024", "portrait": "1024x1536", "landscape": "1536x1024"}
_STABILITY_RATIO = {"square": "1:1", "portrait": "2:3", "landscape": "3:2"}


@dataclass
class DesignConfig:
    """Which generator to use and the credential for it. Read from .env like the rest."""

    provider: str = ""
    openai_key: str = ""
    openai_model: str = DEFAULT_OPENAI_MODEL
    stability_key: str = ""

    @classmethod
    def load(cls) -> DesignConfig:
        from ..config import load_env

        load_env()
        provider = (os.environ.get("STALLKIT_IMAGE_PROVIDER") or "").strip().lower()
        openai_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        stability_key = (os.environ.get("STABILITY_API_KEY") or "").strip()

        # One key set and no provider named is not ambiguous, so do not make the seller
        # say it twice. Two keys and no choice is ambiguous, and guessing would spend
        # money at whichever vendor happened to be first in a tuple.
        if not provider:
            found = [
                name
                for name, key in (("openai", openai_key), ("stability", stability_key))
                if key
            ]
            if len(found) == 1:
                provider = found[0]
            elif len(found) > 1:
                raise ConfigError(
                    "Both OPENAI_API_KEY and STABILITY_API_KEY are set, so stallkit will "
                    "not choose for you — an image costs money at either.\n"
                    "Say which one: STALLKIT_IMAGE_PROVIDER=openai (or stability)."
                )

        return cls(
            provider=provider,
            openai_key=openai_key,
            openai_model=(
                os.environ.get("STALLKIT_OPENAI_IMAGE_MODEL") or DEFAULT_OPENAI_MODEL
            ).strip(),
            stability_key=stability_key,
        )

    def build(self) -> Provider:
        if not self.provider:
            raise ConfigError(
                "No image generator is configured, so there is nothing to draw with.\n"
                "Pick one and put its key in .env:\n"
                "  OPENAI_API_KEY=...        (transparent PNGs directly)\n"
                "  STABILITY_API_KEY=...     (transparent via a second cut-out call)\n"
                "Then, if you set both, STALLKIT_IMAGE_PROVIDER=openai or stability.\n"
                "Neither is required for anything else in stallkit."
            )
        if self.provider == "stub":
            return StubProvider()
        if self.provider == "openai":
            if not self.openai_key:
                raise ConfigError("STALLKIT_IMAGE_PROVIDER=openai but OPENAI_API_KEY is not set.")
            return OpenAIImages(self.openai_key, model=self.openai_model)
        if self.provider == "stability":
            if not self.stability_key:
                raise ConfigError(
                    "STALLKIT_IMAGE_PROVIDER=stability but STABILITY_API_KEY is not set."
                )
            return StabilityImages(self.stability_key)
        raise ConfigError(
            f"Unknown STALLKIT_IMAGE_PROVIDER {self.provider!r}. "
            f"Choose one of: {', '.join(PROVIDERS)}."
        )


class Provider:
    """One print-ready PNG per call. Subclasses add the vendor, and nothing else."""

    name = "provider"
    # Whether this provider returns real alpha. See the module docstring: when it does
    # not, the pipeline will treat the output as a finished photo, which is the wrong
    # answer for artwork, so the caller has to be told rather than left to find out.
    returns_transparency = False

    def generate(self, request: DesignRequest) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self) -> Provider:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class _HttpProvider(Provider):
    def __init__(self) -> None:
        self._http = httpx.Client(timeout=TIMEOUT)

    def close(self) -> None:
        self._http.close()

    def _send(self, request_kwargs: dict[str, Any], *, what: str) -> httpx.Response:
        """One request, retried only where a retry cannot cost a second image.

        A generation request is not idempotent in the way that matters here: it is
        billed. So a response that arrived and was refused (4xx) is final, and only a
        rate limit, a server error or a connection that never opened is tried again.
        """
        last = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._http.request(**request_kwargs)
            except httpx.HTTPError as exc:
                never_arrived = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                last = f"network error: {exc}"
                if attempt == MAX_ATTEMPTS or not never_arrived:
                    raise DesignError(
                        f"{self.name} could not be reached while {what}: {exc}"
                        + (
                            ""
                            if never_arrived
                            else " — the request may have been billed. Check your usage "
                            "before running the same batch again."
                        )
                    ) from exc
                time.sleep(self._backoff(attempt))
                continue

            if response.status_code < 300:
                return response

            detail = _explain(response)
            if response.status_code == 401:
                raise DesignError(f"{self.name} rejected your API key: {detail}")
            if response.status_code == 400:
                raise DesignError(
                    f"{self.name} refused the prompt while {what}: {detail}. "
                    "Rewrite the concept — a refusal is usually a content policy, a "
                    "trademarked name, or a living person."
                )
            retryable = response.status_code == 429 or response.status_code >= 500
            last = f"{response.status_code}: {detail}"
            if not retryable or attempt == MAX_ATTEMPTS:
                raise DesignError(f"{self.name} failed while {what} — {last}")

            retry_after = response.headers.get("Retry-After")
            delay = (
                float(retry_after)
                if retry_after and retry_after.isdigit()
                else self._backoff(attempt)
            )
            time.sleep(delay)

        raise DesignError(f"{self.name} failed while {what} — {last}")

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(30.0, (2 ** (attempt - 1)) + random.uniform(0, 0.6))


def _explain(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or response.reason_phrase or "no detail").strip()[:300]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:300]
        for key in ("message", "errors", "name", "detail"):
            if payload.get(key):
                return str(payload[key])[:300]
    return str(payload)[:300]


class OpenAIImages(_HttpProvider):
    """OpenAI's image endpoint. Returns alpha directly, which is why it is first."""

    name = "OpenAI"
    returns_transparency = True

    def __init__(self, api_key: str, *, model: str = DEFAULT_OPENAI_MODEL) -> None:
        super().__init__()
        self.api_key = api_key
        self.model = model

    def generate(self, request: DesignRequest) -> bytes:
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": request.prompt,
            "n": 1,
            "size": _OPENAI_SIZE.get(request.shape, _OPENAI_SIZE["square"]),
            "output_format": "png",
        }
        if request.transparent:
            body["background"] = "transparent"
        response = self._send(
            {
                "method": "POST",
                "url": OPENAI_IMAGES_URL,
                "headers": {"Authorization": f"Bearer {self.api_key}"},
                "json": body,
            },
            what="generating the design",
        )
        try:
            encoded = response.json()["data"][0]["b64_json"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise DesignError(
                f"{self.name} answered without an image. This model may not support "
                "image generation — check STALLKIT_OPENAI_IMAGE_MODEL."
            ) from exc
        try:
            return base64.b64decode(encoded)
        except (ValueError, TypeError) as exc:
            raise DesignError(f"{self.name} returned an image stallkit could not decode.") from exc


class StabilityImages(_HttpProvider):
    """Stability AI. Generates opaque, then cuts the background out in a second call.

    Two calls means two charges, which is worth saying out loud — but a one-call opaque
    PNG would be routed as a finished product photo by the pipeline, so the alternative
    is not cheaper, it is wrong.
    """

    name = "Stability AI"
    returns_transparency = True

    def __init__(self, api_key: str) -> None:
        super().__init__()
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "image/*"}

    def generate(self, request: DesignRequest) -> bytes:
        form: dict[str, Any] = {
            "prompt": (None, request.prompt),
            "output_format": (None, "png"),
            "aspect_ratio": (None, _STABILITY_RATIO.get(request.shape, "1:1")),
        }
        if request.negative_prompt:
            form["negative_prompt"] = (None, request.negative_prompt)
        if request.seed is not None:
            form["seed"] = (None, str(request.seed))

        generated = self._send(
            {
                "method": "POST",
                "url": STABILITY_GENERATE_URL,
                "headers": self._headers(),
                "files": form,
            },
            what="generating the design",
        ).content
        if not request.transparent:
            return generated
        return self._cut_out(generated)

    def _cut_out(self, image: bytes) -> bytes:
        return self._send(
            {
                "method": "POST",
                "url": STABILITY_CUTOUT_URL,
                "headers": self._headers(),
                "files": {
                    "image": ("design.png", image, "image/png"),
                    "output_format": (None, "png"),
                },
            },
            what="removing the background",
        ).content


class StubProvider(Provider):
    """Draws locally and calls nothing. The offline test suite and --dry-run use this.

    The output is a real transparent PNG rather than a fixed asset, so it exercises the
    same `looks_like_artwork` routing a generated design would, and the repository
    carries no binaries.
    """

    name = "stub"
    returns_transparency = True

    def generate(self, request: DesignRequest) -> bytes:
        import hashlib
        import io

        from PIL import Image, ImageDraw

        width, height = {
            "square": (1024, 1024),
            "portrait": (1024, 1536),
            "landscape": (1536, 1024),
        }.get(request.shape, (1024, 1024))

        # Colour derived from the prompt, so two concepts are visibly different and the
        # same concept is reproducible — which is what makes assertions on it possible.
        digest = hashlib.sha256(request.prompt.encode("utf-8")).digest()
        fill = (digest[0], digest[1], digest[2], 255)

        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        draw.ellipse(
            (width * 0.15, height * 0.15, width * 0.85, height * 0.85), fill=fill
        )
        buffer = io.BytesIO()
        canvas.save(buffer, "PNG")
        return buffer.getvalue()
