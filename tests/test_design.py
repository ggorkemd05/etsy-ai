"""Generating the artwork: every test runs offline, against the stub or a mock transport.

What is pinned here is what would cost a seller money or a wrong listing: a filename
`drop` cannot read the concept out of, a design that comes back opaque and would be
routed as a finished product photo, a refusal that loses images already paid for, and
research phrases leaking into an image prompt.
"""

import io
import json

import httpx
import pytest
from PIL import Image

from stallkit.design import concepts as concepts_mod
from stallkit.design import cutout, prompts, providers, studio
from stallkit.design.providers import DesignConfig, DesignError, DesignRequest, StubProvider
from stallkit.drop import mockup, seeds
from stallkit.errors import ConfigError, ValidationError
from stallkit.seo import MarketReport


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("STALLKIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STALLKIT_IGNORE_CWD_ENV", "1")
    for var in (
        "STALLKIT_IMAGE_PROVIDER",
        "OPENAI_API_KEY",
        "STABILITY_API_KEY",
        "STALLKIT_OPENAI_IMAGE_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


def _report(keyword="botanical wall art", sampled=200, phrases=()):
    return MarketReport(
        keyword=keyword,
        sampled=sampled,
        tags=[],
        phrases=list(phrases),
        price_min=None,
        price_median=None,
        price_max=None,
        currency="USD",
        median_favorers=None,
        top_listings=[],
    )


# --- filenames are the interface between this and `drop` ------------------------


def test_the_drop_pipeline_reads_the_concept_out_of_a_generated_filename(tmp_path):
    # `drop` derives the product concept from the filename. If that round trip breaks,
    # every generated design gets a title about the wrong thing.
    assert studio.slug("retro surf sunset") == "retro-surf-sunset"
    path = tmp_path / f"{studio.slug('retro surf sunset')}-1.png"
    # The trailing variant counter is dropped, so every variant is the same concept.
    assert seeds.derive(path).text == "retro surf sunset"


def test_turkish_characters_are_folded_not_dropped():
    assert studio.slug("çiçek deseni") == "cicek-deseni"
    assert studio.slug("Şirin Kuş Çizimi") == "sirin-kus-cizimi"


def test_a_concept_that_cannot_be_a_filename_is_refused():
    with pytest.raises(ValidationError):
        studio.slug("!!! ???")


# --- prompts --------------------------------------------------------------------


def test_the_concept_leads_the_prompt():
    prompt = prompts.build("pressed fern")
    assert prompt.startswith("pressed fern,")


def test_a_prompt_forbids_the_product_and_the_mockup():
    # Asked for "a mountain sunset shirt", a model draws a shirt — which then gets
    # composited onto a mockup of a shirt.
    prompt = prompts.build("mountain sunset")
    for forbidden in ("no t-shirt", "no mockup", "transparent background"):
        assert forbidden in prompt


def test_text_is_forbidden_unless_asked_for():
    assert "no text" in prompts.build("surf club")
    assert "no text" not in prompts.build("surf club", with_text=True)


def test_a_named_style_expands_and_an_unknown_one_is_passed_through():
    assert prompts.STYLES["line"] in prompts.build("fern", style="line")
    assert "wet plate collodion" in prompts.build("fern", style="wet plate collodion")


def test_an_empty_concept_is_refused():
    with pytest.raises(ValueError):
        prompts.build("   ")


# --- concepts come from research; research does not go into the prompt -----------


def test_product_and_offer_phrases_are_not_treated_as_subjects():
    report = _report(
        phrases=[
            ("instant download", 180),
            ("wall art print", 150),
            ("gift for her", 120),
            ("pressed eucalyptus leaf", 90),
            ("monstera leaf", 70),
        ]
    )
    found, _warnings = concepts_mod.from_research(report, limit=10)
    assert found == ["pressed eucalyptus leaf", "monstera leaf"]


def test_a_single_word_is_not_a_design():
    report = _report(phrases=[("eucalyptus", 200), ("pressed eucalyptus leaf", 90)])
    found, _ = concepts_mod.from_research(report, limit=10)
    assert found == ["pressed eucalyptus leaf"]


def test_two_concepts_that_are_subsets_of_each_other_are_not_both_drawn():
    report = _report(phrases=[("pressed eucalyptus leaf", 90), ("eucalyptus leaf", 85)])
    found, _ = concepts_mod.from_research(report, limit=10)
    assert found == ["pressed eucalyptus leaf"]


def test_a_thin_sample_is_stated_rather_than_hidden():
    report = _report(sampled=6, phrases=[("pressed eucalyptus leaf", 3)])
    found, warnings = concepts_mod.from_research(report, limit=10)
    assert found
    assert any("too thin a sample" in w for w in warnings)


def test_no_listings_yields_no_concepts_and_says_why():
    found, warnings = concepts_mod.from_research(_report(sampled=0), limit=5)
    assert found == []
    assert any("no listings" in w for w in warnings)


def test_a_keyword_whose_titles_are_all_product_words_invents_nothing():
    report = _report(phrases=[("wall art print", 150), ("instant download", 120)])
    found, warnings = concepts_mod.from_research(report, limit=5)
    assert found == []
    assert any("names a product or an offer" in w for w in warnings)


def test_fewer_concepts_than_asked_for_is_reported():
    report = _report(phrases=[("pressed eucalyptus leaf", 90)])
    _found, warnings = concepts_mod.from_research(report, limit=8)
    assert any("would have been invented" in w for w in warnings)


def test_research_phrases_never_reach_the_image_prompt():
    # 'gift for her' describes the sale, not the picture. A model asked to draw it
    # writes the words onto the artwork.
    report = _report(phrases=[("gift for her", 200), ("pressed eucalyptus leaf", 90)])
    found, _ = concepts_mod.from_research(report, limit=5)
    for concept in found:
        assert "gift" not in prompts.build(concept)


# --- generating -----------------------------------------------------------------


def test_a_batch_writes_one_transparent_png_per_variant(tmp_path):
    batch = studio.generate(
        StubProvider(), ["pressed eucalyptus leaf"], tmp_path / "out", variants=3
    )
    assert len(batch.made) == 3
    names = sorted(p.path.name for p in batch.made)
    assert names == [
        "pressed-eucalyptus-leaf-1.png",
        "pressed-eucalyptus-leaf-2.png",
        "pressed-eucalyptus-leaf-3.png",
    ]
    # Transparency is what routes these as artwork rather than as finished photos.
    assert all(mockup.looks_like_artwork(p.path) for p in batch.made)
    assert all(not p.warnings for p in batch.made)


def test_a_second_run_does_not_overwrite_the_first(tmp_path):
    out = tmp_path / "out"
    studio.generate(StubProvider(), ["fern frond"], out, variants=2)
    second = studio.generate(StubProvider(), ["fern frond"], out, variants=2)
    assert sorted(p.path.name for p in second.made) == ["fern-frond-3.png", "fern-frond-4.png"]
    assert len(list(out.glob("*.png"))) == 4


def test_the_manifest_records_the_prompt_and_provider_and_keeps_earlier_runs(tmp_path):
    out = tmp_path / "out"
    studio.generate(StubProvider(), ["fern frond"], out, variants=1)
    batch = studio.generate(StubProvider(), ["surf sunset"], out, variants=1)

    recorded = json.loads(batch.manifest_path.read_text(encoding="utf-8"))
    assert set(recorded) == {"fern-frond-1.png", "surf-sunset-1.png"}
    entry = recorded["surf-sunset-1.png"]
    assert entry["concept"] == "surf sunset"
    assert entry["provider"] == "stub"
    assert entry["prompt"].startswith("surf sunset,")


def test_one_refusal_does_not_lose_the_images_already_paid_for(tmp_path):
    class _FlakyProvider(StubProvider):
        def __init__(self):
            self.calls = 0

        def generate(self, request):
            self.calls += 1
            if self.calls == 2:
                raise DesignError("content policy")
            return super().generate(request)

    batch = studio.generate(
        _FlakyProvider(), ["fern frond", "surf sunset", "desert cactus"], tmp_path
    )
    assert len(batch.made) == 2
    assert len(batch.failed) == 1
    assert "content policy" in batch.failed[0].error
    # The two that arrived are on disk, and the manifest records them.
    assert len(list(tmp_path.glob("*.png"))) == 2


def test_a_concept_that_cannot_be_named_fails_before_anything_is_billed(tmp_path):
    class _CountingProvider(StubProvider):
        def __init__(self):
            self.calls = 0

        def generate(self, request):
            self.calls += 1
            return super().generate(request)

    provider = _CountingProvider()
    with pytest.raises(ValidationError):
        studio.generate(provider, ["fern frond", "!!!"], tmp_path)
    assert provider.calls == 0


def test_an_opaque_design_is_flagged_because_drop_would_not_composite_it(tmp_path):
    class _OpaqueProvider(StubProvider):
        returns_transparency = False

        def generate(self, request):
            buffer = io.BytesIO()
            canvas = Image.new("RGB", (64, 64), (255, 255, 255))
            canvas.paste(Image.new("RGB", (20, 20), (10, 90, 40)), (22, 22))
            canvas.save(buffer, "PNG")
            return buffer.getvalue()

    batch = studio.generate(_OpaqueProvider(), ["fern frond"], tmp_path)
    result = batch.made[0]
    assert any("finished product photo" in w for w in result.warnings)
    assert not mockup.looks_like_artwork(result.path)


def test_cutout_rescues_an_opaque_design_when_asked(tmp_path):
    class _OpaqueProvider(StubProvider):
        def generate(self, request):
            buffer = io.BytesIO()
            canvas = Image.new("RGB", (64, 64), (255, 255, 255))
            canvas.paste(Image.new("RGB", (20, 20), (10, 90, 40)), (22, 22))
            canvas.save(buffer, "PNG")
            return buffer.getvalue()

    batch = studio.generate(_OpaqueProvider(), ["fern frond"], tmp_path, cut_out=True)
    result = batch.made[0]
    assert mockup.looks_like_artwork(result.path)
    assert any("cut out locally" in w for w in result.warnings)


def test_no_concepts_is_refused_before_anything_is_billed(tmp_path):
    with pytest.raises(ValidationError):
        studio.generate(StubProvider(), [], tmp_path)


def test_zero_variants_is_refused():
    with pytest.raises(ValidationError):
        studio.plan(["fern frond"], variants=0)


def test_plan_has_no_side_effects_and_covers_every_image(tmp_path):
    out = tmp_path / "never-created"
    planned = studio.plan(["fern frond", "surf sunset"], variants=2)
    assert len(planned) == 4
    assert not out.exists()


# --- local background removal ---------------------------------------------------


def test_a_flat_border_is_removed_and_the_subject_is_kept(tmp_path):
    path = tmp_path / "design.png"
    canvas = Image.new("RGB", (60, 60), (255, 255, 255))
    canvas.paste(Image.new("RGB", (20, 20), (200, 30, 40)), (20, 20))
    canvas.save(path)

    assert cutout.remove_flat_background(path)
    with Image.open(path) as opened:
        cut = opened.convert("RGBA")
    assert cut.getpixel((0, 0))[3] == 0
    assert cut.getpixel((30, 30))[:3] == (200, 30, 40)
    assert cut.getpixel((30, 30))[3] == 255


def test_artwork_that_is_already_cut_out_is_left_alone(tmp_path):
    path = tmp_path / "design.png"
    Image.new("RGBA", (40, 40), (0, 0, 0, 0)).save(path)
    before = path.read_bytes()
    assert cutout.remove_flat_background(path) is False
    assert path.read_bytes() == before


def test_an_edge_to_edge_image_reports_no_change_rather_than_eating_it(tmp_path):
    # A composition with no uniform border has no background to find. Saying "nothing
    # changed" is the honest answer; guessing at the middle would destroy the design.
    path = tmp_path / "busy.png"
    canvas = Image.new("RGB", (32, 32))
    canvas.putdata([(x * 7 % 256, y * 5 % 256, 90) for y in range(32) for x in range(32)])
    canvas.save(path)
    assert cutout.remove_flat_background(path, tolerance=0) is False


# --- configuration: nothing is chosen for you when money is involved ------------


def test_no_key_means_a_clear_refusal_not_a_traceback():
    with pytest.raises(ConfigError, match="No image generator is configured"):
        DesignConfig.load().build()


def test_a_single_key_needs_no_provider_name(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    config = DesignConfig.load()
    assert config.provider == "openai"
    assert config.build().name == "OpenAI"


def test_two_keys_and_no_choice_is_refused_rather_than_guessed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("STABILITY_API_KEY", "sk-other")
    with pytest.raises(ConfigError, match="will not choose for you"):
        DesignConfig.load()


def test_an_explicit_provider_settles_it(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("STABILITY_API_KEY", "sk-other")
    monkeypatch.setenv("STALLKIT_IMAGE_PROVIDER", "stability")
    assert DesignConfig.load().build().name == "Stability AI"


def test_a_named_provider_without_its_key_says_which_key(monkeypatch):
    monkeypatch.setenv("STALLKIT_IMAGE_PROVIDER", "openai")
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        DesignConfig.load().build()


def test_an_unknown_provider_lists_the_real_ones(monkeypatch):
    monkeypatch.setenv("STALLKIT_IMAGE_PROVIDER", "midjourney")
    with pytest.raises(ConfigError, match="Unknown"):
        DesignConfig.load().build()


# --- provider HTTP behaviour, against a mock transport ---------------------------


def _png_bytes():
    buffer = io.BytesIO()
    Image.new("RGBA", (8, 8), (1, 2, 3, 0)).save(buffer, "PNG")
    return buffer.getvalue()


def _with_transport(provider, handler):
    provider._http = httpx.Client(transport=httpx.MockTransport(handler))
    return provider


def test_openai_asks_for_a_transparent_png_and_decodes_the_answer():
    import base64

    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(_png_bytes()).decode()}]}
        )

    provider = _with_transport(providers.OpenAIImages("sk-test"), handler)
    data = provider.generate(DesignRequest(prompt="fern frond", shape="portrait"))

    assert seen["background"] == "transparent"
    assert seen["output_format"] == "png"
    assert seen["size"] == "1024x1536"
    assert data.startswith(b"\x89PNG")


