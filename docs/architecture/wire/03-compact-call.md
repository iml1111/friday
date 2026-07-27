# CALL 3 — engine.compact() · 요약 호출

컨텍스트 오버플로 복구용 요약 호출. system·tools 모두 턴 루프와 다르다.

> 아래는 `client.messages.create(**params)`에 실제로 들어가는 값 그대로다.
> API 직렬화 순서(`tools` → `system` → `messages`)로 편다.

## 호출 파라미터

| key | value |
|---|---|
| `model` | `'claude-sonnet-4-6'` |
| `max_tokens` | `20000` |
| `stream` | `False` |
| `tools` | *(필드 없음 — 빈 리스트는 통째로 생략)* |
| `system` | 블록 1개 |
| `messages` | 메시지 6개 |

## `tools`

*이 호출에는 `tools` 필드가 없다.*

## `system`

### 블록 0 — 88 chars  ·  `cache_control={"type": "ephemeral"}`

```text
You are a conversation summarizer. Follow the instructions in the final message exactly.
```

## `messages`

### `messages[0]` — role: `user` · 블록 1개

**[0] text**

```text
이 문서 두 개 처리해줘
```

### `messages[1]` — role: `assistant` · 블록 4개

**[0] text**

```text
두 건을 처리한다.
```

**[1] tool_use** · `name=TodoWrite` · `id=toolu_01`

```json
{
  "todos": [
    {
      "content": "입력 텍스트 2건 처리",
      "status": "in_progress"
    },
    {
      "content": "처리 결과 보고",
      "status": "pending"
    }
  ]
}
```

**[2] tool_use** · `name=ExampleTool` · `id=toolu_02`

```json
{
  "payload": "first document",
  "mutating": false
}
```

**[3] tool_use** · `name=ExampleTool` · `id=toolu_03`

```json
{
  "payload": "second document",
  "mutating": true
}
```

### `messages[2]` — role: `user` · 블록 1개

**[0] tool_result** · `tool_use_id=toolu_01`

```text
Updated todo list (2 items).
```

### `messages[3]` — role: `user` · 블록 1개

**[0] tool_result** · `tool_use_id=toolu_02`

```text
processed: first document
```

### `messages[4]` — role: `user` · 블록 1개 *(= `messages[-2]`)*

**[0] tool_result** · `tool_use_id=toolu_03`  ·  `cache_control={"type": "ephemeral"}`

```text
processed: second document
```

### `messages[5]` — role: `user` · content: 문자열 2,710 chars *(= `messages[-1]`)*

```text
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools. Do NOT ask follow-up questions.
- You already have all the context you need in the conversation above.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions. The summary must be thorough in capturing the key information, decisions, and context essential for continuing the work without losing context.

Before your final summary, wrap your analysis in <analysis> tags. In your analysis, go through the conversation chronologically and, for each part, identify: the user's explicit requests and intents; your approach to addressing them; key decisions and concepts; specific details (names, exact quotes, relevant excerpts, parameters); errors you ran into and how you fixed them; and any specific user feedback — especially where the user told you to do something differently. Then double-check for accuracy and completeness.

Your summary should include the following sections:
1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail.
2. Key Technical Concepts: List all important concepts, technologies, and techniques discussed.
3. Files and Code Sections: Enumerate specific files, resources, or artifacts examined, modified, or created. Include relevant excerpts and a note on why each matters.
4. Errors and fixes: List all errors you ran into and how you fixed them, including any user feedback.
5. Problem Solving: Document problems solved and any ongoing troubleshooting.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding feedback and changing intent.
7. Pending Tasks: Outline any pending tasks you have explicitly been asked to work on.
8. Current Work: Describe precisely what was being worked on immediately before this summary request.
9. Optional Next Step: List the next step that is directly in line with the user's most recent explicit request and the work in progress. Include direct quotes to avoid drift. Do not start tangential or already-completed work without confirming first.

Domain-specific requirements for this summary (these take precedence over the generic sections above):
- Preserve every ExampleTool payload that was processed, verbatim.
- For section 3, list the payloads and their processed results instead of code excerpts.

Output format:
<analysis>your analysis (will be stripped)</analysis>
<summary>your summary here</summary>

REMINDER: Respond with plain text only — an <analysis> block followed by a <summary> block.
```

