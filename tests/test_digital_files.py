"""Digital downloads: the file the buyer takes away after paying.

Everything here is offline. The point of these tests is that a row Etsy would refuse
fails *before* the draft exists, because a digital listing with no attached file is
one stallkit cannot fix afterwards — it holds no delete scope.
"""

from pathlib import Path

import pytest

from stallkit.client import MAX_FILE_BYTES, MAX_LISTING_FILES, file_problem
from stallkit.errors import ValidationError
from stallkit.listings import prepare, push

BASE = {
    "listing_id": "",
    "title": "Mountain Sunset Printable Wall Art",
    "description": "Instant download, three sizes.",
    "price": "6.00",
    "quantity": "999",
    "who_made": "i_did",
    "when_made": "made_to_order",
    "taxonomy_id": "2078",
    "type": "download",
}


class _RecordingClient:
    """Stands in for EtsyClient and records what each row would have sent."""

    def __init__(self, fail_files: bool = False):
        self.created: list[dict] = []
        self.images: list[Path] = []
        self.files: list[tuple[str, int, str]] = []
        self.fail_files = fail_files

    def create_draft_listing(self, payload):
        self.created.append(payload)
        return {"listing_id": 800 + len(self.created)}

    def update_listing(self, listing_id, payload):
        return {"listing_id": listing_id}

    def upload_listing_image(self, listing_id, image, *, rank=1, alt_text=""):
        self.images.append(image)
        return {"listing_image_id": rank}

    def upload_listing_file(self, listing_id, path, *, rank=1, name=""):
        if self.fail_files:
            raise OSError("upload refused")
        self.files.append((path.name, rank, name))
        return {"listing_file_id": rank}


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "mountain-sunset-a2.pdf"
    path.write_bytes(b"%PDF-1.4\nprintable\n%%EOF")
    return path


# --- the happy path ------------------------------------------------------------


def test_a_download_is_uploaded_after_the_draft_is_created(tmp_path, pdf):
    client = _RecordingClient()
    row = dict(BASE, files=pdf.name)
    report = push(client, [row], base_dir=tmp_path)

    assert report.created == 1
    assert report.files == 1
    assert client.files == [(pdf.name, 1, "")]
    assert report.results[0].status == "ok"


def test_several_downloads_keep_their_csv_order(tmp_path):
    for name in ("a4.pdf", "a3.pdf", "a2.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-1.4")
    client = _RecordingClient()
    row = dict(BASE, files="a4.pdf|a3.pdf|a2.pdf")
    push(client, [row], base_dir=tmp_path)

    # Rank is the buyer's ordering on the downloads page, so it follows the column.
    assert client.files == [("a4.pdf", 1, ""), ("a3.pdf", 2, ""), ("a2.pdf", 3, "")]


def test_a_dry_run_counts_the_downloads_without_sending_them(tmp_path, pdf):
    report = push(None, [dict(BASE, files=pdf.name)], base_dir=tmp_path, dry_run=True)
    assert "1 download(s)" in report.results[0].message


# --- refusals that have to land before the create ------------------------------


def test_a_download_on_a_physical_listing_is_refused(tmp_path, pdf):
    # Etsy answers 404 on the files endpoint for a physical listing, by which point the
    # draft already exists.
    result = prepare([dict(BASE, type="physical", files=pdf.name)], base_dir=tmp_path)[0]
    assert result.result.failed
    assert "physical" in result.result.message


def test_a_download_on_a_both_listing_is_allowed(tmp_path, pdf):
    result = prepare([dict(BASE, type="both", files=pdf.name)], base_dir=tmp_path)[0]
    assert not result.result.failed
    assert result.file_paths == [pdf]


def test_more_files_than_etsy_allows_fails_the_row(tmp_path):
    names = []
    for index in range(MAX_LISTING_FILES + 1):
        name = f"size-{index}.pdf"
        (tmp_path / name).write_bytes(b"%PDF-1.4")
        names.append(name)
    result = prepare([dict(BASE, files="|".join(names))], base_dir=tmp_path)[0]
    assert result.result.failed
    assert "Nothing was dropped" in result.result.message


def test_a_missing_file_fails_the_row(tmp_path):
    result = prepare([dict(BASE, files="not-there.pdf")], base_dir=tmp_path)[0]
    assert result.result.failed
    assert "file not found" in result.result.message


def test_a_suffix_etsy_refuses_fails_the_row(tmp_path):
    (tmp_path / "working.psd").write_bytes(b"8BPS")
    result = prepare([dict(BASE, files="working.psd")], base_dir=tmp_path)[0]
    assert result.result.failed
    assert ".psd" in result.result.message


def test_an_empty_file_is_refused(tmp_path):
    (tmp_path / "blank.pdf").write_bytes(b"")
    assert "is empty" in (file_problem(tmp_path / "blank.pdf") or "")


def test_a_file_over_etsys_limit_is_refused(tmp_path, monkeypatch):
    big = tmp_path / "huge.zip"
    big.write_bytes(b"x" * 64)
    real_stat = Path.stat

    def fake_stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self.name == "huge.zip":
            class _Stat:
                st_size = MAX_FILE_BYTES + 1
            return _Stat()
        return result

    monkeypatch.setattr(Path, "stat", fake_stat)
    problem = file_problem(big)
    assert problem and "20MB" in problem


def test_an_unknown_suffix_never_gets_a_guessed_mime_type(tmp_path):
    from stallkit.client import _file_mime_for

    (tmp_path / "model.blend").write_bytes(b"BLENDER")
    with pytest.raises(ValidationError):
        _file_mime_for(tmp_path / "model.blend")


# --- a failure after the create is `partial`, never `error` --------------------


def test_a_failed_download_leaves_the_row_partial_and_says_it_cannot_publish(tmp_path, pdf):
    client = _RecordingClient(fail_files=True)
    report = push(client, [dict(BASE, files=pdf.name)], base_dir=tmp_path)
    result = report.results[0]

    assert result.status == "partial"
    assert not result.failed
    assert result.listing_id == 801
    assert "cannot be published" in result.message
    assert report.created == 1


def test_downloads_are_skipped_entirely_with_no_images(tmp_path, pdf):
    # --no-images means "send no binaries". Failing the row over a download that this
    # run will not send would stop a seller fixing their titles.
    result = prepare([dict(BASE, files="gone.pdf")], base_dir=tmp_path, upload_images=False)[0]
    assert not result.result.failed
    assert result.file_paths == []


def test_pull_never_writes_a_files_path(tmp_path):
    # Etsy hands back a file id and name, not a local path. Writing one would attach a
    # second copy of the same download on the next push.
    from stallkit.listings import pull

    class _Client:
        def listings_by_shop(self, *, state="active", max_items=None, includes=None):
            return iter([{"listing_id": 5, "title": "x", "listing_type": "download"}])

    assert pull(_Client())[0]["files"] == ""
