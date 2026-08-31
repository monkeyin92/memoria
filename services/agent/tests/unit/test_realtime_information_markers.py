from services.agent.src.live_query_markers import requires_live_media_lookup


def test_requires_live_media_lookup_matches_train_queries() -> None:
    assert requires_live_media_lookup("查询明天从南京到上海最快的动车")
    assert requires_live_media_lookup("今天南京天气怎么样")
    assert not requires_live_media_lookup("讲个笑话")