---

<details>
<summary>RAW — messages.create()에 넘어가는 dict 전체 (JSON)</summary>

```json
{
  "model": "claude-sonnet-4-6",
  "max_tokens": 20000,
  "system": [
    {
      "type": "text",
      "text": "You are a conversation summarizer. Follow the instructions in the final message exactly.",
      "cache_control": {
        "type": "ephemeral"
      }
    }
  ],
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "이 문서 두 개 처리해줘"
        }
      ]
    },
    {
      "role": "assistant",
      "content": [
        {
          "type": "text",
          "text": "두 건을 처리한다."
        },
        {
          "type": "tool_use",
          "id": "toolu_01",
          "name": "TodoWrite",
          "input": {
            "todos": [
              {
                "content": "입력 텍스트 2건 처리",
                "status": "in_progress"
              },
              {
                "content": "처리 결과 보고",
                "status": "pending"
              }
            ]
          }
        },
        {
          "type": "tool_use",
          "id": "toolu_02",
          "name": "ExampleTool",
          "input": {
            "payload": "first document",
            "mutating": false
          }
        },
        {
          "type": "tool_use",
          "id": "toolu_03",
          "name": "ExampleTool",
          "input": {
            "payload": "second document",
            "mutating": true
          }
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "tool_result",
          "tool_use_id": "toolu_01",
          "content": "Updated todo list (2 items)."
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "tool_result",
          "tool_use_id": "toolu_02",
          "content": "processed: first document"
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "tool_result",
          "tool_use_id": "toolu_03",
          "content": "processed: second document",
          "cache_control": {
            "type": "ephemeral"
          }
        }
      ]
    },
    {
      "role": "user",
      "content": "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools. Do NOT ask follow-up questions.\n- You already have all the context you need in the conversation above.\n- Your entire response must be plain text: an <analysis> block followed by a <summary> block.\n\nYour task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions. The summary must be thorough in capturing the key information, decisions, and context essential for continuing the work without losing context.\n\nBefore your final summary, wrap your analysis in <analysis> tags. In your analysis, go through the conversation chronologically and, for each part, identify: the user's explicit requests and intents; your approach to addressing them; key decisions and concepts; specific details (names, exact quotes, relevant excerpts, parameters); errors you ran into and how you fixed them; and any specific user feedback — especially where the user told you to do something differently. Then double-check for accuracy and completeness.\n\nYour summary should include the following sections:\n1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail.\n2. Key Technical Concepts: List all important concepts, technologies, and techniques discussed.\n3. Files and Code Sections: Enumerate specific files, resources, or artifacts examined, modified, or created. Include relevant excerpts and a note on why each matters.\n4. Errors and fixes: List all errors you ran into and how you fixed them, including any user feedback.\n5. Problem Solving: Document problems solved and any ongoing troubleshooting.\n6. All user messages: List ALL user messages that are not tool results. These are critical for understanding feedback and changing intent.\n7. Pending Tasks: Outline any pending tasks you have explicitly been asked to work on.\n8. Current Work: Describe precisely what was being worked on immediately before this summary request.\n9. Optional Next Step: List the next step that is directly in line with the user's most recent explicit request and the work in progress. Include direct quotes to avoid drift. Do not start tangential or already-completed work without confirming first.\n\nDomain-specific requirements for this summary (these take precedence over the generic sections above):\n- Preserve every ExampleTool payload that was processed, verbatim.\n- For section 3, list the payloads and their processed results instead of code excerpts.\n\nOutput format:\n<analysis>your analysis (will be stripped)</analysis>\n<summary>your summary here</summary>\n\nREMINDER: Respond with plain text only — an <analysis> block followed by a <summary> block."
    }
  ],
  "stream": false
}
```

</details>
