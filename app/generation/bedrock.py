"""Amazon Bedrock adapter (default model: Amazon Nova via the Converse API).

Uses boto3's bedrock-runtime Converse API, which is model-agnostic: the same
call shape works for Amazon Nova, Anthropic Claude, Meta Llama, etc. — pick
the model with BEDROCK_MODEL_ID. Requests are signed with standard AWS
credentials (env vars, ~/.aws, SSO, or an instance role).
All Bedrock-specific code stays inside this file.
"""

from __future__ import annotations

import re
from typing import Callable

from app.generation.base import GenerationError, GenerationProvider, GenerationResult

# Nova models sometimes emit visible <thinking>...</thinking> chain-of-thought
# as plain text (not a distinct content block) when tools are configured, even
# though the prompt tells it not to - stripped defensively rather than trusted
# to prompt compliance alone.
_THINKING_RE = re.compile(r"<thinking>.*?</thinking>\s*", re.DOTALL | re.IGNORECASE)


class BedrockProvider(GenerationProvider):
    name = "bedrock"

    def __init__(
        self,
        aws_region: str,
        model_id: str,
        aws_access_key_id: str = "",
        aws_secret_access_key: str = "",
    ):
        import boto3  # deferred so groq-only deployments never load AWS bits
        import botocore.exceptions

        self.model = model_id
        self._botocore_exceptions = botocore.exceptions
        # Explicit keys from config take precedence; empty strings fall back
        # to boto3's default resolution chain (env, ~/.aws, SSO, role).
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=aws_region,
            aws_access_key_id=aws_access_key_id or None,
            aws_secret_access_key=aws_secret_access_key or None,
        )

    def generate(self, system_prompt: str, user_prompt: str, max_tokens: int) -> GenerationResult:
        try:
            response = self._client.converse(
                modelId=self.model,
                system=[{"text": system_prompt}],
                messages=[{"role": "user", "content": [{"text": user_prompt}]}],
                inferenceConfig={"maxTokens": max_tokens},
            )
        except self._botocore_exceptions.ClientError as exc:
            error = exc.response.get("Error", {})
            raise GenerationError(
                f"Bedrock request failed ({error.get('Code', 'unknown')}): "
                f"{error.get('Message', exc)}"
            ) from exc
        except self._botocore_exceptions.BotoCoreError as exc:
            # Covers missing credentials, endpoint/connection failures, etc.
            raise GenerationError(f"Bedrock error: {exc}") from exc

        if response.get("stopReason") == "guardrail_intervened":
            raise GenerationError("Bedrock model declined to answer this request.")
        content = response.get("output", {}).get("message", {}).get("content", [])
        answer = "".join(block.get("text", "") for block in content).strip()
        if not answer:
            raise GenerationError("Bedrock returned an empty response.")
        return GenerationResult(answer=answer, provider=self.name, model=self.model)

    def generate_with_vision(
        self,
        system_prompt: str,
        user_prompt: str,
        media: list[tuple[str, str, bytes]],
        max_tokens: int,
        temperature: float | None = None,
    ) -> GenerationResult:
        """Answer a prompt that includes image/PDF input via the Converse API.

        `media` is a list of (kind, fmt, data) where kind is "image" or
        "document", fmt is the Bedrock format string ("jpeg", "png", "pdf",
        ...), and data is the raw bytes. Nova (Lite/Pro) accepts both image and
        document content blocks, so a scanned photo or a PDF page both work
        without a separate rasteriser.

        `temperature` is passed through when given; None leaves the model's own
        default (Nova samples at ~0.7). Callers that are READING a document
        rather than writing prose should pass 0.0 - otherwise the same page can
        extract differently on two runs, which is a genuine defect in an OCR
        step, not acceptable variation.

        Only implemented for Bedrock - callers duck-type on
        `hasattr(provider, "generate_with_vision")`, same convention as
        generate_with_tools (Groq has neither yet).
        """
        content: list[dict] = [{"text": user_prompt}]
        for kind, fmt, data in media:
            if kind == "image":
                content.append({"image": {"format": fmt, "source": {"bytes": data}}})
            elif kind == "document":
                content.append({
                    "document": {
                        "format": fmt,
                        # Converse requires a name; it is not interpreted.
                        "name": "invoice",
                        "source": {"bytes": data},
                    }
                })
            else:
                raise GenerationError(f"Unsupported media kind '{kind}' (use image/document).")

        inference: dict = {"maxTokens": max_tokens}
        if temperature is not None:
            inference["temperature"] = temperature
        try:
            response = self._client.converse(
                modelId=self.model,
                system=[{"text": system_prompt}],
                messages=[{"role": "user", "content": content}],
                inferenceConfig=inference,
            )
        except self._botocore_exceptions.ClientError as exc:
            error = exc.response.get("Error", {})
            raise GenerationError(
                f"Bedrock vision request failed ({error.get('Code', 'unknown')}): "
                f"{error.get('Message', exc)}"
            ) from exc
        except self._botocore_exceptions.BotoCoreError as exc:
            raise GenerationError(f"Bedrock error: {exc}") from exc

        if response.get("stopReason") == "guardrail_intervened":
            raise GenerationError("Bedrock model declined to process this document.")
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        answer = "".join(b.get("text", "") for b in blocks).strip()
        answer = _THINKING_RE.sub("", answer).strip()
        if not answer:
            raise GenerationError("Bedrock returned an empty response for the document.")
        return GenerationResult(answer=answer, provider=self.name, model=self.model)

    def generate_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict],
        max_tokens: int,
        execute_tool: Callable[[str, dict], dict],
        max_tool_rounds: int = 4,
    ) -> GenerationResult:
        """Converse API tool-use loop: the model may request a tool call
        instead of answering; we run it and feed the result back, up to
        max_tool_rounds times, until it produces a final text answer.

        Only implemented for Bedrock today - callers should check
        `hasattr(provider, "generate_with_tools")` rather than assume every
        GenerationProvider has it (Groq doesn't, yet).
        """
        messages: list[dict] = [{"role": "user", "content": [{"text": user_prompt}]}]
        tool_config = {"tools": tools}

        for _ in range(max_tool_rounds):
            try:
                response = self._client.converse(
                    modelId=self.model,
                    system=[{"text": system_prompt}],
                    messages=messages,
                    inferenceConfig={"maxTokens": max_tokens},
                    toolConfig=tool_config,
                )
            except self._botocore_exceptions.ClientError as exc:
                error = exc.response.get("Error", {})
                raise GenerationError(
                    f"Bedrock request failed ({error.get('Code', 'unknown')}): "
                    f"{error.get('Message', exc)}"
                ) from exc
            except self._botocore_exceptions.BotoCoreError as exc:
                raise GenerationError(f"Bedrock error: {exc}") from exc

            if response.get("stopReason") == "guardrail_intervened":
                raise GenerationError("Bedrock model declined to answer this request.")

            message = response.get("output", {}).get("message", {})
            content = message.get("content", [])
            messages.append({"role": "assistant", "content": content})

            if response.get("stopReason") != "tool_use":
                answer = "".join(block.get("text", "") for block in content).strip()
                answer = _THINKING_RE.sub("", answer).strip()
                if not answer:
                    raise GenerationError("Bedrock returned an empty response.")
                return GenerationResult(answer=answer, provider=self.name, model=self.model)

            tool_results = []
            for block in content:
                tool_use = block.get("toolUse")
                if not tool_use:
                    continue
                try:
                    result = execute_tool(tool_use["name"], tool_use.get("input") or {})
                except Exception as exc:  # the loop must survive a bad tool call
                    result = {"ok": False, "error": f"Tool execution failed: {exc}"}
                tool_results.append({
                    "toolResult": {
                        "toolUseId": tool_use["toolUseId"],
                        "content": [{"json": result}],
                    }
                })
            messages.append({"role": "user", "content": tool_results})

        raise GenerationError(
            "Bedrock did not produce a final answer within the tool-call round limit."
        )
