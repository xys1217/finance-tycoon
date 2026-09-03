#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""
函数「返回元组长度」一致性检查器。

存在的理由：2026-09-03 第三轮检查时，我把 engine.sig_plan() 的返回值从
4 元组改成 5 元组（加了 budget、shares），却漏改 server.py 里的解包：

    for c, n, leg, w in st["plan"]        # 4 个变量 vs 5 元组 → ValueError

结果 /api/paper 直接 500。这类 bug 的三个特点：
  1. pyflakes / pylint 都查不出来（元组长度是运行时才确定的）
  2. 只在特定分支触发（当时只在「有当日信号」这条路径上炸）
  3. 手工验证极易漏掉 —— 因为改的人脑子里想的是「我改了 sig_plan」，
     不会自动想起「谁在解包它的返回值」

所以这个脚本做的事：
  A. 扫出每个函数所有 return 语句的元组长度，若同一函数返回长度不一致 → 报警
  B. 扫出所有调用点（含跨模块 PE.xxx 这种别名调用），检查解包变量个数
     是否落在该函数返回长度集合内 → 不一致则报警
  C. 追踪「先赋值再解包」的间接调用：plan = sig_plan(sig); for a,b,c in plan
     —— B 只能查 `for a,b,c in f()` 这种直接调用，但真实代码里大量是
        先存变量再遍历，这个盲区必须补上

