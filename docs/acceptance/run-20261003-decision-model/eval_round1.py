"""Server-side A/B for the two per-turn semantic classifiers (runs INSIDE the bridge container: keys never leave it).

Baselines reuse the production classifier classes unchanged (same prompts, parsing, request body):
  ds = DeepSeek (production now), qw = Qwen flash via DashScope (before the DeepSeek switch).
Candidate: Bailian decision-model-preview, asked as two single-question requests in parallel (dm_single)
or both questions in ONE request (dm_fan).
cold = a fresh HTTP client per call (what production sees: httpx drops idle keep-alive connections after 5 s,
sentences are further apart than that); warm = one persistent client, calls back to back.
Emits one JSON line per measurement on stdout.  Synthetic test sentences only.
"""
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

DATA = json.loads(r"""__DATASET_JSON__""")
TIMEOUT_S = 10.0  # measure the natural latency; production cuts at 1.2 s (analysed offline)
WARM_N = int(os.environ.get("WARM_N", "60"))
SEED = 20261003

DS = dict(key=os.environ["DEEPSEEK_API_KEY"].strip(), url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
          model=os.environ.get("DEEPSEEK_FAST_MODEL", "deepseek-flash"), thinking="deepseek")
QW = dict(key=os.environ["DASHSCOPE_API_KEY"].strip(),
          url=os.environ.get("DASHSCOPE_COMPATIBLE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
          model="qwen-flash", thinking="dashscope")
DM_URL = os.environ["DECISION_MODEL_URL"]  # the account-specific .../compatible-mode/v1/systemone endpoint
DM_KEY = os.environ["DASHSCOPE_API_KEY"].strip()

Q_LIVE = {
    "type": "noul",
    "instructions": "说话人这句话是否在请求需要最新的、会变化的或依赖当前时间地点的公开信息才能回答的内容？"
                    "例如天气、新闻、股价、汇率、航班/火车/动车时刻、路况、政策、赛事比分等。",
    "criteria": {
        "true": "说话人想知道现在或近期才会变化的实时信息，必须联网查询才能可靠回答",
        "false": "闲聊、故事、常识、观点、回忆、固定知识，或者只是谈到天气、股票、新闻等话题但不需要实时信息就能回答",
    },
}
Q_CLOSE = {
    "type": "noul",
    "instructions": "说话人这句话是否在要求结束与语音助手的对话并让助手进入待命休息？"
                    "例如明确告别、让助手退下、先不聊了、不用陪了、可以休息了。",
    "criteria": {
        "true": "说话人明确想结束这次对话",
        "false": "普通聊天、提问、讲故事、继续当前话题，或只是在讨论或引用「再见/拜拜」这个词、替别人说再见、让助手留下继续陪",
    },
}


def emit(**row):
    print(json.dumps(row, ensure_ascii=False), flush=True)


def ms_since(t0):
    return (time.perf_counter() - t0) * 1000


async def timed_classify(clf, text):
    t0 = time.perf_counter()
    verdict = await clf.classify(current_text=text)
    return str(verdict.value), ms_since(t0)


def build_pair(p, client_live, client_close):
    live = LiveLookupSemanticClassifier(LiveLookupSemanticClassifierConfig(
        api_key=p["key"], base_url=p["url"], model=p["model"], timeout_s=TIMEOUT_S, thinking_mode=p["thinking"]),
        client=client_live)
    close = CloseIntentSemanticClassifier(CloseIntentSemanticClassifierConfig(
        api_key=p["key"], base_url=p["url"], model=p["model"], timeout_s=TIMEOUT_S, thinking_mode=p["thinking"]),
        client=client_close)
    return live, close


async def run_llm_pair(p, text, warm_clients):
    """Both production classifiers in parallel (as the runtime does), cold or warm."""
    if warm_clients is None:
        c1, c2 = httpx.AsyncClient(), httpx.AsyncClient()
    else:
        c1, c2 = warm_clients
    live, close = build_pair(p, c1, c2)
    t0 = time.perf_counter()
    (v1, l1), (v2, l2) = await asyncio.gather(timed_classify(live, text), timed_classify(close, text))
    wall = ms_since(t0)
    if warm_clients is None:
        await c1.aclose()
        await c2.aclose()
    return dict(raw_live=v1, raw_close=v2, ms_live=round(l1, 1), ms_close=round(l2, 1), pair_ms=round(wall, 1))


async def dm_call(client, questions, text):
    t0 = time.perf_counter()
    try:
        r = await client.post(DM_URL, headers={"Authorization": DM_KEY, "Content-Type": "application/json"},
                              json={"model": "decision-model-preview", "state": text, "questions": questions},
                              timeout=TIMEOUT_S)
        ms = ms_since(t0)
        if r.status_code != 200:
            return dict(status=r.status_code, err=r.text[:200], ms=ms)
        j = r.json()
        return dict(status=200, ans=j.get("answers", {}), server_ms=j.get("latency_ms"), ms=ms,
                    tokens=(j.get("usage") or {}).get("input_tokens"))
    except Exception as e:  # noqa: BLE001
        return dict(status=-1, err=f"{type(e).__name__}: {e}"[:200], ms=ms_since(t0))


def noul(res, key):
    try:
        return float(res["ans"][key]["noul"])
    except Exception:  # noqa: BLE001
        return None


async def run_dm_single(text, warm_clients):
    if warm_clients is None:
        c1, c2 = httpx.AsyncClient(), httpx.AsyncClient()
    else:
        c1, c2 = warm_clients
    t0 = time.perf_counter()
    r1, r2 = await asyncio.gather(dm_call(c1, {"needs_live_lookup": Q_LIVE}, text),
                                  dm_call(c2, {"ends_conversation": Q_CLOSE}, text))
    wall = ms_since(t0)
    if warm_clients is None:
        await c1.aclose()
        await c2.aclose()
    return dict(p_live=noul(r1, "needs_live_lookup"), p_close=noul(r2, "ends_conversation"),
                ms_live=round(r1["ms"], 1), ms_close=round(r2["ms"], 1), pair_ms=round(wall, 1),
                server_ms=[r1.get("server_ms"), r2.get("server_ms")], status=[r1["status"], r2["status"]],
                err=[r1.get("err"), r2.get("err")])


async def run_dm_fan(text, warm_client):
    c = httpx.AsyncClient() if warm_client is None else warm_client
    r = await dm_call(c, {"needs_live_lookup": Q_LIVE, "ends_conversation": Q_CLOSE}, text)
    if warm_client is None:
        await c.aclose()
    return dict(p_live=noul(r, "needs_live_lookup"), p_close=noul(r, "ends_conversation"), pair_ms=round(r["ms"], 1),
                server_ms=r.get("server_ms"), status=r["status"], err=r.get("err"), tokens=r.get("tokens"))


async def one(cfg, text, warm):
    if cfg == "ds":
        return await run_llm_pair(DS, text, warm)
    if cfg == "qw":
        return await run_llm_pair(QW, text, warm)
    if cfg == "dm_single":
        return await run_dm_single(text, warm)
    if cfg == "dm_fan":
        return await run_dm_fan(text, warm)
    raise ValueError(cfg)


async def main():
    rnd = random.Random(SEED)
    order = list(range(len(DATA)))
    rnd.shuffle(order)
    configs = ["ds", "qw", "dm_single", "dm_fan"]
    emit(kind="start", n=len(DATA), configs=configs, ds_model=DS["model"], warm_n=WARM_N)

    # ---- pass 1: accuracy + production-like (cold) latency, every utterance through every config ----
    for k, i in enumerate(order):
        row = DATA[i]
        cs = configs[:]
        rnd.shuffle(cs)
        for cfg in cs:
            res = await one(cfg, row["text"], None)
            emit(kind="cold", cfg=cfg, i=i, text=row["text"], a=row["a"], b=row["b"], hard=row["hard"], **res)
        if k % 20 == 0:
            print(f"cold pass {k}/{len(order)}", file=sys.stderr, flush=True)

    # ---- pass 2: warm (persistent connection) latency on a subset ----
    warm = {
        "ds": (httpx.AsyncClient(), httpx.AsyncClient()),
        "qw": (httpx.AsyncClient(), httpx.AsyncClient()),
        "dm_single": (httpx.AsyncClient(), httpx.AsyncClient()),
        "dm_fan": httpx.AsyncClient(),
    }
    for cfg in configs:  # throwaway warm-ups (not emitted)
        for _ in range(3):
            await one(cfg, "你好呀。", warm[cfg])
    subset = order[:WARM_N]
    for k, i in enumerate(subset):
        row = DATA[i]
        cs = configs[:]
        rnd.shuffle(cs)
        for cfg in cs:
            res = await one(cfg, row["text"], warm[cfg])
            emit(kind="warm", cfg=cfg, i=i, text=row["text"], a=row["a"], b=row["b"], hard=row["hard"], **res)
        if k % 20 == 0:
            print(f"warm pass {k}/{len(subset)}", file=sys.stderr, flush=True)
    for v in warm.values():
        for c in (v if isinstance(v, tuple) else (v,)):
            await c.aclose()
    emit(kind="done")


asyncio.run(main())
