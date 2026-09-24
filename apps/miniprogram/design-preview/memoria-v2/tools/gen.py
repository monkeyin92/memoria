import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

KEY = os.environ["IMG_KEY"]
BASE = "https://www.bb-api.com/v1"


def post_json(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=600))


def post_multipart(path, fields, files):
    b = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, p in files:
        parts.append(
            f'--{b}\r\nContent-Disposition: form-data; name="{k}"; '
            f'filename="{os.path.basename(p)}"\r\nContent-Type: image/png\r\n\r\n'.encode()
            + open(p, "rb").read()
            + b"\r\n"
        )
    parts.append(f"--{b}--\r\n".encode())
    req = urllib.request.Request(
        BASE + path,
        data=b"".join(parts),
        headers={
            "Authorization": "Bearer " + KEY,
            "Content-Type": "multipart/form-data; boundary=" + b,
        },
    )
    return json.load(urllib.request.urlopen(req, timeout=600))


def save(res, out):
    d = res["data"][0]
    if d.get("b64_json"):
        open(out, "wb").write(base64.b64decode(d["b64_json"]))
    else:
        open(out, "wb").write(urllib.request.urlopen(d["url"], timeout=120).read())


# usage: gen.py out.png size "prompt" [ref.png ...]
out, size, prompt, refs = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
EXTRA = json.loads(os.environ.get("IMG_EXTRA", "{}"))
model = os.environ.get("IMG_MODEL", "gpt-image-2.5")
for _ in range(3):
    try:
        t = time.time()
        fields = dict({"model": model, "prompt": prompt, "size": size, "n": 1}, **EXTRA)
        if refs:
            image_field = "image[]" if len(refs) > 1 else "image"
            res = post_multipart("/images/edits", fields, [(image_field, r) for r in refs])
        else:
            res = post_json("/images/generations", fields)
        save(res, out)
        print("ok", out, round(time.time() - t), "s")
        break
    except urllib.error.HTTPError as e:
        print("http", e.code, e.read()[:400].decode(errors="ignore"))
        time.sleep(3)
    except Exception as e:
        print("err", repr(e)[:300])
        time.sleep(3)
