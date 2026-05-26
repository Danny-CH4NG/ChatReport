"""
Chainlit UI entry point — P3 implementation.

Features:
  - Schema discovery on session start
  - Clarification check before running SQL
  - Per-tool Chainlit Steps (list_tables / describe_table / execute_query …)
  - Streaming final response tokens via cl.Message.stream_token
  - Action Button for Excel export (handler calls excel_exporter, added in P4)
"""
import logging

import chainlit as cl
from agent import AgentResult, run_agent
from skills.clarification_checker import check as clarification_check
from skills.schema_discovery import build_schema_cache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_MAX_HISTORY = 20
_KEEP_RECENT = 10

_TOOL_LABELS: dict[str, str] = {
    "list_tables":    "📋 列出資料表",
    "describe_table": "🔍 取得資料表結構",
    "get_sample_rows": "📄 取得範例資料",
    "execute_query":  "⚡ 執行 SQL 查詢",
}


def _trim_history(history: list[dict]) -> list[dict]:
    """Keep first system message + newest _KEEP_RECENT turns."""
    if len(history) <= _MAX_HISTORY:
        return history
    return history[:1] + history[-_KEEP_RECENT:]


# ── Session start ─────────────────────────────────────────────────────────────

@cl.on_chat_start
async def on_start():
    await cl.Message(content="⏳ 正在載入資料庫 Schema，請稍候…").send()

    schema_cache = await build_schema_cache()
    cl.user_session.set("schema_cache", schema_cache)
    cl.user_session.set("message_history", [])
    cl.user_session.set("last_query_result", None)

    table_count = len(schema_cache)
    await cl.Message(
        content=f"✅ Schema 載入完成（{table_count} 張資料表）。\n\n"
                "您好！我是交通管理系統的資料分析助理。\n"
                "請用自然語言描述您想查詢的內容，例如：\n"
                "- 「今天哪個路口的 VD 壅塞最嚴重？」\n"
                "- 「目前哪些設備在故障？」\n"
                "- 「本月維護費用最高的設備類型？」"
    ).send()


# ── Message handler ───────────────────────────────────────────────────────────

@cl.on_message
async def on_message(message: cl.Message):
    history: list[dict]  = cl.user_session.get("message_history") or []
    schema_cache: dict   = cl.user_session.get("schema_cache") or {}

    # ── Clarification check ───────────────────────────────────────────────────
    clarification = clarification_check(message.content, history)
    if clarification["needs_clarification"]:
        questions_text = "\n\n".join(clarification["questions"])
        reply = f"💬 在查詢之前，我需要確認幾個細節：\n\n{questions_text}"
        await cl.Message(content=reply).send()
        history.append({"role": "user",      "content": message.content})
        history.append({"role": "assistant", "content": reply})
        cl.user_session.set("message_history", _trim_history(history))
        return

    history.append({"role": "user", "content": message.content})

    # ── Streaming response message (starts empty, fills with tokens) ──────────
    response_msg = cl.Message(content="")
    await response_msg.send()

    # ── Per-tool-call step tracking ───────────────────────────────────────────
    active_step: cl.Step | None = None

    async def on_tool_start(name: str, input_str: str) -> None:
        nonlocal active_step
        label = _TOOL_LABELS.get(name, name)
        active_step = cl.Step(name=label, type="tool")
        active_step.input = input_str
        await active_step.send()

    async def on_tool_done(name: str, output_str: str) -> None:
        nonlocal active_step
        if active_step:
            active_step.output = output_str
            await active_step.update()
            active_step = None

    async def on_token(token: str) -> None:
        await response_msg.stream_token(token)

    # ── Run agent ─────────────────────────────────────────────────────────────
    result: AgentResult = await run_agent(
        history,
        schema_cache,
        on_token=on_token,
        on_tool_start=on_tool_start,
        on_tool_done=on_tool_done,
    )

    # Finalise the streaming message.
    # Edge case: agent returned a non-streamed fallback (e.g. max-rounds error).
    if not response_msg.content and result.text:
        response_msg.content = result.text
    await response_msg.update()

    history.append({"role": "assistant", "content": result.text})
    cl.user_session.set("message_history", _trim_history(history))

    # ── Show export button if there is query data ─────────────────────────────
    if result.query_result and result.query_result.get("row_count", 0) > 0:
        cl.user_session.set("last_query_result", result.query_result)
        await _show_export_button()


# ── Excel export Action Button ────────────────────────────────────────────────

async def _show_export_button() -> None:
    actions = [
        cl.Action(
            name="export_excel",
            payload={"action": "export_excel"},
            label="📥 匯出完整 Excel",
            description="將完整查詢結果匯出為 .xlsx 檔案（含統計摘要與 SQL 紀錄）",
        )
    ]
    await cl.Message(
        content="查詢完成。如需匯出完整資料（含統計摘要與 SQL 紀錄）：",
        actions=actions,
    ).send()


@cl.action_callback("export_excel")
async def on_export(action: cl.Action):
    query_result = cl.user_session.get("last_query_result")
    if not query_result:
        await cl.Message(content="⚠️ 尚無可匯出的查詢結果，請先執行一次查詢。").send()
        return

    row_count = query_result.get("row_count", 0)

    # P4 will replace this block with the real excel_exporter call
    try:
        from skills.excel_exporter import generate as excel_generate  # noqa: PLC0415
        history: list[dict] = cl.user_session.get("message_history") or []

        async with cl.Step(name="📊 產生 Excel 報表") as step:
            filepath = await excel_generate(query_result, history)
            step.output = f"已生成：{filepath}（{row_count} 筆資料）"

        elements = [
            cl.File(
                name="查詢報表.xlsx",
                path=filepath,
                display="inline",
            )
        ]
        await cl.Message(
            content=f"✅ Excel 報表已準備完成（共 {row_count} 筆）：",
            elements=elements,
        ).send()

    except ImportError:
        # excel_exporter not yet implemented (P4)
        await cl.Message(
            content=f"📊 Excel 匯出功能將於 P4 階段實作。\n目前查詢結果共 **{row_count}** 筆資料。"
        ).send()
