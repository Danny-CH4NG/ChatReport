"""
Unit tests for app/skills/clarification_checker.py

Verifies that the clarification gate fires only when genuinely needed.
No DB / Docker required.
"""
import pytest

from skills.clarification_checker import check

# ── Helper ────────────────────────────────────────────────────────────────────

def _user(text: str) -> dict:
    return {"role": "user", "content": text}

def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


# ── Should NOT ask for clarification ─────────────────────────────────────────

class TestNoClarificationNeeded:
    def test_query_with_explicit_date(self):
        result = check("查詢 2025-05-01 到 2025-05-07 的壅塞統計", [])
        assert result["needs_clarification"] is False

    def test_query_with_today(self):
        result = check("今天哪些路口壅塞？", [])
        assert result["needs_clarification"] is False

    def test_query_with_this_week(self):
        result = check("本週車流統計", [])
        assert result["needs_clarification"] is False

    def test_query_with_last_week(self):
        result = check("上週哪 5 個路口機車流量最高？", [])
        assert result["needs_clarification"] is False

    def test_query_with_last_month(self):
        result = check("上個月各設備類型的維護費用？", [])
        assert result["needs_clarification"] is False

    def test_query_with_past_n_months(self):
        result = check("過去 3 個月各設備類型的維護費用總計？", [])
        assert result["needs_clarification"] is False

    def test_query_with_recent_n_days(self):
        result = check("最近 7 天的事件統計", [])
        assert result["needs_clarification"] is False

    def test_query_scoped_with_目前(self):
        # 目前 = "currently" — scoped, no time range needed
        result = check("目前哪些設備在故障？按行政區分類", [])
        assert result["needs_clarification"] is False

    def test_query_scoped_with_現在(self):
        result = check("CMS 現在還在顯示哪些訊息？", [])
        assert result["needs_clarification"] is False

    def test_non_aggregation_query_device_list(self):
        # Listing device status — not time-sensitive aggregation
        result = check("顯示所有設備清單", [])
        assert result["needs_clarification"] is False

    def test_peak_hour_keyword_implies_time(self):
        result = check("早上尖峰時段哪些路口壅塞？", [])
        assert result["needs_clarification"] is False

    def test_weekday_keyword_implies_time(self):
        result = check("週一到週五哪些路口車流最高？", [])
        assert result["needs_clarification"] is False

    def test_history_contains_time_context(self):
        # User said "本週" in a prior turn → no need to ask again
        history = [
            _user("本週各路口的壅塞統計"),
            _assistant("以下是本週資料…"),
        ]
        result = check("哪些路口排名最高？", history)
        assert result["needs_clarification"] is False

    def test_month_shorthand_in_query(self):
        result = check("5/20 ~ 5/26 的車流統計", [])
        assert result["needs_clarification"] is False

    def test_english_time_keyword(self):
        result = check("show congestion stats for today", [])
        assert result["needs_clarification"] is False


# ── SHOULD ask for clarification ─────────────────────────────────────────────

class TestClarificationRequired:
    def test_congestion_without_time(self):
        result = check("哪些路口壅塞？", [])
        assert result["needs_clarification"] is True
        assert len(result["questions"]) > 0

    def test_traffic_flow_without_time(self):
        result = check("哪些路口車流最高？", [])
        assert result["needs_clarification"] is True

    def test_statistics_without_time(self):
        result = check("各路口的壅塞統計", [])
        assert result["needs_clarification"] is True

    def test_ranking_query_without_time(self):
        result = check("哪些路口排名最高？", [])
        assert result["needs_clarification"] is True

    def test_maintenance_cost_without_time(self):
        result = check("各設備類型的維護費用", [])
        assert result["needs_clarification"] is True

    def test_incident_count_without_time(self):
        result = check("中山路口有幾次事件？", [])
        assert result["needs_clarification"] is True

    def test_empty_history_does_not_help(self):
        result = check("哪些路口壅塞最嚴重？", [])
        assert result["needs_clarification"] is True


# ── Questions format ──────────────────────────────────────────────────────────

class TestClarificationQuestionFormat:
    def test_question_contains_time_options(self):
        result = check("哪些路口車流量最高？", [])
        assert result["needs_clarification"] is True
        q = result["questions"][0]
        # Should offer some options to the user
        assert "今天" in q or "本週" in q or "本月" in q

    def test_returns_list_of_strings(self):
        result = check("哪些路口壅塞？", [])
        assert isinstance(result["questions"], list)
        assert all(isinstance(q, str) for q in result["questions"])


# ── Multi-turn context inheritance ────────────────────────────────────────────

class TestMultiTurnContext:
    def test_time_in_third_to_last_turn_is_detected(self):
        history = [
            _user("上週哪些路口壅塞？"),
            _assistant("上週前 5 名如下…"),
            _user("那機車流量呢？"),
            _assistant("機車流量排名如下…"),
        ]
        result = check("比較這兩個排名", history)
        assert result["needs_clarification"] is False

    def test_no_time_in_recent_history_triggers_clarification(self):
        history = [
            _user("設備清單是什麼？"),
            _assistant("目前共有 200 台設備。"),
        ]
        # Follow-up query that IS data-aggregation without time
        result = check("哪些路口車流量最高？", history)
        assert result["needs_clarification"] is True
