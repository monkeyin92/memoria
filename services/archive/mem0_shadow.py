"""Optional Mem0 evaluation adapter isolated from authoritative Memoria projections."""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Protocol, cast

from services.archive.memory_domain import MemoryKind
from services.archive.memory_evaluation import (
    EvaluationItem,
    EvaluationObservation,
    EvaluationQueryResult,
    MemoryEvaluationCase,
    MemoryEvaluationReport,
    report_json,
)


class Mem0ShadowUnavailableError(RuntimeError):
    """The optional Mem0 package or its configured backend is unavailable."""


class Mem0ClientPort(Protocol):
    def add(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        user_id: str,
        metadata: Mapping[str, object],
    ) -> object: ...

    def search(
        self,
        query: str,
        *,
        user_id: str,
        limit: int,
    ) -> object: ...


Mem0ClientFactory = Callable[[Mapping[str, object]], Mem0ClientPort]


class Mem0ShadowEvaluationAdapter:
    """Run Mem0 only against the synthetic benchmark namespace."""

    name = "mem0-shadow"

    def __init__(
        self,
        *,
        config: Mapping[str, object],
        experiment_id: str,
        client_factory: Mem0ClientFactory | None = None,
    ) -> None:
        if not experiment_id.strip():
            raise ValueError("Mem0 shadow experiment_id must not be blank")
        self._config = dict(config)
        self._experiment_id = experiment_id
        self._factory = client_factory or _default_mem0_factory
        try:
            self._client = self._factory(self._config)
        except (ImportError, ModuleNotFoundError) as exc:
            raise Mem0ShadowUnavailableError(
                "Mem0 shadow requires the optional `mem0ai` package and an isolated "
                "shadow-store configuration"
            ) from exc

    async def observe(self, case: MemoryEvaluationCase) -> EvaluationObservation:
        source_by_item: dict[str, tuple[str, ...]] = {}
        extracted: list[EvaluationItem] = []
        input_tokens = 0
        output_tokens = 0
        try:
            for evidence in case.evidence:
                if evidence.speaker_class != "owner":
                    continue
                namespace = self._namespace(case.case_id, evidence.account_id)
                response = await asyncio.to_thread(
                    self._client.add,
                    ({"role": "user", "content": evidence.text},),
                    user_id=namespace,
                    metadata={
                        "memoria_shadow": True,
                        "experiment_id": self._experiment_id,
                        "case_id": case.case_id,
                        "source_event_id": evidence.event_id,
                        "observed_at": evidence.occurred_at.isoformat(),
                    },
                )
                added = _items(
                    response,
                    account_id=evidence.account_id,
                    fallback_source_event_ids=(evidence.event_id,),
                )
                extracted.extend(added)
                source_by_item.update(
                    (item.item_id, item.source_event_ids) for item in added
                )
                usage = _usage(response)
                input_tokens += usage[0]
                output_tokens += usage[1]

            query_results: list[EvaluationQueryResult] = []
            for query in case.queries:
                started = perf_counter()
                response = await asyncio.to_thread(
                    self._client.search,
                    query.text,
                    user_id=self._namespace(case.case_id, query.account_id),
                    limit=query.limit,
                )
                latency_ms = (perf_counter() - started) * 1000
                usage = _usage(response)
                input_tokens += usage[0]
                output_tokens += usage[1]
                query_results.append(
                    EvaluationQueryResult(
                        query_id=query.query_id,
                        mode=query.mode,
                        latency_ms=latency_ms,
                        items=_items(
                            response,
                            account_id=query.account_id,
                            fallback_source_by_item=source_by_item,
                        ),
                    )
                )
            return EvaluationObservation(
                case_id=case.case_id,
                extracted_items=tuple(_dedupe(extracted)),
                query_results=tuple(query_results),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except Exception as exc:
            # This adapter is evaluation-only. Provider failures become data in the
            # report and can never fail authoritative compilation or context reads.
            return EvaluationObservation(
                case_id=case.case_id,
                extracted_items=(),
                query_results=tuple(
                    EvaluationQueryResult(
                        query_id=query.query_id,
                        mode=query.mode,
                        items=(),
                        latency_ms=0,
                    )
                    for query in case.queries
                ),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error_code=type(exc).__name__,
            )

    def _namespace(self, case_id: str, account_id: str) -> str:
        scope_digest = sha256(f"{case_id}\0{account_id}".encode()).hexdigest()[:24]
        return f"memoria-shadow:{self._experiment_id}:{scope_digest}"


class ShadowEvaluationStore:
    """Separate synthetic-benchmark store; never queried by ContextAssembler."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS shadow_evaluation_runs (
        run_id TEXT PRIMARY KEY,
        experiment_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        dataset_version TEXT NOT NULL,
        case_count INTEGER NOT NULL,
        failed_cases_json TEXT NOT NULL,
        report_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser().resolve()
        self._initialized = False
        self._lock = threading.Lock()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._path) as connection:
                connection.executescript(self._SCHEMA)
            self._initialized = True

    def record(
        self,
        *,
        experiment_id: str,
        report: MemoryEvaluationReport,
    ) -> str:
        self.initialize()
        run_id = str(uuid.uuid4())
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                """
                INSERT INTO shadow_evaluation_runs (
                    run_id, experiment_id, provider, dataset_version, case_count,
                    failed_cases_json, report_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    experiment_id,
                    report.adapter,
                    report.dataset_version,
                    report.case_count,
                    json.dumps(
                        list(report.failed_cases),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    report_json(report),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return run_id


def _default_mem0_factory(config: Mapping[str, object]) -> Mem0ClientPort:
    module = importlib.import_module("mem0")
    memory_type = getattr(module, "Memory", None)
    if memory_type is None:
        raise Mem0ShadowUnavailableError("installed mem0 package does not expose Memory")
    client = memory_type.from_config(dict(config))
    return cast(Mem0ClientPort, client)


def _items(
    response: object,
    *,
    account_id: str,
    fallback_source_event_ids: tuple[str, ...] = (),
    fallback_source_by_item: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[EvaluationItem, ...]:
    result = _result_items(response)
    items: list[EvaluationItem] = []
    for index, raw in enumerate(result):
        item_id = str(raw.get("id") or raw.get("memory_id") or f"mem0-{index}")
        text = str(raw.get("memory") or raw.get("text") or raw.get("content") or "").strip()
        if not text:
            continue
        metadata = raw.get("metadata")
        metadata_map = (
            {str(key): value for key, value in metadata.items()}
            if isinstance(metadata, Mapping)
            else {}
        )
        source = str(metadata_map.get("source_event_id", "")).strip()
        sources = (
            (source,)
            if source
            else (fallback_source_by_item or {}).get(
                item_id,
                fallback_source_event_ids,
            )
        )
        memory_kind = _memory_kind(raw)
        items.append(
            EvaluationItem(
                item_id=item_id,
                account_id=account_id,
                kind="knowledge" if memory_kind == "procedural" else "claim",
                memory_kind=memory_kind,
                title=text[:120],
                body=text,
                # Shadow output cannot promote itself into authoritative confirmed
                # memory; evaluation exposes that boundary explicitly.
                status="candidate",
                source_event_ids=tuple(sources),
                valid_from=_optional_time(metadata_map.get("valid_from")),
                valid_to=_optional_time(metadata_map.get("valid_to")),
                conflict_state="none",
            )
        )
    return tuple(items)


def _result_items(response: object) -> list[Mapping[str, object]]:
    if isinstance(response, Mapping):
        raw = response.get("results", response.get("memories", []))
    else:
        raw = response
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise ValueError("Mem0 shadow response must contain a results array")
    result: list[Mapping[str, object]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("Mem0 shadow result items must be objects")
        result.append({str(key): value for key, value in item.items()})
    return result


def _memory_kind(item: Mapping[str, object]) -> MemoryKind:
    raw = str(item.get("memory_type", "")).casefold()
    if "procedural" in raw:
        return "procedural"
    if "episodic" in raw:
        return "episodic"
    if "relationship" in raw:
        return "relationship"
    return "semantic"


def _usage(response: object) -> tuple[int, int]:
    if not isinstance(response, Mapping):
        return (0, 0)
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return (0, 0)
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
    return (int(str(input_tokens)), int(str(output_tokens)))


def _optional_time(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _dedupe(items: Sequence[EvaluationItem]) -> list[EvaluationItem]:
    result: list[EvaluationItem] = []
    seen: set[str] = set()
    for item in items:
        if item.item_id in seen:
            continue
        seen.add(item.item_id)
        result.append(item)
    return result
