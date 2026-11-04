"""逐条案例归类+抽取，结果增量写入 draft_raw.json（可断点续跑）。"""
import json, sys, time
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parent))
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import llm
from llm import kb

HERE = Path(__file__).resolve().parent
TREE = kb.tree_text(kb.load_tree(Path(__import__("tempfile").mkdtemp())))

PROMPT = """你是「大模型中转站」客服知识库的整理员。下面是《问答案例文档》里已整理好的【一条】案例（表格的一行，或一个要点）。
请把它整理成知识库的一个问题，严格输出一个 JSON 对象（不要多余文字、不要代码块）：
{{
 "title": "问题标题，25字内，概括现象或问法",
 "category": "只能从下面分类树里选一个【细类编号】；实在放不进去填 0",
 "new_cat": null 或 {{"name":"建议新增的细类名","major":"所属大类编号","reason":"为什么放不进现有细类"}},
 "status": "已解决|未解决|待跟进|需人工核验 之一",
 "source": "官方文档|后台核验|经验判断|知识库提炼|待验证 之一",
 "tags": ["模型/厂商、渠道形态(号池/直连/反代/Bedrock/Azure/VT/AS)、分组、状态码、接入工具(Claude Code/Codex CLI/OpenClaw/Cherry Studio等)，只写原文出现的"],
 "brief": "简要：1~3句，说清问题是什么、结论是什么",
 "phenomenon": "现象（客户问题/报错）",
 "elements": {{"status_code":"","message":"","status_field":"","rid":"","model_path_group":""}},
 "cause": "原因",
 "solution": "解决方案",
 "reply_script": "可直接发给客户的答复口径；原文已有就取原文，没有就依据原文结论简短拟写，不加新事实",
 "retry_helps": "有用|无效|视情况|未说明",
 "need_verify": "是|否",
 "verify_materials": "若需人工核验，写要收集哪些材料，否则空",
 "impact": "影响范围",
 "evidence": "对应原文的关键句（简短引用）",
 "dedupe_key": "用10~20字概括这条案例讲的核心问题，用于后续找重复"
}}
规则：
1）不得编造原文没有的事实，原文没有就写“原文未提及”；原文里的接口路径、报错原文、模型名必须保留。
2）口径来源：章节是 2.8（官方文档）→ 官方文档；章节是 3.x（知识库提炼）→ 知识库提炼；2.1~2.7 里写了“平台口径/官方口径”→ 官方文档，写了“后台核验/需查 rid”→ 后台核验，其余 → 经验判断。
3）案例来自「3.6 不能给精确答案的问法」时：status 必须是“需人工核验”，need_verify 为“是”，verify_materials 取箭头后面的内容，brief 写成“该问法不能仅凭知识库回答，需要…”。
4）状态：给出明确解决办法的填“已解决”；只是解释现象/口径的也填“已解决”；原文说平台待修复/观察的填“待跟进”。
5）模型×接口、判定口诀这类通用方法不在此列，只处理这一条案例。

分类树：
{tree}

案例所在章节：{section}
表头：{header}
案例原行（第{line}行）：
{raw}
"""

def one(c):
    body = c["raw"] if c["kind"] != "table" else c["raw"]
    d = llm.chat_json(PROMPT.format(tree=TREE, section=c["section"], header=" | ".join(c["header"]) or "（无）",
                                    line=c["line"], raw=body))
    return {"line": c["line"], "data": d}

if __name__ == "__main__":
    cases = json.load(open(HERE / "cases.json", encoding="utf-8"))
    out_p = HERE / "draft_raw.json"
    done = {r["line"]: r for r in json.load(open(out_p, encoding="utf-8"))} if out_p.exists() else {}
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    todo = [c for c in cases if not (c["line"] in done and done[c["line"]]["data"])]
    if limit: todo = todo[:limit]
    print(f"待处理 {len(todo)} 条（已完成 {sum(1 for r in done.values() if r['data'])}）", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(6) as ex:
        for i, r in enumerate(ex.map(one, todo), 1):
            done[r["line"]] = r
            out_p.write_text(json.dumps(list(done.values()), ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[{i}/{len(todo)}] 第{r['line']}行 {'OK ' + r['data'].get('title','') if r['data'] else '解析失败'}  ({time.time()-t0:.0f}s)", flush=True)
