"""Every outbound semantic classifier sends redacted text to its provider.

Interruption has no outbound classifier: ``InterruptSemanticVerdict`` is a
local enum and nothing posts interrupt text, so it is not in this table.
"""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
from typing import Any

import httpx
import pytest
from services.agent.src import providers
from services.agent.src.providers.close_intent_semantic_classifier import (
    CloseIntentSemanticClassifier,
    CloseIntentSemanticClassifierConfig,
)
from services.agent.src.providers.crisis_semantic_classifier import (
    CrisisSemanticClassifier,
    CrisisSemanticClassifierConfig,
)
from services.agent.src.providers.live_lookup_semantic_classifier import (
    LiveLookupSemanticClassifier,
    LiveLookupSemanticClassifierConfig,
)

CLASSIFIERS: tuple[tuple[type[Any], type[Any]], ...] = (
    (CrisisSemanticClassifier, CrisisSemanticClassifierConfig),
    (CloseIntentSemanticClassifier, CloseIntentSemanticClassifierConfig),
    (LiveLookupSemanticClassifier, LiveLookupSemanticClassifierConfig),
)


async def _sent_user_content(classifier_type: type[Any], config_type: type[Any], text: str) -> str:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "UNSURE"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    classifier = classifier_type(
        config_type(api_key="test-secret", base_url="https://provider.test/v1"),
        client=client,
    )
    await classifier.classify(current_text=text)
    await client.aclose()
    assert len(bodies) == 1
    return str(bodies[0]["messages"][1]["content"])


@pytest.mark.asyncio
@pytest.mark.parametrize(("classifier_type", "config_type"), CLASSIFIERS)
async def test_classifier_redacts_before_the_length_bound(
    classifier_type: type[Any], config_type: type[Any]
) -> None:
    # Slicing first would cut the number to "345678" and send it unredacted.
    content = await _sent_user_content(classifier_type, config_type, "13812345678" + "好" * 506)

    assert content == "[手机号]" + "好" * 506


@pytest.mark.asyncio
@pytest.mark.parametrize(("classifier_type", "config_type"), CLASSIFIERS)
async def test_classifier_keeps_risk_words_beside_a_redacted_address(
    classifier_type: type[Any], config_type: type[Any]
) -> None:
    content = await _sent_user_content(classifier_type, config_type, " 我不想活了住在南京市中山路1号 ")

    assert content == "我不想活了住在[地址]"


def test_every_outbound_semantic_classifier_is_covered() -> None:
    discovered: set[str] = set()
    for module_info in pkgutil.iter_modules(providers.__path__):
        module = importlib.import_module(f"{providers.__name__}.{module_info.name}")
        discovered |= {
            name
            for name, value in inspect.getmembers(module, inspect.isclass)
            if name.endswith("SemanticClassifier") and value.__module__ == module.__name__
        }

    assert discovered == {classifier.__name__ for classifier, _ in CLASSIFIERS}