用法：python3.11 arities_check.py [目录]
退出码：0 干净 / 1 有问题
"""
from __future__ import annotations

import ast
import os
import sys
from collections import defaultdict

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {"__pycache__", ".git", "node_modules"}


def iter_py(root: str):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            if fn.endswith(".py"):
                yield os.path.join(dp, fn)


def ret_len(node: ast.Return) -> int | None:
    """返回 return 语句的元组长度；不是元组返回 None。"""
    v = node.value
    if v is None:
        return 0
    if isinstance(v, ast.Tuple):
        return len(v.elts)
    return None


def tuple_len(node) -> int | None:
    """任意表达式节点是不是元组字面量，是则返回长度。"""
    if isinstance(node, ast.Tuple):
        return len(node.elts)
    return None


class Collector(ast.NodeVisitor):
    """第一遍：收集 函数名 → {元组长度: 行号列表}

    产出元组的三种写法都要认，只认 `return (a,b,c)` 会漏掉最常见的
    「先 append 到 list 再 return list」：

        def sig_plan(sig):
            out = []
            out.append((code, name, leg, budget, shares))   # ← 真正的结构在这
            return out                                       # ← 这里只是个变量名

    漏了 append 模式 = 表里根本没有这个函数 = 所有针对它的检查全部静默失效。
    """

    def __init__(self, path: str):
        self.path = path
        self.rets: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))

    def _record(self, fname: str, n: int | None, lineno: int):
        if n is not None and n > 0:
            self.rets[fname][n].append(lineno)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        for sub in ast.walk(node):
            # 1. return (a, b, c)
            if isinstance(sub, ast.Return):
                self._record(node.name, ret_len(sub), sub.lineno)
            # 2. out.append((a, b, c)) / s.add((a, b))
            elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                    and sub.func.attr in ("append", "add") and sub.args:
                self._record(node.name, tuple_len(sub.args[0]), sub.lineno)
            # 3. yield (a, b, c)
            elif isinstance(sub, ast.Yield) and sub.value is not None:
                self._record(node.name, tuple_len(sub.value), sub.lineno)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef


def collect_all(root: str):
    """跨模块合并：funcname -> {length: [(file, line)]}"""
    merged: dict[str, dict[int, list[tuple[str, int]]]] = defaultdict(
        lambda: defaultdict(list))
    for p in iter_py(root):
        try:
            tree = ast.parse(open(p, encoding="utf-8").read())
        except SyntaxError as e:
            print(f"[语法错误] {p}:{e.lineno} {e.msg}")
            continue
        c = Collector(p)
        c.visit(tree)
        for fname, lens in c.rets.items():
            for n, lines in lens.items():
                merged[fname][n].extend((os.path.relpath(p, root), ln) for ln in lines)
    return merged


class UnpackChecker(ast.NodeVisitor):
    """第二遍：检查所有调用点的元组解包长度"""

    def __init__(self, path: str, table, root):
        self.path = os.path.relpath(path, root)
        self.table = table
        self.hits: list[str] = []
        # 数据流：变量名 -> 它可能来自哪些函数（用于 C. 间接解包）
        self.var2funcs: dict[str, set[str]] = {}
        # 记录 import 别名：engine as PE → PE.xxx 视为 engine 模块的函数
        self.aliases: dict[str, str] = {}
        for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.aliases[a.asname] = a.name.split(".")[-1]

    # ---- C. 变量数据流 -------------------------------------------------
    def _calls_in(self, node) -> set[str]:
        """递归收集表达式里出现的所有调用名（覆盖 A if cond else B 两支）。"""
        out = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                nm = self._callee(sub)
                if nm:
                    out.add(nm)
        return out

    def _track_assign(self, node: ast.Assign):
        """x = f() / x = y / x = A if c else B  → 记录 x 的来源函数。"""
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            return
        var = node.targets[0].id
        val = node.value
        if isinstance(val, ast.Call):
            nm = self._callee(val)
            if nm:
                self.var2funcs[var] = {nm}
                return
        if isinstance(val, ast.Name) and val.id in self.var2funcs:
            self.var2funcs[var] = set(self.var2funcs[val.id])   # 别名传递
            return
        srcs = self._calls_in(val)
        if srcs:
            self.var2funcs[var] = srcs

    def visit_Assign(self, node: ast.Assign):
        self._track_assign(node)
        self._check_unpack(node.value, node.targets, node.lineno)
        self.generic_visit(node)

    def visit_For(self, node: ast.For):
        # for a, b, c in f(): ...
        if isinstance(node.iter, ast.Call) and isinstance(node.target, ast.Tuple):
            n = len(node.target.elts)
            self._check_call(node.iter, n, node.lineno)
        # for a, b, c in plan:   ← plan 是变量，走数据流查来源函数
        elif isinstance(node.iter, ast.Name) and isinstance(node.target, ast.Tuple):
            n = len(node.target.elts)
            for fn in self.var2funcs.get(node.iter.id, ()):
                if fn in self.table and n not in set(self.table[fn]):
                    self.hits.append(
                        f"{self.path}:{node.lineno}  遍历 {node.iter.id} "
                        f"（来自 {fn}()）解包 {n} 个变量，"
                        f"但该函数返回长度 {sorted(set(self.table[fn]))}")
        self.generic_visit(node)

    def visit_ListComp(self, node: ast.ListComp):
        # [expr for a,b,c in f()]  —— comprehension 的 for 里解包
        for gen in node.generators:
            if isinstance(gen.iter, ast.Call) and isinstance(gen.target, ast.Tuple):
                name = self._callee(gen.iter)
                if name and name in self.table:
                    lens = set(self.table[name])
                    n = len(gen.target.elts)
                    if n not in lens:
                        self.hits.append(
                            f"{self.path}:{node.lineno}  列表推导解包 {name}() "
                            f"→ {n} 个变量，但该函数返回长度 {sorted(lens)}")
        self.generic_visit(node)

    def _check_unpack(self, value, targets, lineno):
        if not isinstance(value, ast.Call):
            return
        for t in targets:
            if isinstance(t, ast.Tuple):
                self._check_call(value, len(t.elts), lineno)

    def _callee(self, call: ast.Call) -> str | None:
        f = call.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            base = f.value.id if isinstance(f.value, ast.Name) else None
            if base and base in self.aliases:
                base = self.aliases[base]  # PE.xxx → paper/engine 里的 xxx
            return f.attr
        return None

    def _check_call(self, call: ast.Call, n: int, lineno: int):
        name = self._callee(call)
        if not name or name not in self.table:
            return
        lens = set(self.table[name])
        if n not in lens:
            self.hits.append(
                f"{self.path}:{lineno}  解包 {name}() 用了 {n} 个变量，"
                f"但该函数返回长度 {sorted(lens)}")


def main() -> int:
    table = collect_all(ROOT)
    print(f"扫描 {ROOT}")
    print(f"收集到 {len(table)} 个返回元组的函数\n")

    # A. 同一函数返回长度不一致
    print("=" * 68)
    print("A. 返回元组长度不一致的函数（调用方极易漏改）")
    print("=" * 68)
    bad_var = 0
    for name, lens in sorted(table.items()):
        if len(lens) > 1:
            bad_var += 1
            desc = "、".join(
                f"{n} 元组({len(v)} 处)" for n, v in sorted(lens.items()))
            print(f"  ⚠ {name}()：{desc}")
            for n, locs in sorted(lens.items()):
                for f, ln in locs[:3]:
                    print(f"      {n} 元组 @ {f}:{ln}")
    if not bad_var:
        print("  无 ✓")

    # B. 调用点解包长度不匹配
    print()
    print("=" * 68)
    print("B. 调用点解包变量个数 vs 函数返回长度")
    print("=" * 68)
    hits: list[str] = []
    for p in iter_py(ROOT):
        try:
            tree = ast.parse(open(p, encoding="utf-8").read())
        except SyntaxError:
            continue
        c = UnpackChecker(p, table, ROOT)
        c.visit(tree)
        hits.extend(c.hits)
    if hits:
        for h in sorted(set(hits)):
            print(f"  ✗ {h}")
    else:
        print("  无 ✓")

    print()
    print("=" * 68)
    total = bad_var + len(set(hits))
    print(f"问题合计：{total}")
    if total:
        print("结论：存在返回值/解包不一致，修复后重跑")
        return 1
    print("结论：干净 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
