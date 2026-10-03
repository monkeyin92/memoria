"""Embed the datasets into a harness and print the result: build.py eval_round1.py | build.py eval_round2.py.

    python build.py eval_round1.py > /tmp/eval1.py
    ssh <host> "docker exec -i -e PYTHONPATH=/app -e DECISION_MODEL_URL=... -w /app <bridge> /app/.venv/bin/python -" < /tmp/eval1.py > full.out

The harness needs DEEPSEEK_API_KEY and DASHSCOPE_API_KEY in the container's environment (they never leave it).
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
template = (HERE / sys.argv[1]).read_text(encoding="utf-8")
dev = json.loads((HERE / "dataset-dev.json").read_text(encoding="utf-8"))
held = json.loads((HERE / "dataset-heldout.json").read_text(encoding="utf-8"))
template = template.replace("__DATASET_JSON__", json.dumps(dev, ensure_ascii=False))
template = template.replace("__DEV_JSON__", json.dumps(dev, ensure_ascii=False))
template = template.replace("__HELD_JSON__", json.dumps(held, ensure_ascii=False))
sys.stdout.write(template)
