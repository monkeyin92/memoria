#!/usr/bin/env python3
"""Create the stop-word hotword list for the production FunASR model (N-10).

A DashScope vocabulary is bound to one target model, so the model here must be
the one the bridge runs (`FUNASR_MODEL`, currently fun-asr-realtime). The
resulting id goes into `FUNASR_VOCABULARY_ID` in /etc/memoria-agent.env.

Usage:
    DASHSCOPE_API_KEY=... uv run --with dashscope python scripts/create_stop_word_vocabulary.py \
        [--model fun-asr-realtime] [--delete <old_vocabulary_id>]
"""
from __future__ import annotations

import argparse
import os
import sys

import dashscope
from dashscope.audio.asr import VocabularyService

STOP_WORDS = [
    {"text": "停", "weight": 5},
    {"text": "别说了", "weight": 5},
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="fun-asr-realtime")
    parser.add_argument("--delete", metavar="VOCABULARY_ID")
    args = parser.parse_args()

    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        print("DASHSCOPE_API_KEY is not set", file=sys.stderr)
        return 1
    dashscope.api_key = api_key

    service = VocabularyService()
    if args.delete:
        service.delete_vocabulary(args.delete)
        print(f"deleted {args.delete}")
        return 0

    vocabulary_id = service.create_vocabulary(
        prefix="memstop",
        target_model=args.model,
        vocabulary=STOP_WORDS,
    )
    info = service.query_vocabulary(vocabulary_id)
    print(f"vocabulary_id={vocabulary_id}")
    print(f"status={info.get('status')} target_model={info.get('target_model')}")
    print(f"words={[(w['text'], w['weight']) for w in info.get('vocabulary', [])]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
