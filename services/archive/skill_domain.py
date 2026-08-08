"""Executable procedural-memory contracts with explicit approval and audit boundaries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from services.archive.memory_domain import DomainCategory, MemorySensitivity

SkillSourceKind = Literal["explicit_instruction", "repeated_tool_success"]
SkillVersionStatus = Literal["candidate", "approved", "superseded", "retired"]
SkillRunStatus = Literal["running", "succeeded", "failed"]
SkillStepPhase = Literal["forward", "compensation"]
SkillStepStatus = Literal["succeeded", "failed"]
SkillRollbackStatus = Literal["not_required", "completed", "partial"]


class SkillError(RuntimeError):
    """Base error for executable procedural memory."""


class SkillNotFoundError(SkillError):
    """A skill, version or run was not found inside the account boundary."""


class SkillApprovalRequiredError(SkillError):
    """A candidate or superseded skill was requested for execution."""


class SkillConfirmationRequiredError(SkillError):
    """The current execution lacks a fresh owner confirmation evidence event."""


class SkillSchemaValidationError(SkillError):
    """An input, output or stored JSON Schema failed validation."""


class SkillExecutionError(SkillError):
    """A skill run failed after its audit record was durably created."""

    def __init__(self, run_id: str, message: str) -> None:
        super().__init__(message)
        self.run_id = run_id


@dataclass(frozen=True, slots=True)
class SkillStepDefinition:
    step_id: str
    tool_name: str
    arguments: Mapping[str, object]
    compensation_tool_name: str | None = None
    compensation_arguments: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.step_id.strip() or not self.tool_name.strip():
            raise ValueError("skill step requires step_id and tool_name")
        if (self.compensation_tool_name is None) != (
            self.compensation_arguments is None
        ):
            raise ValueError(
                "skill compensation tool and arguments must be configured together"
            )
        _json_text(dict(self.arguments))
        if self.compensation_arguments is not None:
            _json_text(dict(self.compensation_arguments))


@dataclass(frozen=True, slots=True)
class SkillProposal:
    account_id: str
    name: str
    description: str
    trigger_phrases: tuple[str, ...]
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    output_template: Mapping[str, object]
    allowed_tools: tuple[str, ...]
    steps: tuple[SkillStepDefinition, ...]
    source_kind: SkillSourceKind
    source_event_ids: tuple[str, ...]
    domain_category: DomainCategory = "daily_life"
    sensitivity: MemorySensitivity = "personal"
    salience: float = 0.7

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not self.name.strip():
            raise ValueError("skill proposal requires account_id and name")
        if not self.description.strip() or not self.trigger_phrases:
            raise ValueError("skill proposal requires description and trigger phrases")
        if not self.steps:
            raise ValueError("skill proposal requires at least one step")
        if not 0 <= self.salience <= 1:
            raise ValueError("skill salience must be between zero and one")
        if len(set(self.trigger_phrases)) != len(self.trigger_phrases):
            raise ValueError("skill trigger phrases must be unique")
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("skill evidence ids must be unique")
        minimum_sources = 3 if self.source_kind == "repeated_tool_success" else 1
        if len(self.source_event_ids) < minimum_sources:
            raise ValueError(
                f"{self.source_kind} requires at least {minimum_sources} evidence events"
            )
        allowed = set(self.allowed_tools)
        if not allowed or any(not tool.strip() for tool in allowed):
            raise ValueError("skill allowed_tools must not be empty or blank")
        step_ids = [step.step_id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("skill step ids must be unique")
        used_tools = {
            tool
            for step in self.steps
            for tool in (step.tool_name, step.compensation_tool_name)
            if tool is not None
        }
        if not used_tools <= allowed:
            raise ValueError("every forward and compensation tool must be allowlisted")
        validate_json_schema(self.input_schema, require_object=True)
        validate_json_schema(self.output_schema, require_object=True)
        _json_text(dict(self.output_template))


@dataclass(frozen=True, slots=True)
class SkillApproval:
    account_id: str
    skill_id: str
    version: int
    approval_event_id: str

    def __post_init__(self) -> None:
        if (
            not self.account_id.strip()
            or not self.skill_id.strip()
            or not self.approval_event_id.strip()
            or self.version < 1
        ):
            raise ValueError("skill approval requires account, skill, version and evidence")


@dataclass(frozen=True, slots=True)
class SkillRunRequest:
    account_id: str
    skill_id: str
    version: int
    confirmation_event_id: str
    inputs: Mapping[str, object]
    evolution_candidate_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.account_id.strip()
            or not self.skill_id.strip()
            or not self.confirmation_event_id.strip()
            or self.version < 1
        ):
            raise ValueError("skill run requires account, version and confirmation evidence")
        _json_text(dict(self.inputs))
        if self.evolution_candidate_id is not None and (
            not self.evolution_candidate_id.strip() or len(self.evolution_candidate_id) > 128
        ):
            raise ValueError("evolution_candidate_id must be a bounded non-empty id")


@dataclass(frozen=True, slots=True)
class SkillVersion:
    skill_id: str
    account_id: str
    name: str
    description: str
    version: int
    status: SkillVersionStatus
    trigger_phrases: tuple[str, ...]
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    output_template: Mapping[str, object]
    allowed_tools: tuple[str, ...]
    steps: tuple[SkillStepDefinition, ...]
    source_kind: SkillSourceKind
    source_event_ids: tuple[str, ...]
    domain_category: DomainCategory
    sensitivity: MemorySensitivity
    salience: float
    created_at: datetime
    approved_at: datetime | None = None
    approval_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class SkillRunStep:
    sequence: int
    step_id: str
    phase: SkillStepPhase
    tool_name: str
    arguments: Mapping[str, object]
    status: SkillStepStatus
    output: object | None
    error_code: str | None
    started_at: datetime
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class SkillRun:
    run_id: str
    account_id: str
    skill_id: str
    version: int
    status: SkillRunStatus
    rollback_status: SkillRollbackStatus
    confirmation_event_id: str
    inputs: Mapping[str, object]
    output: Mapping[str, object] | None
    error_code: str | None
    started_at: datetime
    completed_at: datetime | None
    steps: tuple[SkillRunStep, ...] = ()


class SkillCatalogPort(Protocol):
    async def propose(self, proposal: SkillProposal) -> SkillVersion: ...

    async def approve(self, command: SkillApproval) -> SkillVersion: ...

    async def get_version(
        self,
        *,
        account_id: str,
        skill_id: str,
        version: int,
    ) -> SkillVersion: ...

    async def list_versions(
        self,
        *,
        account_id: str,
    ) -> tuple[SkillVersion, ...]: ...

    async def rebuild_search_projections(self, *, account_id: str) -> int: ...

    async def start_run(self, request: SkillRunRequest) -> SkillRun: ...

    async def append_run_step(
        self,
        *,
        run_id: str,
        account_id: str,
        step_id: str,
        phase: SkillStepPhase,
        tool_name: str,
        arguments: Mapping[str, object],
        status: SkillStepStatus,
        output: object | None,
        error_code: str | None,
        started_at: datetime,
        completed_at: datetime,
    ) -> None: ...

    async def finish_run(
        self,
        *,
        run_id: str,
        account_id: str,
        status: Literal["succeeded", "failed"],
        rollback_status: SkillRollbackStatus,
        output: Mapping[str, object] | None,
        error_code: str | None,
        completed_at: datetime,
    ) -> SkillRun: ...

    async def get_run(self, *, account_id: str, run_id: str) -> SkillRun: ...


class SkillToolPort(Protocol):
    async def invoke(
        self,
        *,
        account_id: str,
        run_id: str,
        step_id: str,
        tool_name: str,
        arguments: Mapping[str, object],
        compensation: bool,
    ) -> object: ...


def validate_json_schema(
    schema: Mapping[str, object],
    *,
    require_object: bool = False,
) -> None:
    """Validate the small JSON Schema subset accepted by executable skills."""

    schema_type = schema.get("type")
    if not isinstance(schema_type, str) or schema_type not in {
        "object",
        "array",
        "string",
        "number",
        "integer",
        "boolean",
        "null",
    }:
        raise SkillSchemaValidationError("skill schema requires a supported type")
    if require_object and schema_type != "object":
        raise SkillSchemaValidationError("skill input/output schema must be an object")
    allowed_keys = {"type", "enum"}
    if schema_type == "object":
        allowed_keys.update({"properties", "required", "additionalProperties"})
    elif schema_type == "array":
        allowed_keys.add("items")
    elif schema_type == "string":
        allowed_keys.add("minLength")
    unknown_keys = set(schema) - allowed_keys
    if unknown_keys:
        raise SkillSchemaValidationError(
            f"unsupported schema fields: {sorted(str(key) for key in unknown_keys)}"
        )
    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            raise SkillSchemaValidationError("schema enum must be a non-empty array")
        _json_text(enum)
        if any(not _matches_json_type(schema_type, value) for value in enum):
            raise SkillSchemaValidationError("schema enum values must match its type")
    if schema_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        additional = schema.get("additionalProperties", False)
        if not isinstance(properties, dict):
            raise SkillSchemaValidationError("object schema properties must be an object")
        if not isinstance(required, list) or any(
            not isinstance(value, str) for value in required
        ):
            raise SkillSchemaValidationError("object schema required must be a string array")
        if not isinstance(additional, bool):
            raise SkillSchemaValidationError("additionalProperties must be boolean")
        unknown_required = set(cast_str_list(required)) - set(properties)
        if unknown_required:
            raise SkillSchemaValidationError(
                f"required properties are not declared: {sorted(unknown_required)}"
            )
        for child in properties.values():
            if not isinstance(child, dict):
                raise SkillSchemaValidationError("property schemas must be objects")
            validate_json_schema(child)
    if schema_type == "array":
        items = schema.get("items")
        if not isinstance(items, dict):
            raise SkillSchemaValidationError("array schema requires an items schema")
        validate_json_schema(items)
    if schema_type == "string":
        min_length = schema.get("minLength")
        if min_length is not None and (
            not isinstance(min_length, int)
            or isinstance(min_length, bool)
            or min_length < 0
        ):
            raise SkillSchemaValidationError(
                "string schema minLength must be a non-negative integer"
            )


def validate_json_instance(
    schema: Mapping[str, object],
    value: object,
    *,
    path: str = "$",
) -> None:
    schema_type = str(schema["type"])
    if not _matches_json_type(schema_type, value):
        raise SkillSchemaValidationError(f"{path} must be {schema_type}")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise SkillSchemaValidationError(f"{path} is not one of the allowed values")
    if schema_type == "object":
        assert isinstance(value, Mapping)
        properties_raw = schema.get("properties", {})
        assert isinstance(properties_raw, dict)
        required = set(cast_str_list(schema.get("required", [])))
        missing = required - set(value)
        if missing:
            raise SkillSchemaValidationError(f"{path} is missing {sorted(missing)}")
        additional = bool(schema.get("additionalProperties", False))
        unknown = set(value) - set(properties_raw)
        if unknown and not additional:
            raise SkillSchemaValidationError(f"{path} contains unknown fields {sorted(unknown)}")
        for key, child_value in value.items():
            child_schema = properties_raw.get(key)
            if isinstance(child_schema, dict):
                validate_json_instance(
                    child_schema,
                    child_value,
                    path=f"{path}.{key}",
                )
    elif schema_type == "array":
        assert isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        )
        items = schema["items"]
        assert isinstance(items, dict)
        for index, child in enumerate(value):
            validate_json_instance(items, child, path=f"{path}[{index}]")
    elif schema_type == "string":
        assert isinstance(value, str)
        min_length = schema.get("minLength")
        if min_length is not None and len(value) < int(str(min_length)):
            raise SkillSchemaValidationError(f"{path} is shorter than minLength")


def canonical_json(value: object) -> str:
    return _json_text(value)


def skill_input_sha256(inputs: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json(dict(inputs)).encode()).hexdigest()


def cast_str_list(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SkillSchemaValidationError("expected an array of strings")
    return [str(item) for item in value]


def _matches_json_type(schema_type: str, value: object) -> bool:
    if schema_type == "object":
        return isinstance(value, Mapping)
    if schema_type == "array":
        return isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        )
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    return value is None


def _json_text(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("skill definitions and values must be JSON serializable") from exc
