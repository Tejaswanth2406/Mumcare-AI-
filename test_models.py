import json
import os
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("OPENROUTER_API_KEY", "")

if not api_key:
    raise SystemExit("OPENROUTER_API_KEY is missing. Set it in .env before running this script.")

models = [
    "anthropic/claude-3-haiku",
    "anthropic/claude-3-haiku:beta",
    "openai/gpt-4o-mini",
]

for model in models:
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Say OK"}],
        "max_tokens": 5
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST"
    )
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        print(f"OK  {model}: {content}")
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode())
        msg = body.get("error", {}).get("message", "")[:70]
        print(f"ERR {model}: {e.code} - {msg}")
    except Exception as e:
        print(f"EXC {model}: {e}")
