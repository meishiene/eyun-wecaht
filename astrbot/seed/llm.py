import json, os, sys, time, urllib.request
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "plugins" / "kb_digest"))
import kb_core as kb

URL = "https://mux.la/v1/chat/completions"
MODEL = os.environ.get("SEED_MODEL", "gpt-5.6-sol")

def _opener():
    """服务器直连 mux.la 不通，要走代理：SEED_PROXY=http://<代理地址>:<端口>（只用于这一个请求，不影响别的）。"""
    px = os.environ.get("SEED_PROXY")
    return urllib.request.build_opener(urllib.request.ProxyHandler({"https": px, "http": px})) if px else urllib.request.build_opener()

def chat(prompt, retries=3, timeout=240):
    key = os.environ["SEED_KEY"]
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": prompt}]}).encode()
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(URL, body, {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
            with _opener().open(req, timeout=timeout) as r:
                return json.load(r)["choices"][0]["message"]["content"]
        except Exception as e:
            last = e; time.sleep(3 * (i + 1))
    raise RuntimeError(f"LLM 调用失败: {last}")

def chat_json(prompt, tries=2):
    for _ in range(tries):
        obj = kb.parse_json(chat(prompt))
        if obj is not None:
            return obj
    return None
