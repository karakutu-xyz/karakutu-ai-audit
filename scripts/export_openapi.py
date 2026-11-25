"""Generate/check the public API schema, never sample requests or article records.

The contract is written as YAML for people to read: sorted keys, block style, no anchors or
aliases, and non-ASCII text kept as is, so the same application always gives the same bytes
and `--check` can compare them exactly (UTF-8, LF line endings). Strings that a YAML 1.2
reader would take for a number, boolean or null are quoted, and the rendered text must load
back to the same schema. The running app still serves `/openapi.json`.
"""
import argparse
import re
from pathlib import Path

import yaml

# Plain scalars the YAML 1.2 core schema resolves to null, bool, int or float. PyYAML follows
# YAML 1.1 and would leave some of these unquoted (for example "1e3" or "0o17").
_YAML12_NON_STRING = re.compile(
    r"""^(?:~|null|Null|NULL|true|True|TRUE|false|False|FALSE
        |[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+
        |[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?
        |[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$""",
    re.VERBOSE,
)


class _ContractDumper(yaml.SafeDumper):
    """Write a shared schema object out in full each time instead of as an anchor/alias."""

    def ignore_aliases(self, data):
        return True


def _represent_str(dumper, value):
    style = '"' if value == "" or _YAML12_NON_STRING.match(value) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_ContractDumper.add_representer(str, _represent_str)


def render(schema: dict) -> bytes:
    text = yaml.dump(
        schema, Dumper=_ContractDumper, sort_keys=True, default_flow_style=False,
        allow_unicode=True, width=100,
    )
    if yaml.safe_load(text) != schema:
        raise SystemExit("OpenAPI contract does not load back to the same schema")
    return text.encode("utf-8")


def main(argv=None) -> None:
    from audit.api import create_app
    from audit.config import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    path = Path(__file__).resolve().parents[1] / "openapi.yaml"
    value = render(create_app(Settings()).openapi())
    if args.check:
        if not path.exists() or path.read_bytes() != value:
            raise SystemExit("OpenAPI contract is stale; run scripts/export_openapi.py")
        print("OpenAPI contract matches the application.")
    else:
        path.write_bytes(value)
        print("Wrote openapi.yaml.")


if __name__ == "__main__":
    main()
