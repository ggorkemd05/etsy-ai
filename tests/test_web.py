"""The browser front end: offline, against a real bound server on a free port.

A local web server holding a seller's Etsy keys is a browser-reachable surface in front
of live credentials, so most of what is pinned here is a refusal: no token, the wrong
token, a token in a cookie where a header is required, a Host that is not this machine,
a path that climbs out of the asset folder. The rest checks that a command still runs the
way the terminal and the window run it.
"""

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from stallkit.web import server as web


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def running(tmp_path, monkeypatch):
    """A live server on a free port, torn down after the test."""
    monkeypatch.setenv("STALLKIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STALLKIT_IGNORE_CWD_ENV", "1")
    monkeypatch.setenv("STALLKIT_IMAGE_PROVIDER", "stub")
    for var in ("ETSY_KEYSTRING", "ETSY_SHARED_SECRET", "OPENAI_API_KEY", "STABILITY_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    server, app = web.build(_free_port())
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield app
    finally:
        server.shutdown()
        server.server_close()
        app.runner.stop(wait=2.0)


def request(app, path, *, method="GET", body=None, token=None, headers=None, host=None):
    """One HTTP call, returning (status, parsed-or-text)."""
    url = f"http://127.0.0.1:{app.port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header(web.TOKEN_HEADER, token)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def run_to_completion(app, args, *, timeout=30.0):
    """Start a command and poll until it finishes, the way the page does."""
    status, job = request(app, "/api/jobs", method="POST", body={"args": args}, token=app.token)
    assert status == 202, job
    seen = 0
    lines = []
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        status, snapshot = request(
            app, f"/api/jobs/{job['id']}?since={seen}&wait=1", token=app.token
        )
        assert status == 200, snapshot
        lines += snapshot["lines"]
        seen = snapshot["total"]
        if snapshot["done"]:
            return snapshot["exit_code"], "\n".join(line["text"] for line in lines)
    raise AssertionError(f"command never finished: {args}")


# --- nothing happens without the token ------------------------------------------


def test_the_api_refuses_a_request_with_no_token(running):
    status, payload = request(running, "/api/state")
    assert status == 403
    assert "token" in payload["error"].lower()


def test_the_api_refuses_the_wrong_token(running):
    status, _ = request(running, "/api/state", token="not-the-token")
    assert status == 403


def test_the_page_refuses_without_the_token_and_explains_where_to_find_it(running):
    status, body = request(running, "/")
    assert status == 403
    assert "token" in body


def test_the_page_opens_with_the_token_in_the_query(running):
    status, body = request(running, f"/?token={running.token}")
    assert status == 200
    # The token reaches the page's JavaScript here and nowhere else.
    assert running.token in body
    assert "__STALLKIT_TOKEN__" not in body


def test_a_cookie_alone_does_not_authorise_an_api_call(running):
    # This is the CSRF defence: a cross-site page can cause the cookie to be sent but
    # cannot set a custom header without a preflight this server never answers.
    status, _ = request(
        running,
        "/api/state",
        headers={"Cookie": f"{web.COOKIE_NAME}={running.token}"},
    )
    assert status == 403


def test_a_cookie_is_enough_for_the_page_itself(running):
    # So a reload that drops the query string still works.
    status, body = request(
        running, "/", headers={"Cookie": f"{web.COOKIE_NAME}={running.token}"}
    )
    assert status == 200
    assert "<title>stallkit</title>" in body


def test_a_host_that_is_not_this_machine_is_refused(running):
    # A public DNS name resolving to 127.0.0.1 is how a remote page reaches a local
    # server, and the browser sends its own Host, not ours.
    status, payload = request(running, "/api/state", token=running.token, host="shop.example.com")
    assert status == 421
    assert "localhost" in payload["error"]


def test_localhost_is_answered(running):
    status, _ = request(
        running, "/api/state", token=running.token, host=f"localhost:{running.port}"
    )
    assert status == 200


# --- assets ----------------------------------------------------------------------


def test_the_assets_are_served_and_need_no_network(running):
    for name, marker in (("app.css", "--accent"), ("app.js", "X-Stallkit-Token")):
        status, body = request(
            running, f"/static/{name}", headers={"Cookie": f"{web.COOKIE_NAME}={running.token}"}
        )
        assert status == 200
        assert marker in body
        # A CDN or a web font would leave the page unstyled exactly when the seller is
        # offline fixing their credentials.
        assert "http://" not in body.replace("http://127.0.0.1", "")
        assert "https://fonts." not in body


def test_a_path_that_climbs_out_of_the_asset_folder_is_refused(running):
    cookie = {"Cookie": f"{web.COOKIE_NAME}={running.token}"}
    # Encoded, because a browser normalises a literal ../ away before sending it.
    status, payload = request(running, "/static/%2e%2e%2fserver.py", headers=cookie)
    assert status == 403
    assert "asset folder" in payload["error"]


# --- state ------------------------------------------------------------------------


def test_state_describes_the_install_without_running_anything(running):
    status, payload = request(running, "/api/state", token=running.token)
    assert status == 200
    assert payload["shops"][0]["index"] == 1
    assert payload["keys"]["has_keystring"] is False
    assert payload["design_provider"] == "stub"
    assert payload["busy"] is False


def test_state_never_returns_a_secret(running):
    request(
        running,
        "/api/keys",
        method="POST",
        token=running.token,
        body={"keystring": "abcdef123456", "shared_secret": "topsecret1"},
    )
    _status, payload = request(running, "/api/state", token=running.token)
    body = json.dumps(payload)
    assert "topsecret1" not in body
    # Enough of the keystring to recognise it, never enough to use it.
    assert payload["keys"]["keystring"] == "abcdef…"
    assert "abcdef123456" not in body


def test_the_translations_are_served_for_both_languages(running):
    status, payload = request(running, "/api/i18n", token=running.token)
    assert status == 200
    assert set(payload["strings"]) == {"en", "tr"}
    assert payload["strings"]["tr"]["tab_design"]


# --- saving credentials ------------------------------------------------------------


def test_half_a_credential_is_refused_rather_than_written(running, tmp_path):
    status, payload = request(
        running,
        "/api/keys",
        method="POST",
        token=running.token,
        body={"keystring": "only-the-keystring"},
    )
    assert status == 400
    assert "both halves" in payload["error"]
    assert not (tmp_path / "home" / ".env").exists()


def test_both_halves_are_written_to_the_shops_own_env(running, tmp_path):
    status, payload = request(
        running,
        "/api/keys",
        method="POST",
        token=running.token,
        body={"keystring": "abc123", "shared_secret": "shh", "redirect_uri": "http://localhost:3003/x"},
    )
    assert status == 200
    written = (tmp_path / "home" / ".env").read_text(encoding="utf-8")
    assert "ETSY_KEYSTRING=abc123" in written
    assert "ETSY_SHARED_SECRET=shh" in written
    assert payload["saved"].endswith(".env")


def test_a_colon_joined_credential_is_split_back_apart(running, tmp_path):
    # Etsy's own error text tells people to join them, so someone will paste it joined.
    # OAuth's client_id must be the bare keystring, so it cannot be stored that way.
    request(
        running,
        "/api/keys",
        method="POST",
        token=running.token,
        body={"keystring": "keypart:secretpart"},
    )
    written = (tmp_path / "home" / ".env").read_text(encoding="utf-8")
    assert "ETSY_KEYSTRING=keypart" in written
    assert "ETSY_SHARED_SECRET=secretpart" in written


# --- running commands -------------------------------------------------------------


def test_a_command_runs_and_its_output_comes_back(running):
    from stallkit import __version__

    code, output = run_to_completion(running, ["--version"])
    assert code == 0
    assert f"stallkit {__version__}" in output


def test_a_failing_command_reports_its_exit_code_not_a_traceback(running):
    code, output = run_to_completion(running, ["seo", "keywords"])
    assert code != 0
    assert "Traceback" not in output


def test_a_library_error_is_a_message_with_a_hint(running):
    # No keystring is configured, so this is the error a new seller meets first.
    code, output = run_to_completion(running, ["listings", "pull"])
    assert code == 1
    assert "ETSY_KEYSTRING" in output
    assert "Traceback" not in output


def test_a_dry_run_validates_a_csv_with_no_credentials_at_all(running, tmp_path):
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "examples" / "listings.csv"
    code, output = run_to_completion(running, ["listings", "push", str(example), "--dry-run"])
    assert code == 0
    assert "Nothing was sent" in output


def _start_a_command_that_waits_for_an_answer(running, tmp_path):
    """`listings push` without --yes stops at its confirmation, which reads stdin.

    Documented behaviour — it is why `--yes` exists for cron — and it is the only way to
    hold a job open for exactly as long as a test needs.
    """
    csv = tmp_path / "rows.csv"
    csv.write_text(
        "listing_id,title,description,price,quantity,who_made,when_made,taxonomy_id\n"
        ",A Mug,A mug,10,1,i_did,made_to_order,1633\n",
        encoding="utf-8",
    )
    status, job = request(
        running,
        "/api/jobs",
        method="POST",
        token=running.token,
        body={"args": ["listings", "push", str(csv)]},
    )
    assert status == 202, job
    # Poll until the command is actually sitting on the question.
    for _attempt in range(200):
        _status, snapshot = request(
            running, f"/api/jobs/{job['id']}?wait=1", token=running.token
        )
        if snapshot["question"] or snapshot["done"]:
            return job["id"], snapshot
    raise AssertionError("the command never asked anything")


def _drain(running, job_id):
    while True:
        _status, snapshot = request(running, f"/api/jobs/{job_id}?wait=1", token=running.token)
        if snapshot["done"]:
            return snapshot


def test_a_command_that_asks_something_gets_the_answer_the_page_types(running, tmp_path):
    job_id, snapshot = _start_a_command_that_waits_for_an_answer(running, tmp_path)
    assert "Proceed?" in snapshot["question"]

    status, _ = request(
        running, f"/api/jobs/{job_id}/answer", method="POST", token=running.token, body={"text": "n"}
    )
    assert status == 200
    finished = _drain(running, job_id)
    text = "\n".join(line["text"] for line in finished["lines"])
    assert "Cancelled" in text
    # Saying no stops the run, and the command says so with a non-zero exit rather than
    # reporting a summary of work it did not do.
    assert finished["exit_code"] == 1
    assert "Created " not in text


def test_only_one_command_runs_at_a_time(running, tmp_path):
    """Serial by design: run_cli redirects stdout process-wide, and commands share
    a token file that a refresh rewrites and an upload lock."""
    job_id, _snapshot = _start_a_command_that_waits_for_an_answer(running, tmp_path)

    status, payload = request(
        running, "/api/jobs", method="POST", token=running.token, body={"args": ["--version"]}
    )
    assert status == 409
    assert "still running" in payload["error"]

    request(
        running, f"/api/jobs/{job_id}/answer", method="POST", token=running.token, body={"text": "n"}
    )
    _drain(running, job_id)
    # And once it is done, the next command is accepted.
    status, _ = request(
        running, "/api/jobs", method="POST", token=running.token, body={"args": ["--version"]}
    )
    assert status == 202


def test_answering_a_command_that_is_gone_says_so(running):
    status, payload = request(
        running, "/api/jobs/nope/answer", method="POST", token=running.token, body={"text": "y"}
    )
    assert status == 404
    assert "no longer here" in payload["error"]


def test_an_empty_command_is_refused(running):
    for args in ([], "listings", [1, 2]):
        status, _ = request(
            running, "/api/jobs", method="POST", token=running.token, body={"args": args}
        )
        assert status == 400


def test_output_is_replayed_from_the_start_for_a_tab_that_reconnects(running):
    code, _ = run_to_completion(running, ["--version"])
    assert code == 0
    job_id = next(iter(running.runner.jobs))
    _status, snapshot = request(running, f"/api/jobs/{job_id}?since=0", token=running.token)
    # A browser can be closed mid-command, which a terminal cannot, so the log is kept.
    assert snapshot["lines"]
    assert snapshot["done"] is True


def test_asking_about_a_forgotten_job_says_so(running):
    status, payload = request(running, "/api/jobs/nope", token=running.token)
    assert status == 404
    assert "no longer here" in payload["error"]


def test_anonymised_output_hides_the_shop(running, tmp_path):
    csv = tmp_path / "one.csv"
    csv.write_text(
        "listing_id,title,description,price,quantity,who_made,when_made,taxonomy_id\n"
        ",Secret Shop Mug,A mug,10,1,i_did,made_to_order,1633\n",
        encoding="utf-8",
    )
    status, job = request(
        running,
        "/api/jobs",
        method="POST",
        token=running.token,
        body={"args": ["listings", "push", str(csv), "--dry-run"], "anonymise": True},
    )
    assert status == 202
    while True:
        _status, snapshot = request(
            running, f"/api/jobs/{job['id']}?wait=1", token=running.token
        )
        if snapshot["done"]:
            break
    text = "\n".join(line["text"] for line in snapshot["lines"])
    assert "Secret Shop Mug" not in text


# --- a browser cannot return a path, so the server lists folders -------------------


def test_browsing_a_folder_lists_its_children(running, tmp_path):
    (tmp_path / "designs").mkdir()
    (tmp_path / "designs" / "sub").mkdir()
    (tmp_path / "designs" / "fern.png").write_bytes(b"x")
    (tmp_path / "designs" / ".hidden").write_bytes(b"x")

    status, payload = request(
        running,
        "/api/browse",
        method="POST",
        token=running.token,
        body={"path": str(tmp_path / "designs")},
    )
    assert status == 200
    names = {entry["name"]: entry["dir"] for entry in payload["entries"]}
    assert names == {"sub": True, "fern.png": False}
    assert payload["parent"] == str(tmp_path)


def test_browsing_somewhere_that_is_not_a_folder_falls_back(running, tmp_path):
    status, payload = request(
        running,
        "/api/browse",
        method="POST",
        token=running.token,
        body={"path": str(tmp_path / "nope" / "deeper")},
    )
    assert status == 200
    assert payload["path"]


# --- preferences ------------------------------------------------------------------


def test_a_folder_choice_is_remembered_for_the_shop(running):
    request(
        running,
        "/api/prefs",
        method="POST",
        token=running.token,
        body={"workspace": "/tmp/Etsy Studio", "language": "tr"},
    )
    _status, payload = request(running, "/api/state", token=running.token)
    assert payload["workspace"] == "/tmp/Etsy Studio"
    assert payload["language"] == "tr"


def test_an_unknown_preference_is_ignored_rather_than_stored(running):
    request(
        running,
        "/api/prefs",
        method="POST",
        token=running.token,
        body={"shared_secret": "nice try"},
    )
    _status, payload = request(running, "/api/state", token=running.token)
    assert "nice try" not in json.dumps(payload)


def test_an_unknown_path_is_a_clean_404(running):
    status, payload = request(running, "/api/nothing", token=running.token)
    assert status == 404
    assert payload["error"]
