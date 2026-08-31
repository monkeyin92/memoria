from services.agent.src.clock_fact_queries import is_clock_fact_query


def test_is_clock_fact_query_matches_weekday_and_time() -> None:
    assert is_clock_fact_query("今天星期几")
    assert is_clock_fact_query("现在几点了")
    assert not is_clock_fact_query("今天南京天气怎么样")
