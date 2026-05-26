"""
Decide whether the agent should ask for clarification before running SQL.

Kept intentionally conservative: only triggers when a data-heavy query
genuinely lacks a time range. Location is rarely required since most queries
default to "all intersections."
"""
from __future__ import annotations

import re

# Patterns that indicate a time range is already present
_TIME_PATTERNS = [
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}",       # ISO date 2025-05-01
    r"\d{1,2}[-/]\d{1,2}",                  # M/D shorthand
    r"今天|本週|本月|上週|上個月|昨天|本季|本年",
    r"最近\s*\d+\s*[天日週個月年]",
    r"過去\s*\d+\s*[天日週個月年]",
    r"這\s*[週月季年]",
    r"\d+\s*[天日週個月年]",
    r"尖峰|離峰|週一|週二|週三|週四|週五|週六|週日",
    r"daily|weekly|monthly|today|yesterday|this week|last week",
]

# Keywords that imply a time-sensitive aggregation query
_DATA_QUERY_KEYWORDS = [
    "壅塞", "車流", "流量", "統計", "最高", "最多", "最少",
    "平均", "尖峰", "次數", "事件", "故障", "維護", "費用",
    "哪些路口", "哪個路口", "排名", "比較",
]

# Keywords that indicate the query is already scoped (no time needed)
_SCOPED_KEYWORDS = [
    "目前", "現在", "即時", "current", "currently", "now",
    "所有", "清單", "列表", "狀態",
]


def _has_time_context(text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in _TIME_PATTERNS)


def _scan_history(history: list[dict], n: int = 4) -> str:
    """Concatenate the last n assistant+user messages into a single string."""
    parts = []
    for msg in history[-n:]:
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
    return " ".join(parts)


def check(user_query: str, history: list[dict]) -> dict:
    """
    Return {"needs_clarification": bool, "questions": list[str]}.

    Only fires when:
    - The query contains data-aggregation keywords (壅塞, 車流, 統計, …)
    - AND there is no time context in the query or in the last 4 turns
    - AND the query is not already scoped to "current state" (目前, 現在, …)
    """
    questions: list[str] = []

    is_data_query = any(kw in user_query for kw in _DATA_QUERY_KEYWORDS)
    is_scoped = any(kw in user_query for kw in _SCOPED_KEYWORDS)

    if is_data_query and not is_scoped:
        # Check query itself, then recent history
        history_text = _scan_history(history)
        has_time = _has_time_context(user_query) or _has_time_context(history_text)

        if not has_time:
            questions.append(
                "請問要查哪個時間區間？\n"
                "1. 今天\n"
                "2. 本週（週一至今）\n"
                "3. 本月\n"
                "4. 指定日期範圍（請輸入，例如：5/20 ~ 5/26）"
            )

    return {
        "needs_clarification": len(questions) > 0,
        "questions": questions,
    }
