"""GenAI client spans for one logical model call.

Field names follow the OpenTelemetry GenAI semantic conventions at commit
cb10b70 (Development). Gate code reads the normalized trial, not these keys,
so a later rename stays in this module.
"""

from __future__ import annotations

from typing import Any, Mapping

from curbgate.desk import POLICY_SENTENCE
from curbgate.models import RunManifest


def _text_part(content: str) -> dict[str, str]:
    return {"type": "text", "content": content}


def chat_span(
    manifest: RunManifest,
    case_id: str,
    seed: int,
    invoked: Mapping[str, Any],
    user_text: str,
    output_type: str,
    include_messages: bool,
) -> dict[str, Any]:
    span: dict[str, Any] = {
        "name": f"chat {manifest.request_model}",
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": manifest.provider_name,
        "gen_ai.request.model": manifest.request_model,
        "gen_ai.prompt.name": manifest.prompt_name,
        "gen_ai.prompt.version": manifest.prompt_version,
        "gen_ai.output.type": output_type,
        "gen_ai.request.temperature": manifest.temperature,
        "gen_ai.request.top_p": manifest.top_p,
        "gen_ai.request.max_tokens": manifest.max_tokens,
        "gen_ai.request.seed": seed,
        "curbgate.case_id": case_id,
        "curbgate.retry_count": invoked["attempts"],
        "curbgate.retry_backoff_seconds": list(invoked["backoffs"]),
    }
    if include_messages:
        span["gen_ai.system_instructions"] = [_text_part(POLICY_SENTENCE)]
        span["gen_ai.input.messages"] = [
            {"role": "user", "parts": [_text_part(user_text)]}
        ]
    if invoked["ok"]:
        spec = invoked["spec"]
        span["gen_ai.response.model"] = spec.get("response_model") or manifest.response_model
        content = _produced_text(spec)
        if content is not None:
            span["gen_ai.output.messages"] = [
                {"role": "assistant", "parts": [_text_part(content)]}
            ]
        if spec.get("input_tokens") is not None:
            span["gen_ai.usage.input_tokens"] = int(spec["input_tokens"])
            span["gen_ai.usage.output_tokens"] = int(spec.get("output_tokens") or 0)
        span["gen_ai.response.finish_reasons"] = [spec.get("finish_reason") or "stop"]
    else:
        span["error.type"] = invoked["error_type"]
        partial = invoked.get("partial")
        if partial:
            span["gen_ai.output.messages"] = [
                {"role": "assistant", "parts": [_text_part(partial)]}
            ]
            span["gen_ai.response.finish_reasons"] = ["error"]
    return span


def tool_span(case_id: str, name: str, arguments: Mapping[str, Any], allowed: bool, executed: bool, reason: str) -> dict[str, Any]:
    return {
        "name": "execute_tool",
        "gen_ai.operation.name": "execute_tool",
        "curbgate.case_id": case_id,
        "gen_ai.tool.name": name,
        "curbgate.tool.arguments": dict(arguments),
        "curbgate.tool.blocked": not allowed,
        "curbgate.tool.executed": executed,
        "curbgate.tool.reason": reason,
    }


def _produced_text(spec: Mapping[str, Any]) -> str | None:
    if spec.get("raw") is not None:
        return str(spec["raw"])
    turns = spec.get("turns") or []
    parts = []
    for turn in turns:
        if turn.get("text"):
            parts.append(str(turn["text"]))
    if not parts and not turns:
        return None
    return "\n".join(parts) if parts else None