def test_stability_cuts_the_background_out_in_a_second_call():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=_png_bytes())

    provider = _with_transport(providers.StabilityImages("sk-test"), handler)
    provider.generate(DesignRequest(prompt="fern frond"))

    assert calls == [providers.STABILITY_GENERATE_URL, providers.STABILITY_CUTOUT_URL]


def test_a_refused_prompt_is_not_retried_and_explains_itself():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "content policy"}})

    provider = _with_transport(providers.OpenAIImages("sk-test"), handler)
    with pytest.raises(DesignError, match="content policy"):
        provider.generate(DesignRequest(prompt="a living person"))
    # A 400 is a real answer. Retrying it would bill four times for one refusal.
    assert len(calls) == 1


def test_a_bad_key_says_so_rather_than_retrying():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    provider = _with_transport(providers.OpenAIImages("sk-bad"), handler)
    with pytest.raises(DesignError, match="rejected your API key"):
        provider.generate(DesignRequest(prompt="fern frond"))


def test_a_rate_limit_is_retried_and_then_succeeds(monkeypatch):
    monkeypatch.setattr(providers.time, "sleep", lambda _seconds: None)
    import base64

    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"error": "slow down"})
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(_png_bytes()).decode()}]}
        )

    provider = _with_transport(providers.OpenAIImages("sk-test"), handler)
    assert provider.generate(DesignRequest(prompt="fern frond")).startswith(b"\x89PNG")
    assert len(calls) == 2


def test_an_answer_with_no_image_blames_the_model_not_the_seller():
    def handler(request):
        return httpx.Response(200, json={"data": [{"revised_prompt": "..."}]})

    provider = _with_transport(providers.OpenAIImages("sk-test"), handler)
    with pytest.raises(DesignError, match="answered without an image"):
        provider.generate(DesignRequest(prompt="fern frond"))
