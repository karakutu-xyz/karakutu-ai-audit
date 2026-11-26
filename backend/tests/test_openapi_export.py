"""The checked-in OpenAPI contract is stable YAML that any reader loads as the same schema."""
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("export_openapi", ROOT / "scripts" / "export_openapi.py")
export_openapi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_openapi)


@pytest.mark.parametrize("value", [
    "1e3", "-2.5E-4", ".5", "0o17", "0x1F", "12", "+7", ".inf", "-.Inf", ".NaN",
    "null", "Null", "~", "true", "False", "yes", "off", "", "0.1.0",
])
def test_strings_a_yaml_reader_could_misread_stay_strings(value):
    text = export_openapi.render({"default": value}).decode("utf-8")
    assert yaml.safe_load(text) == {"default": value}
    if export_openapi._YAML12_NON_STRING.match(value) or value == "":
        assert f'"{value}"' in text


def test_output_is_sorted_block_style_without_aliases_and_keeps_unicode():
    shared = {"type": "string"}
    text = export_openapi.render({
        "b": {"x": shared, "y": shared}, "a": "Türkçe başlık",
    }).decode("utf-8")
    assert text.index("a:") < text.index("b:")
    assert "&" not in text and "*" not in text
    assert "Türkçe başlık" in text
    assert "{" not in text
    assert "\r" not in text


def test_checked_in_contract_matches_the_application():
    from audit.api import create_app
    from audit.config import Settings

    path = ROOT / "openapi.yaml"
    assert path.read_bytes() == export_openapi.render(create_app(Settings()).openapi())
