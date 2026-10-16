"""Two hardening properties that matter once the code is public.

* With nothing real configured the service boots open (the zero-config quick start and this
  suite depend on it); as soon as a real model, a non-stub search provider or a wallet is
  configured, an unset API_AUTH_TOKEN refuses the boot (fail closed) unless the explicit local
  override is set.
* HTML prose extraction is linear in the page size: pages full of unclosed <script> or <p>
  tags used to make the lazy regexes backtrack quadratically and pin the event loop.
"""
import time

import pytest

from src.config.settings import SettingsError, get_settings
from src.search.fetch import _strip_tags, html_to_text, markdown_to_text, prose_of

TOKEN = "test-api-token-0123456789abcdef-0123456789"


def _synthetic(monkeypatch, **over):
    s = get_settings()
    values = {"API_AUTH_TOKEN": "", "ALLOW_UNAUTHENTICATED_AUDIT": False, "MODEL_ENABLED": False,
              "PROVIDER_TYPE": "local", "WEB_SEARCH_PROVIDER": "stub", "KARAKUTU_WALLET_JSON": "",
              "KARAKUTU_WALLET_PATH": "arweave-keyfile.json"}
    values.update(over)
    for k, v in values.items():
        monkeypatch.setattr(s, k, v)
    return s


def test_synthetic_defaults_boot_without_a_token(monkeypatch):
    _synthetic(monkeypatch).validate()


@pytest.mark.parametrize("real", [
    {"MODEL_ENABLED": True},
    {"PROVIDER_TYPE": "hosted"},
    {"WEB_SEARCH_PROVIDER": "searxng"},
    {"KARAKUTU_WALLET_JSON": '{"kty": "RSA"}'},
])
def test_a_real_configuration_without_a_token_refuses_to_boot(monkeypatch, real):
    s = _synthetic(monkeypatch, **real)
    with pytest.raises(SettingsError) as exc:
        s.validate()
    assert "API_AUTH_TOKEN is unset" in str(exc.value)
    assert "ALLOW_UNAUTHENTICATED_AUDIT" in str(exc.value)


def test_a_wallet_file_counts_as_real(monkeypatch, tmp_path):
    wallet = tmp_path / "keyfile.json"
    wallet.write_text("{}")
    s = _synthetic(monkeypatch, KARAKUTU_WALLET_PATH=str(wallet))
    with pytest.raises(SettingsError):
        s.validate()
    _synthetic(monkeypatch, KARAKUTU_WALLET_PATH=str(tmp_path / "missing.json")).validate()


def test_a_token_or_the_explicit_override_boots_a_real_configuration(monkeypatch):
    _synthetic(monkeypatch, MODEL_ENABLED=True, API_AUTH_TOKEN=TOKEN).validate()
    _synthetic(monkeypatch, MODEL_ENABLED=True, ALLOW_UNAUTHENTICATED_AUDIT=True).validate()


def test_the_refusal_never_echoes_the_token(monkeypatch):
    s = _synthetic(monkeypatch, MODEL_ENABLED=True, API_AUTH_TOKEN="hunter2-tiny")
    with pytest.raises(SettingsError) as exc:
        s.validate()
    assert "hunter2" not in str(exc.value)


# --- linear extraction ---------------------------------------------------------------------

def test_extraction_output_is_unchanged_for_ordinary_markup():
    page = ("<html><head><style>p{}</style><script>var x = '<p>not prose</p>';</script></head>"
            "<body><nav><a>Home</a></nav>"
            "<P class='lead'>First paragraph of the article body, long enough to count as prose.</P>"
            # paragraphs are collected before hidden blocks are dropped (unchanged behaviour)
            "<p>Short.</p><noscript><p>Enable JS, please, this paragraph is long enough too.</p></noscript>"
            "<p>Second paragraph of the article body, also long enough &amp; entity-decoded.</p>"
            "</body></html>")
    assert html_to_text(page) == ("First paragraph of the article body, long enough to count as prose.\n"
                                  "Enable JS, please, this paragraph is long enough too.\n"
                                  "Second paragraph of the article body, also long enough & entity-decoded.")
    assert _strip_tags("<b>a</b> <script>x</SCRIPT> b <script>unclosed") == "a b unclosed"
    assert html_to_text("<div>no paragraphs here at all, just a div with text</div>") == \
        "no paragraphs here at all, just a div with text"


@pytest.mark.parametrize("page", [
    "<script>" * 100_000,
    "<p>" * 100_000,
    "<p>x</p>" * 50_000 + "<p>",
    "<style>" * 50_000 + "</style>" + "<noscript>" * 50_000,
])
def test_hostile_pages_are_extracted_in_linear_time(page):
    started = time.perf_counter()
    html_to_text(page)
    _strip_tags(page)
    assert time.perf_counter() - started < 2.0, "extraction must not backtrack on hostile markup"


@pytest.mark.parametrize("page", [
    "<script" * 200_000,
    "<p" * 200_000,
    "<x" * 200_000,
    "<p>" * 200_000 + "</p>",
])
def test_unclosed_tags_are_extracted_in_linear_time(page):
    started = time.perf_counter()
    html_to_text(page)
    prose_of(page)
    assert time.perf_counter() - started < 2.0


@pytest.mark.parametrize("md", [
    "[" * 200_000,
    "![" * 200_000,
    "[a]" * 200_000,
    "<http://" * 200_000,
    "\n" * 200_000,
    "\n [a]:" * 50_000,
    "- " * 400_000,
])
def test_hostile_markdown_is_extracted_in_linear_time(md):
    started = time.perf_counter()
    markdown_to_text(md)
    assert time.perf_counter() - started < 2.0


def test_markdown_extraction_output_is_unchanged_for_ordinary_markdown():
    md = ("# Title\n\n![alt text](https://img.example/a.png) Intro paragraph with a [link](https://x.example/p) "
          "and a [ref][1] and <https://auto.example/> plus https://bare.example/q — long enough to count.\n\n"
          "[1]: https://ref.example/\n\n---\n\n> quoted paragraph that is also long enough to be kept as prose text.\n")
    assert markdown_to_text(md) == (
        "alt text Intro paragraph with a link and a ref and plus — long enough to count.\n"
        "quoted paragraph that is also long enough to be kept as prose text.")
