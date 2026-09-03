# -*- coding: utf-8 -*-
"""
把 modules/quant/index.html（原仪表盘）转成可注入的片段，
CSS 全部加 #mod-quant 作用域前缀，避免污染主页面。
输出：api/quant_partial.json
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "modules", "quant", "index.html")
OUT = os.path.join(HERE, "api", "quant_partial.json")

html = open(SRC, encoding="utf-8").read()

# 1) 抽 style
m = re.search(r"<style>(.*?)</style>", html, re.S)
css = m.group(1) if m else ""

# 2) 抽 body 内容
m = re.search(r"<body>(.*?)</body>", html, re.S)
body = m.group(1) if m else html

# 去掉顶部那条「归档产物」提示（信息已并入模块头部）
body = re.sub(r'<div style="background:#fff3e0.*?</div></div>', "", body, flags=re.S)
# 去掉重复的 h1（主页面已有大标题）
body = re.sub(r"<h1>.*?</h1>", "", body, flags=re.S)

# 3) CSS 加作用域前缀：每个选择器前加 #mod-quant
#    先去掉注释，再按 "}" 切块
css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
blocks = [b.strip() for b in css.split("}") if b.strip()]
scoped = []
for b in blocks:
    if "{" not in b:
        continue
    sel, decl = b.split("{", 1)
    sel = sel.strip()
    if not sel:
        continue
    if sel.startswith("@"):
        scoped.append(f"{sel}{{{decl}}}")          # @media 保持原样
        continue
    # 逗号分隔的多选择器逐个加前缀
    parts = []
    for s in sel.split(","):
        s = s.strip()
        if not s:
            continue
        if s.startswith("#mod-quant"):
            parts.append(s)
        elif s == "body" or s == "html":
            parts.append("#mod-quant")              # 容器本身扮演 body 角色
        elif s in ("*", "*:before", "*::before", "*::after"):
            parts.append("#mod-quant, #mod-quant *")
        else:
            parts.append(f"#mod-quant {s}")
    scoped.append(", ".join(parts) + " {" + decl + "}")

out = {"css": "\n".join(scoped), "html": body.strip()}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)

print(f"CSS 规则 {len(scoped)} 条，HTML {len(out['html'])} 字符 → {OUT}")
