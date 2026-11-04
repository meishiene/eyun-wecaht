"""把《问答案例文档》解析成：手册文本 + 案例行列表。表格每行=一个案例，原行逐字保留。"""
import json, re, sys
from pathlib import Path

def split_row(line):
    cells, cur, tick = [], "", False
    for ch in line.strip().strip("|"):
        if ch == "`": tick = not tick
        if ch == "|" and not tick:
            cells.append(cur.strip()); cur = ""
        else:
            cur += ch
    cells.append(cur.strip())
    return cells

def parse(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    manual_end = next(i for i, l in enumerate(lines) if l.startswith("## 二"))
    manual = "\n".join(lines[:manual_end]).rstrip() + "\n"
    cases, section, header, in_36 = [], "", None, False
    for i in range(manual_end, len(lines)):
        n, l = i + 1, lines[i]
        if l.startswith("#"):
            section = l.lstrip("# ").strip(); header = None
            in_36 = section.startswith("3.6")
            continue
        if l.startswith("|"):
            cells = split_row(l)
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c): continue
            if header is None: header = cells; continue
            cases.append({"line": n, "section": section, "kind": "table", "header": header, "cells": cells, "raw": l})
        elif in_36 and l.startswith("- "):
            cases.append({"line": n, "section": section, "kind": "need_verify", "header": [], "cells": [l[2:]], "raw": l})
        elif l.startswith("> 知识库视角补充"):
            cases.append({"line": n, "section": section, "kind": "note", "header": [], "cells": [l[2:]], "raw": l})
    return manual, cases

if __name__ == "__main__":
    manual, cases = parse(sys.argv[1])
    from collections import Counter
    c = Counter(x["section"] for x in cases)
    for k, v in c.items(): print(f"{v:3d}  {k}")
    print("合计案例行:", len(cases), "| 手册行数:", manual.count("\n"))
    json.dump(cases, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    Path(sys.argv[3]).write_text(manual, encoding="utf-8")
