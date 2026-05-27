"""
Claude Tool Use loop — P2/P3 Agent core.

Receives full message history + schema cache, connects to MCP server via SSE,
runs the ReAct tool-call loop, and returns an AgentResult.

P3 additions:
  - on_token callback: streams final-response text tokens to the UI
  - on_tool_start / on_tool_done callbacks: drive per-tool Chainlit Steps
"""
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import anthropic
import yaml
from mcp import ClientSession
from mcp.client.sse import sse_client
from skills.nl_to_sql import build_dialect_section, get_db_dialect

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-5"
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8080")
_SCHEMA_CONTEXT_PATH = os.getenv("SCHEMA_CONTEXT_PATH", "/app/schema_context.yaml")
_MAX_TOOL_ROUNDS = 20

# Callback type aliases
OnToken    = Callable[[str], Awaitable[None]]
OnToolCall = Callable[[str, str], Awaitable[None]]


@dataclass
class AgentResult:
    text: str
    query_result: dict | None = None
    tool_calls: list[dict] = field(default_factory=list)


# ── System prompt ─────────────────────────────────────────────────────────────

def _load_schema_context() -> dict:
    if os.path.exists(_SCHEMA_CONTEXT_PATH):
        with open(_SCHEMA_CONTEXT_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def build_system_prompt(schema_cache: dict) -> str:
    ctx = _load_schema_context()

    vocab = ctx.get("database", {}).get("domain_vocabulary", {})
    vocab_section = ""
    if vocab:
        lines = "\n".join(f"  - {k}: {v}" for k, v in vocab.items())
        vocab_section = f"\n## 業務術語對照\n{lines}"

    examples = ctx.get("query_examples", [])
    examples_section = ""
    if examples:
        lines = "\n".join(
            f"  - 「{e['nl']}」 → {e['sql_hint']}"
            for e in examples
        )
        examples_section = f"\n## 常見查詢範例（SQL 提示）\n{lines}"

    schema_section = ""
    if schema_cache:
        parts = []
        for tname, tinfo in schema_cache.items():
            alias    = tinfo.get("alias", "")
            desc     = tinfo.get("description", "")
            join_pat = tinfo.get("join_pattern", "")
            tips     = tinfo.get("query_tips") or tinfo.get("common_patterns") or []
            cols     = ", ".join(
                f"{c['name']}({c['type']})"
                for c in tinfo.get("columns", [])
            )
            header = f"  - **{tname}**" + (f"（{alias}）" if alias else "")
            body = []
            if desc:
                body.append(f"    說明：{desc}")
            if join_pat:
                body.append(f"    JOIN 模式：{join_pat}")
            if tips:
                body.append("    常用模式：" + " / ".join(tips))
            body.append(f"    欄位：{cols}")
            parts.append(header + "\n" + "\n".join(body))
        schema_section = "\n## 資料表清單\n" + "\n".join(parts)

    dialect = get_db_dialect(schema_cache)
    dialect_label = "Vertica" if dialect == "vertica" else "PostgreSQL"
    dialect_section = build_dialect_section(schema_cache)

    return f"""你是台北市交通管理系統的資料分析助理，使用繁體中文回應。
{schema_section}{vocab_section}
{examples_section}

## 對話規則
- 記住本次對話的所有查詢與結果
- 使用者可用「剛才那個」「同樣的路口」等指代，從歷史推斷
- 若問題是前次查詢的延伸，繼承前次的過濾條件

## 補問規則
- 問題不明確時，先列出 1-3 個具體問題（用 💬 開頭）再執行 SQL
- 常見需補問情境：
    • 缺少時間範圍 → 詢問「請問要查哪個時間區間？例如今天、本週、或指定日期」
    • 缺少路口名稱 → 詢問「請問是哪個路口？或要查全市嗎？」
    • 查詢目的模糊 → 詢問「您想了解的是車流量、事件數，還是設備狀態？」

## 查詢規則
- 只能 SELECT，不能修改資料
- 使用 {dialect_label} 語法
- 查詢特定設備類型：從 devices WHERE device_type='...' 開始再 JOIN 子表
{dialect_section}
## 回覆格式
1. 說明此次查詢的邏輯與範圍
2. Markdown 表格呈現重點數據（最多 20 列，超過說明總筆數）
3. 若有查詢結果，最後一行顯示：「📥 如需完整資料，請點選上方『匯出 Excel』按鈕」""".strip()


# ── MCP helpers ───────────────────────────────────────────────────────────────

def _to_claude_tools(mcp_tools) -> list[dict]:
    result = []
    for t in mcp_tools:
        schema = t.inputSchema or {"type": "object", "properties": {}, "required": []}
        result.append({
            "name": t.name,
            "description": t.description or "",
            "input_schema": schema,
        })
    return result


def _extract_text(content_blocks) -> str:
    return "\n".join(b.text for b in content_blocks if hasattr(b, "text"))


def _mcp_text(mcp_result) -> str:
    content = mcp_result.content
    if content and hasattr(content[0], "text"):
        return content[0].text
    return "{}"


# ── Main agent loop ───────────────────────────────────────────────────────────

async def run_agent(
    history: list[dict],
    schema_cache: dict,
    on_token: OnToken | None = None,
    on_tool_start: OnToolCall | None = None,
    on_tool_done: OnToolCall | None = None,
) -> AgentResult:
    """
    Run the Claude Tool Use loop with optional streaming callbacks.

    Args:
        history:       Full message history [{role, content}]
        schema_cache:  Built by skills/schema_discovery.py on session start
        on_token:      Called with each text token of the final response
        on_tool_start: Called with (tool_name, input_json_str) before each MCP call
        on_tool_done:  Called with (tool_name, output_snippet) after each MCP call

    Returns:
        AgentResult with final text, last query result, and tool call log
    """
    client = anthropic.AsyncAnthropic()
    system = build_system_prompt(schema_cache)

    async with sse_client(f"{MCP_SERVER_URL}/sse") as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()

            tools_resp   = await session.list_tools()
            claude_tools = _to_claude_tools(tools_resp.tools)

            messages           = [{"role": m["role"], "content": m["content"]} for m in history]
            last_query_result: dict | None = None
            all_tool_calls:    list[dict]  = []

            for _ in range(_MAX_TOOL_ROUNDS):
                # ── Stream the API response ───────────────────────────────────
                # Text tokens are emitted live via on_token.
                # If the response ends with tool_use we buffer and loop;
                # if end_turn we return with the streamed text.
                streamed_text: list[str] = []

                async with client.messages.stream(
                    model=MODEL,
                    max_tokens=4096,
                    system=system,
                    tools=claude_tools,
                    messages=messages,
                ) as stream:
                    async for event in stream:
                        etype = getattr(event, "type", None)

                        if etype == "content_block_delta":
                            delta = event.delta
                            if getattr(delta, "type", None) == "text_delta":
                                token = delta.text
                                streamed_text.append(token)
                                if on_token:
                                    await on_token(token)

                    final_msg = await stream.get_final_message()

                # ── end_turn → return ─────────────────────────────────────────
                if final_msg.stop_reason == "end_turn":
                    text = "".join(streamed_text) or _extract_text(final_msg.content)
                    return AgentResult(
                        text=text,
                        query_result=last_query_result,
                        tool_calls=all_tool_calls,
                    )

                # ── tool_use → execute tools and loop ─────────────────────────
                tool_blocks  = [b for b in final_msg.content if b.type == "tool_use"]
                tool_results = []

                for block in tool_blocks:
                    input_str = json.dumps(block.input, ensure_ascii=False)[:300]
                    logger.info("→ %s(%s)", block.name, input_str)
                    all_tool_calls.append({"tool": block.name, "input": block.input})

                    if on_tool_start:
                        await on_tool_start(block.name, input_str)

                    try:
                        mcp_result = await session.call_tool(block.name, block.input)
                        raw = _mcp_text(mcp_result)
                        if block.name == "execute_query":
                            try:
                                last_query_result = json.loads(raw)
                                last_query_result["sql"] = block.input.get("sql", "")
                            except json.JSONDecodeError:
                                pass
                        output = raw
                    except Exception as exc:
                        logger.error("Tool %s error: %s", block.name, exc)
                        output = json.dumps({"error": str(exc)})

                    if on_tool_done:
                        await on_tool_done(block.name, output[:400])

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    })

                messages.append({
                    "role": "assistant",
                    "content": [
                        {k: v for k, v in b.model_dump().items() if k != "parsed_output"}
                        for b in final_msg.content
                    ],
                })
                messages.append({"role": "user", "content": tool_results})

    return AgentResult(
        text="⚠️ 已達到最大工具呼叫輪次上限，請嘗試更具體的問題。",
        query_result=last_query_result,
        tool_calls=all_tool_calls,
    )
