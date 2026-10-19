#!/usr/bin/env python3
"""Generate agent_core/core/models/model_catalog.json, the model catalog.

The catalog holds each model's context window and, where the provider caps
input below the window, its input limit. LLMInterface budgets every request
from it (agent_core/core/models/catalog.py); a model with no row uses
settings.json model.context_window.

Sources, all structured fields (nothing is inferred from a model's name):

* models.dev (MODELS_DEV_URL): ``limit.context`` and ``limit.input`` of every
  model of each provider profile's ``catalog_provider``
  (agent_core/core/models/provider_config.py), keyed ``<profile key>/<id>``.
* the Codex model catalogue (CODEX_CATALOG_URL): ``context_window`` of each
  model on the ChatGPT-subscription surface, the window Codex runs it at.
* scripts/catalog_overrides.json: rows both sources lack. A row is either
  ``{"same_as": "<catalog key>"}``, which copies a row the sources produced,
  or ``{"context_window": N}`` with an optional ``"max_input_tokens"``.

The output is deterministic (sorted, one row per line, no timestamps), so a
regeneration diff shows exactly which models changed. Run it by hand when
providers ship models; nothing is fetched at runtime.

Usage:
    python scripts/gen_model_catalog.py
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent_core.core.models.catalog import CATALOG_PATH  # noqa: E402
from agent_core.core.models.provider_config import PROVIDER_CONFIG  # noqa: E402
from agent_core.core.models.reasoning import (  # noqa: E402
    SUBSCRIPTION_AUTH_MODE,
    reasoning_surface,
)

MODELS_DEV_URL = "https://models.dev/api.json"
CODEX_CATALOG_URL = (
    "https://raw.githubusercontent.com/openai/codex/main/"
    "codex-rs/models-manager/models.json"
)
OVERRIDES_PATH = Path(__file__).with_name("catalog_overrides.json")

Row = Dict[str, int]


def _fetch_json(url: str) -> Any:
    import certifi

    # certifi's bundle, not the OS store: a broken certificate in the Windows
    # store fails the default context before any request is made.
    context = ssl.create_default_context(cafile=certifi.where())
    request = urllib.request.Request(
        url, headers={"User-Agent": "CraftBot gen_model_catalog.py"}
    )
    with urllib.request.urlopen(request, timeout=60, context=context) as response:
        return json.load(response)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _models_dev_rows(models_dev: Dict[str, Any]) -> Dict[str, Row]:
    rows: Dict[str, Row] = {}
    for profile in PROVIDER_CONFIG.values():
        if profile.catalog_provider is None:
            continue
        models = models_dev[profile.catalog_provider]["models"]
        for model_id, model in models.items():
            limit = model.get("limit") or {}
            context = limit.get("context")
            if not _positive_int(context):
                continue
            row: Row = {"context_window": context}
            if _positive_int(limit.get("input")) and limit["input"] < context:
                row["max_input_tokens"] = limit["input"]
            rows[f"{profile.key}/{model_id}"] = row
    return rows


def _codex_rows(codex: Dict[str, Any]) -> Dict[str, Row]:
    surface = reasoning_surface("openai", SUBSCRIPTION_AUTH_MODE)
    return {
        f"{surface}/{model['slug']}": {"context_window": model["context_window"]}
        for model in codex["models"]
        if _positive_int(model.get("context_window"))
    }


def _override_rows(sourced: Dict[str, Row]) -> Dict[str, Row]:
    overrides = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))["models"]
    rows: Dict[str, Row] = {}
    for key, override in overrides.items():
        if "same_as" in override:
            target = sourced.get(override["same_as"])
            if target is None:
                raise SystemExit(
                    f"{OVERRIDES_PATH.name}: {key} is same_as "
                    f"{override['same_as']}, which no source lists"
                )
            rows[key] = dict(target)
            continue
        if not _positive_int(override.get("context_window")):
            raise SystemExit(f"{OVERRIDES_PATH.name}: {key} needs a context_window")
        row: Row = {"context_window": override["context_window"]}
        if "max_input_tokens" in override:
            if not _positive_int(override["max_input_tokens"]):
                raise SystemExit(
                    f"{OVERRIDES_PATH.name}: {key} has an invalid max_input_tokens"
                )
            row["max_input_tokens"] = override["max_input_tokens"]
        rows[key] = row
    return rows


def _render(rows: Dict[str, Row]) -> str:
    items = sorted(rows.items())
    lines = [
        "{",
        '  "generator": "scripts/gen_model_catalog.py",',
        '  "sources": '
        + json.dumps(
            [MODELS_DEV_URL, CODEX_CATALOG_URL, "scripts/catalog_overrides.json"]
        )
        + ",",
        '  "models": {',
    ]
    for index, (key, row) in enumerate(items):
        comma = "," if index < len(items) - 1 else ""
        lines.append(f"    {json.dumps(key)}: {json.dumps(row, sort_keys=True)}{comma}")
    lines += ["  }", "}"]
    return "\n".join(lines) + "\n"


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    sourced = _models_dev_rows(_fetch_json(MODELS_DEV_URL))
    sourced.update(_codex_rows(_fetch_json(CODEX_CATALOG_URL)))
    rows = {**sourced, **_override_rows(sourced)}

    text = _render(rows)
    json.loads(text)  # the hand-rendered layout must stay valid JSON
    CATALOG_PATH.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(rows)} models to {CATALOG_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
