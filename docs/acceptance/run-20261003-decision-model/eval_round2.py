"""Round 2 (runs inside the bridge container): decision model with the original yes/no wording (v1) against a
category wording (v2: Choice with the 'mention vs use' and 'topic word vs live data' cases written into the options),
on the dev set AND a held-out set that was written before v2 and never used to tune it.  DeepSeek (production
classifier classes) is the reference.  Synthetic test sentences only."""
import asyncio
import json
import logging
import os
import random
import sys
import time

import httpx
from services.agent.src.providers.close_intent_semantic_classifier import (
    CloseIntentSemanticClassifier,
    CloseIntentSemanticClassifierConfig,
)
from services.agent.src.providers.live_lookup_semantic_classifier import (
    LiveLookupSemanticClassifier,
    LiveLookupSemanticClassifierConfig,
)

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
DEV = json.loads(r"""__DEV_JSON__""")
HELD = json.loads(r"""__HELD_JSON__""")
DS = dict(key=os.environ["DEEPSEEK_API_KEY"].strip(), url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
          model=os.environ.get("DEEPSEEK_FAST_MODEL", "deepseek-flash"), thinking="deepseek")
DM_URL = os.environ["DECISION_MODEL_URL"]  # the account-specific .../compatible-mode/v1/systemone endpoint
DM_KEY = os.environ["DASHSCOPE_API_KEY"].strip()
TIMEOUT_S = 10.0

V1 = {
    "needs_live_lookup": {
        "type": "noul",
        "instructions": "说话人这句话是否在请求需要最新的、会变化的或依赖当前时间地点的公开信息才能回答的内容？"
                        "例如天气、新闻、股价、汇率、航班/火车/动车时刻、路况、政策、赛事比分等。",
        "criteria": {"true": "说话人想知道现在或近期才会变化的实时信息，必须联网查询才能可靠回答",
                     "false": "闲聊、故事、常识、观点、回忆、固定知识，或者只是谈到天气、股票、新闻等话题但不需要实时信息就能回答"},
    },
    "ends_conversation": {
        "type": "noul",
        "instructions": "说话人这句话是否在要求结束与语音助手的对话并让助手进入待命休息？例如明确告别、让助手退下、先不聊了、不用陪了、可以休息了。",
        "criteria": {"true": "说话人明确想结束这次对话",
                     "false": "普通聊天、提问、讲故事、继续当前话题，或只是在讨论或引用「再见/拜拜」这个词、替别人说再见、让助手留下继续陪"},
    },
}
V2 = {
    "lookup_type": {
        "type": "choice",
        "instructions": "根据说话人这句话本身的意图，判断它想得到哪一类信息。注意：句子里出现天气、新闻、股票、汇率、交通等词，"
                        "不代表需要最新数据；只有明确想知道现在或近期才变化的具体情况，才算需要实时信息。",
        "criteria": {
            "realtime": "想知道此刻或近期才有的最新具体情况，必须联网查询才能回答，例如今明天的天气气温、最新新闻与比分、"
                        "当前股价汇率、路况、车次航班时刻、近期活动与最新发布。",
            "knowledge": "询问原理、定义、概念、成因、历史或固定常识，答案不随时间变化，即使话题涉及天气、金融、新闻、交通等"
                         "也属于这一类，例如询问闪电的成因或基金的含义。",
            "chat": "打招呼、闲聊、讲故事、唱歌、猜谜、回忆、表达心情、请求陪伴，以及关于说话人自己或家人的个人问题。",
        },
    },
    "close_type": {
        "type": "choice",
        "instructions": "根据说话人这句话本身的意图，判断说话人是否自己想结束这次与助手的对话。"
                        "只是提到或询问告别用语、替别人告别、或还想继续聊，都不算结束。",
        "criteria": {
            "end_now": "说话人自己明确想结束对话：告别、让助手退下或休息、说先不聊了或不用陪了、因为要去做别的事而结束聊天。",
            "mention_only": "只是在提到、询问或谈论告别用语本身（含义、写法、怎么说、相关的歌曲或游戏），或转述别人的告别，"
                            "说话人并没有结束对话的意思。",
            "continue": "普通聊天、提问、讲故事、问候、换话题、请助手留下或继续陪伴，或只是表示知道了、让助手继续说。",
        },
    },
}


