"""Sequential execution and reverse compensation for approved procedural skills."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from services.archive.skill_domain import (
    SkillCatalogPort,
    SkillExecutionError,
    SkillRollbackStatus,
    SkillRun,
    SkillRunRequest,
    SkillStepDefinition,
    SkillToolPort,
    validate_json_instance,
)


class SkillExecutor:
    def __init__(self, *, catalog: SkillCatalogPort, tools: SkillToolPort) -> None:
        self._catalog = catalog
        self._tools = tools

    async def execute(self, request: SkillRunRequest) -> SkillRun:
        version = await self._catalog.get_version(
            account_id=request.account_id,
            skill_id=request.skill_id,
            version=request.version,
        )
        run = await self._catalog.start_run(request)
        outputs: dict[str, object] = {}
        completed: list[SkillStepDefinition] = []
        try:
            for step in version.steps:
                arguments = _mapping(
                    _resolve_template(
                        step.arguments,
                        inputs=request.inputs,
                        outputs=outputs,
                    )
                )
                started_at = datetime.now(UTC)
                try:
                    output = await self._tools.invoke(
                        account_id=request.account_id,
                        run_id=run.run_id,
                        step_id=step.step_id,
                        tool_name=step.tool_name,
                        arguments=arguments,
                        compensation=False,
                    )
                except Exception as exc:
                    completed_at = datetime.now(UTC)
                    await self._catalog.append_run_step(
                        run_id=run.run_id,
                        account_id=request.account_id,
                        step_id=step.step_id,
                        phase="forward",
                        tool_name=step.tool_name,
                        arguments=arguments,
                        status="failed",
                        output=None,
                        error_code=type(exc).__name__,
                        started_at=started_at,
                        completed_at=completed_at,
                    )
                    raise
                outputs[step.step_id] = output
                completed.append(step)
                completed_at = datetime.now(UTC)
                await self._catalog.append_run_step(
                    run_id=run.run_id,
                    account_id=request.account_id,
                    step_id=step.step_id,
                    phase="forward",
                    tool_name=step.tool_name,
                    arguments=arguments,
                    status="succeeded",
                    output=output,
                    error_code=None,
                    started_at=started_at,
                    completed_at=completed_at,
                )
            rendered = _mapping(
                _resolve_template(
                    version.output_template,
                    inputs=request.inputs,
                    outputs=outputs,
                )
            )
            validate_json_instance(version.output_schema, rendered)
        except Exception as exc:
            rollback = await self._rollback(
                request=request,
                run_id=run.run_id,
                completed=completed,
                outputs=outputs,
            )
            await self._catalog.finish_run(
                run_id=run.run_id,
                account_id=request.account_id,
                status="failed",
                rollback_status=rollback,
                output=None,
                error_code=type(exc).__name__,
                completed_at=datetime.now(UTC),
            )
            raise SkillExecutionError(run.run_id, f"skill execution failed: {type(exc).__name__}") from exc
        return await self._catalog.finish_run(
            run_id=run.run_id,
            account_id=request.account_id,
            status="succeeded",
            rollback_status="not_required",
            output=rendered,
            error_code=None,
            completed_at=datetime.now(UTC),
        )

    async def _rollback(
        self,
        *,
        request: SkillRunRequest,
        run_id: str,
        completed: list[SkillStepDefinition],
        outputs: Mapping[str, object],
    ) -> SkillRollbackStatus:
        if not completed:
            return "not_required"
        complete = True
        compensated = 0
        for step in reversed(completed):
            if (
                step.compensation_tool_name is None
                or step.compensation_arguments is None
            ):
                complete = False
                continue
            compensated += 1
            started_at = datetime.now(UTC)
            arguments: Mapping[str, object] = {}
            try:
                arguments = _mapping(
                    _resolve_template(
                        step.compensation_arguments,
                        inputs=request.inputs,
                        outputs=outputs,
                    )
                )
                output = await self._tools.invoke(
                    account_id=request.account_id,
                    run_id=run_id,
                    step_id=step.step_id,
                    tool_name=step.compensation_tool_name,
                    arguments=arguments,
                    compensation=True,
                )
            except Exception as exc:
                complete = False
                await self._catalog.append_run_step(
                    run_id=run_id,
                    account_id=request.account_id,
                    step_id=step.step_id,
                    phase="compensation",
                    tool_name=step.compensation_tool_name,
                    arguments=arguments,
                    status="failed",
                    output=None,
                    error_code=type(exc).__name__,
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                )
                continue
            await self._catalog.append_run_step(
                run_id=run_id,
                account_id=request.account_id,
                step_id=step.step_id,
                phase="compensation",
                tool_name=step.compensation_tool_name,
                arguments=arguments,
                status="succeeded",
                output=output,
                error_code=None,
                started_at=started_at,
                completed_at=datetime.now(UTC),
            )
        if not compensated:
            return "partial"
        return "completed" if complete else "partial"


def _resolve_template(
    value: object,
    *,
    inputs: Mapping[str, object],
    outputs: Mapping[str, object],
) -> object:
    if isinstance(value, str) and value.startswith("$"):
        return _resolve_reference(value, inputs=inputs, outputs=outputs)
    if isinstance(value, Mapping):
        return {
            str(key): _resolve_template(child, inputs=inputs, outputs=outputs)
            for key, child in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _resolve_template(child, inputs=inputs, outputs=outputs)
            for child in value
        ]
    return value


def _resolve_reference(
    reference: str,
    *,
    inputs: Mapping[str, object],
    outputs: Mapping[str, object],
) -> object:
    if reference == "$input":
        return dict(inputs)
    if reference.startswith("$input."):
        return _walk(inputs, reference.removeprefix("$input.").split("."), reference)
    if reference.startswith("$steps."):
        parts = reference.removeprefix("$steps.").split(".")
        if len(parts) < 2 or parts[1] != "output":
            raise ValueError(f"invalid skill output reference: {reference}")
        try:
            current = outputs[parts[0]]
        except KeyError as exc:
            raise ValueError(f"skill step output is unavailable: {reference}") from exc
        return _walk(current, parts[2:], reference)
    raise ValueError(f"unsupported skill template reference: {reference}")


def _walk(value: object, path: Sequence[str], reference: str) -> object:
    current = value
    for part in path:
        if isinstance(current, Mapping):
            if part not in current:
                raise ValueError(f"skill template reference is missing: {reference}")
            current = current[part]
            continue
        if isinstance(current, Sequence) and not isinstance(
            current,
            (str, bytes, bytearray),
        ):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise ValueError(f"skill template index is invalid: {reference}") from exc
            continue
        raise ValueError(f"skill template reference is not traversable: {reference}")
    return current


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("skill arguments and rendered output must be objects")
    return {str(key): child for key, child in value.items()}
