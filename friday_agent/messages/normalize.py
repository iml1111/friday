from __future__ import annotations

from friday_agent.messages.types import ContentBlock, Message


def normalize_for_api(messages: list[Message]) -> list[dict]:
    """Convert internal Message list to the LLM API wire format.

    Filtering rules:
    - synthetic messages (is_meta=True) are transcript-only and excluded
    - messages without a role (e.g. internal system messages) are excluded
    - messages with empty content are excluded (the API rejects them)

    Each surviving message is normalized to {"role": str, "content": list[dict]}.
    """
    result = []
    for msg in messages:
        if msg.is_meta:
            continue
        if not msg.role:
            continue
        if not msg.content:
            continue

        api_content = _convert_content_blocks(msg.content)
        if not api_content:
            continue

        result.append({"role": msg.role, "content": api_content})

    return result


def _convert_content_blocks(blocks: list[ContentBlock]) -> list[dict]:
    """Convert a list of ContentBlocks to the API wire format."""
    api_content = []
    for block in blocks:
        if block.type == "text":
            if block.text is not None:
                api_content.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            api_content.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
        elif block.type == "tool_result":
            entry: dict = {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": block.content,
            }
            if block.is_error:
                entry["is_error"] = True
            api_content.append(entry)
        elif block.type == "thinking":
            # thinking blocks must be echoed back verbatim, INCLUDING their signature;
            # omitting the signature makes the API reject the next turn (400).
            if block.text is not None:
                entry: dict = {"type": "thinking", "thinking": block.text}
                if block.signature:
                    entry["signature"] = block.signature
                api_content.append(entry)
    return api_content