def emit(**row):
    print(json.dumps(row, ensure_ascii=False), flush=True)


def ms_since(t0):
    return (time.perf_counter() - t0) * 1000


async def ds_pair(text):
    c1, c2 = httpx.AsyncClient(), httpx.AsyncClient()
    live = LiveLookupSemanticClassifier(LiveLookupSemanticClassifierConfig(
        api_key=DS["key"], base_url=DS["url"], model=DS["model"], timeout_s=TIMEOUT_S, thinking_mode=DS["thinking"]), client=c1)
    close = CloseIntentSemanticClassifier(CloseIntentSemanticClassifierConfig(
        api_key=DS["key"], base_url=DS["url"], model=DS["model"], timeout_s=TIMEOUT_S, thinking_mode=DS["thinking"]), client=c2)
    t0 = time.perf_counter()
    v1, v2 = await asyncio.gather(live.classify(current_text=text), close.classify(current_text=text))
    wall = ms_since(t0)
    await c1.aclose()
    await c2.aclose()
    return dict(raw_live=str(v1.value), raw_close=str(v2.value), pair_ms=round(wall, 1))


async def dm(text, questions):
    async with httpx.AsyncClient() as c:
        t0 = time.perf_counter()
        try:
            r = await c.post(DM_URL, headers={"Authorization": DM_KEY, "Content-Type": "application/json"},
                             json={"model": "decision-model-preview", "state": text, "questions": questions}, timeout=TIMEOUT_S)
            ms = ms_since(t0)
            if r.status_code != 200:
                return dict(status=r.status_code, err=r.text[:160], pair_ms=round(ms, 1))
            return dict(status=200, ans=r.json().get("answers", {}), pair_ms=round(ms, 1))
        except Exception as e:  # noqa: BLE001
            return dict(status=-1, err=f"{type(e).__name__}: {e}"[:160], pair_ms=round(ms_since(t0), 1))


def probs_v1(res):
    try:
        return float(res["ans"]["needs_live_lookup"]["noul"]), float(res["ans"]["ends_conversation"]["noul"])
    except Exception:  # noqa: BLE001
        return None, None


def probs_v2(res):
    try:
        return (float(res["ans"]["lookup_type"]["probabilities"]["realtime"]),
                float(res["ans"]["close_type"]["probabilities"]["end_now"]),
                res["ans"]["lookup_type"]["probabilities"], res["ans"]["close_type"]["probabilities"])
    except Exception:  # noqa: BLE001
        return None, None, None, None


async def main():
    rnd = random.Random(7)
    work = [("dev", i, r) for i, r in enumerate(DEV)] + [("held", i, r) for i, r in enumerate(HELD)]
    rnd.shuffle(work)
    emit(kind="start", dev=len(DEV), held=len(HELD))
    for k, (name, i, row) in enumerate(work):
        for cfg in rnd.sample(["ds", "dm_v1", "dm_v2"], 3):
            if cfg == "ds":
                emit(kind="r", set=name, cfg=cfg, i=i, text=row["text"], a=row["a"], b=row["b"], hard=row["hard"], **await ds_pair(row["text"]))
            elif cfg == "dm_v1":
                res = await dm(row["text"], V1)
                pa, pb = probs_v1(res)
                emit(kind="r", set=name, cfg=cfg, i=i, text=row["text"], a=row["a"], b=row["b"], hard=row["hard"],
                     p_live=pa, p_close=pb, pair_ms=res["pair_ms"], status=res["status"], err=res.get("err"))
            else:
                res = await dm(row["text"], V2)
                pa, pb, la, lb = probs_v2(res)
                emit(kind="r", set=name, cfg=cfg, i=i, text=row["text"], a=row["a"], b=row["b"], hard=row["hard"],
                     p_live=pa, p_close=pb, probs_live=la, probs_close=lb, pair_ms=res["pair_ms"], status=res["status"], err=res.get("err"))
        if k % 40 == 0:
            print(f"{k}/{len(work)}", file=sys.stderr, flush=True)
    emit(kind="done")


asyncio.run(main())
