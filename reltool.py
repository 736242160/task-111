#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reltool.py — 多类关系一致性维护工具（纯 Python 标准库，单文件）

功能概述
========
维护一批实体上的三类关系（配对 / 引用 / 映射），按操作流（增 / 删 / 改）逐条
应用并即时校验，输出最终关系状态与错误清单。

输入格式（文本行，`#` 起为注释，空行忽略；字段以空白分隔）
==========================================================
    实体 <名称>                          定义实体
    关系 <类> <名称> <目标>               初始关系表（类 ∈ 配对|引用|映射）
    操作 增 <类> <名称> <目标>            新增关系
    操作 删 <类> <名称> <目标>            删除关系
    操作 改 <类> <名称> <旧目标> <新目标> 修改关系目标

一致性规则
==========
类内（硬约束，违反则操作失败并回滚）：
  * 配对：无向、成对；任一实体最多属于一对（配对唯一）。
  * 引用：源与目标实体必须已定义（引用目标存在）。
  * 映射：双向唯一（单射）：一名一目标，一目标一名。
跨类（软约束，即时报告但保留状态）：
  * 交叉冲突：实体 e 与 p 存在配对，且 e 的映射目标恰为 p（或 p 的映射目标
    恰为 e），视为“配对—映射”角色冲突，报告冲突实体链。
  * 残留引用：某 (名称,目标) 的映射/配对被删除后，仍存在同 (名称,目标)
    的引用关系，视为残留引用并报告（关系被重新建立后自动消除）。
  * 关系环：以 引用/映射 为有向边、配对 为双向边构图，凡环上涉及两类及
    以上关系的关系环，报告环上实体链。
操作级错误（硬失败）：引用不存在的实体、增已存在的关系、删/改不存在的
关系、改操作的新目标违反硬约束。

回滚策略（自定，理由见下）
==========================
以“单个操作”为原子单位：每个操作开始前做状态快照，操作内（含 改 = 删+增
两步）任一步骤违反硬约束即整体恢复到操作前状态，并记录错误。理由：操作是
用户意图的最小完整单元，半应用状态语义不明；而跨操作状态正常延续，已确认
成功的操作不受后续失败影响。初始关系表按同样规则逐条应用。

运行方式
========
    python3 reltool.py 输入文件          # 从文件读取
    python3 reltool.py < 输入文件        # 从标准输入读取
    python3 reltool.py --demo            # 运行内置示例
"""

import sys

REL_CLASSES = ("配对", "引用", "映射")
OP_TYPES = ("增", "删", "改")
MAX_CYCLE_LEN = 8  # 环搜索长度上限，防止组合爆炸


class RelationStore:
    """实体与三类关系的状态存储及全部校验逻辑。"""

    def __init__(self):
        self.entities = set()
        self.pairs = set()   # 配对: frozenset({a, b})，无向
        self.refs = set()    # 引用: (源, 目标)，有向
        self.maps = {}       # 映射: 名称 -> 目标，有向且双向唯一
        # 已被删除且尚未重建的 (类, 名称, 目标)，用于残留引用检测
        self.deleted = set()

    # ---------- 快照 / 回滚 ----------
    def snapshot(self):
        return (set(self.entities), set(self.pairs),
                set(self.refs), dict(self.maps), set(self.deleted))

    def restore(self, snap):
        (self.entities, self.pairs,
         self.refs, self.maps, self.deleted) = snap

    # ---------- 关系查询 ----------
    def has_rel(self, cls, a, b):
        if cls == "配对":
            return frozenset((a, b)) in self.pairs
        if cls == "引用":
            return (a, b) in self.refs
        return self.maps.get(a) == b

    # ---------- 硬校验：增一条关系是否合法（不修改状态） ----------
    def check_add(self, cls, a, b):
        errs = []
        for e in (a, b):
            if e not in self.entities:
                errs.append("实体「%s」不存在" % e)
        if errs:
            return errs
        if self.has_rel(cls, a, b):
            errs.append("关系重复：%s %s -> %s 已存在" % (cls, a, b))
            return errs
        if cls == "配对":
            if a == b:
                errs.append("配对不允许自配对：%s" % a)
            for p in self.pairs:
                if a in p or b in p:
                    errs.append("配对唯一性冲突：%s 或 %s 已属于配对 %s"
                                % (a, b, "<->".join(sorted(p))))
        elif cls == "映射":
            if a in self.maps:
                errs.append("映射唯一性冲突：%s 已映射到 %s"
                            % (a, self.maps[a]))
            for src, dst in self.maps.items():
                if dst == b:
                    errs.append("映射双向唯一冲突：目标 %s 已被 %s 映射"
                                % (b, src))
        return errs

    # ---------- 应用增 / 删（调用前须通过硬校验） ----------
    def do_add(self, cls, a, b):
        if cls == "配对":
            self.pairs.add(frozenset((a, b)))
        elif cls == "引用":
            self.refs.add((a, b))
        else:
            self.maps[a] = b
        self.deleted.discard((cls, a, b))  # 重建即消除残留标记

    def do_del(self, cls, a, b):
        if cls == "配对":
            self.pairs.discard(frozenset((a, b)))
        elif cls == "引用":
            self.refs.discard((a, b))
        else:
            del self.maps[a]
        if cls in ("配对", "映射"):
            self.deleted.add((cls, a, b))  # 记录删除，供残留引用检测

    # ---------- 软校验：即时报告 ----------
    def soft_issues(self):
        issues = []
        # 1) 跨类交叉冲突：配对双方互为映射目标
        for p in sorted(self.pairs, key=lambda s: sorted(s)):
            a, b = sorted(p)
            if self.maps.get(a) == b or self.maps.get(b) == a:
                issues.append("交叉冲突：%s 与 %s 存在配对，"
                              "却又互为映射目标（配对—映射角色冲突）" % (a, b))
        # 2) 删除关系后的残留引用
        for (a, b) in sorted(self.refs):
            if ("映射", a, b) in self.deleted or ("配对", a, b) in self.deleted:
                issues.append("残留引用：引用 %s -> %s 仍存在，"
                              "但其对应的映射/配对关系已被删除" % (a, b))
        # 3) 跨类关系环
        for chain, classes in self.find_cycles():
            issues.append("关系环（跨类 %s）：%s"
                          % ("/".join(sorted(classes)), " -> ".join(chain)))
        return issues

    def find_cycles(self):
        """在跨类有向图上找简单环，仅保留涉及 >=2 类关系的环。"""
        adj = {}
        for a, b in self.refs:
            adj.setdefault(a, []).append((b, "引用"))
        for a, b in self.maps.items():
            adj.setdefault(a, []).append((b, "映射"))
        for p in self.pairs:
            a, b = sorted(p)
            adj.setdefault(a, []).append((b, "配对"))
            adj.setdefault(b, []).append((a, "配对"))

        found = {}

        def canonical(nodes):
            rots = [tuple(nodes[i:] + nodes[:i]) for i in range(len(nodes))]
            return min(rots)

        def dfs(start, node, path, classes):
            if len(path) > MAX_CYCLE_LEN:
                return
            for nxt, cls in adj.get(node, []):
                if nxt == start and len(path) >= 2:
                    all_cls = classes | {cls}
                    if len(all_cls) >= 2:  # 跨类环才报告
                        key = (canonical(path), frozenset(all_cls))
                        found.setdefault(key, (path + [start], all_cls))
                elif nxt not in path:
                    dfs(start, nxt, path + [nxt], classes | {cls})

        for s in sorted(adj):
            dfs(s, s, [s], set())
        return sorted(found.values(), key=lambda x: x[0])


class Engine:
    """解析输入、逐条应用、收集错误与即时报告。"""

    def __init__(self):
        self.store = RelationStore()
        self.errors = []        # 硬错误清单
        self.reports = []       # 软问题清单（去重，记录首次出现位置）
        self._reported = set()
        self.op_log = []

    def _err(self, where, msg):
        self.errors.append("[%s] %s" % (where, msg))

    def _soft_check(self, where):
        for issue in self.store.soft_issues():
            if issue not in self._reported:
                self._reported.add(issue)
                self.reports.append("[%s] %s" % (where, issue))

    # ---------- 初始数据 ----------
    def add_entity(self, where, name):
        if name in self.store.entities:
            self._err(where, "实体重复定义：%s" % name)
        else:
            self.store.entities.add(name)

    def apply_add(self, where, cls, a, b):
        """增操作（含初始关系表）。硬失败则回滚，返回是否成功。"""
        snap = self.store.snapshot()
        errs = self.store.check_add(cls, a, b)
        if errs:
            self.store.restore(snap)
            for e in errs:
                self._err(where, "增 %s %s %s 失败（已回滚）：%s"
                          % (cls, a, b, e))
            return False
        self.store.do_add(cls, a, b)
        self._soft_check(where)
        return True

    def apply_del(self, where, cls, a, b):
        if not self.store.has_rel(cls, a, b):
            self._err(where, "删 %s %s %s 失败：关系不存在（重复删除或从未建立）"
                      % (cls, a, b))
            return False
        self.store.do_del(cls, a, b)
        self._soft_check(where)
        return True

    def apply_mod(self, where, cls, a, old_b, new_b):
        """改 = 删旧 + 增新，任一步失败整体回滚到操作前。"""
        snap = self.store.snapshot()
        if not self.store.has_rel(cls, a, old_b):
            self._err(where, "改 %s %s %s 失败：原关系不存在"
                      % (cls, a, old_b))
            return False
        self.store.do_del(cls, a, old_b)
        errs = self.store.check_add(cls, a, new_b)
        if errs:
            self.store.restore(snap)  # 回滚：恢复被删的旧关系
            for e in errs:
                self._err(where, "改 %s %s %s -> %s 失败（已回滚）：%s"
                          % (cls, a, old_b, new_b, e))
            return False
        self.store.do_add(cls, a, new_b)
        self._soft_check(where)
        return True

    # ---------- 解析 ----------
    def run(self, lines):
        op_no = 0
        init_no = 0
        for ln, raw in enumerate(lines, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            t = line.split()
            head = t[0]
            if head == "实体" and len(t) == 2:
                self.add_entity("第%d行" % ln, t[1])
            elif head == "关系" and len(t) == 4 and t[1] in REL_CLASSES:
                init_no += 1
                ok = self.apply_add("初始表#%d" % init_no, t[1], t[2], t[3])
                self.op_log.append((init_no, "初始", line, ok))
            elif head == "操作" and len(t) >= 5 and t[1] in OP_TYPES \
                    and t[2] in REL_CLASSES:
                op_no += 1
                where = "操作#%d" % op_no
                typ, cls, a, b = t[1], t[2], t[3], t[4]
                if typ == "增" and len(t) == 5:
                    ok = self.apply_add(where, cls, a, b)
                elif typ == "删" and len(t) == 5:
                    ok = self.apply_del(where, cls, a, b)
                elif typ == "改" and len(t) == 6:
                    ok = self.apply_mod(where, cls, a, b, t[5])
                else:
                    self._err(where, "操作参数个数错误：%s" % line)
                    ok = False
                self.op_log.append((op_no, typ, line, ok))
            else:
                self._err("第%d行" % ln, "无法解析的输入行：%s" % line)

    # ---------- 输出 ----------
    def render(self):
        out = []
        out.append("==== 逐条处理结果 ====")
        for no, kind, line, ok in self.op_log:
            out.append("[%s#%d] %s -> %s"
                       % (kind, no, line, "成功" if ok else "失败（已回滚）"))
        s = self.store
        out.append("")
        out.append("==== 最终关系状态 ====")
        out.append("实体(%d): %s" % (len(s.entities),
                                     " ".join(sorted(s.entities)) or "（无）"))
        out.append("配对(%d): %s" % (len(s.pairs),
                   "  ".join("<->".join(sorted(p)) for p in
                             sorted(s.pairs, key=lambda x: sorted(x)))
                   or "（无）"))
        out.append("引用(%d): %s" % (len(s.refs),
                   "  ".join("%s->%s" % r for r in sorted(s.refs)) or "（无）"))
        out.append("映射(%d): %s" % (len(s.maps),
                   "  ".join("%s->%s" % kv for kv in sorted(s.maps.items()))
                   or "（无）"))
        out.append("")
        out.append("==== 错误清单（硬错误，共%d条） ====" % len(self.errors))
        out.extend("  %d. %s" % (i, e) for i, e in enumerate(self.errors, 1))
        out.append("")
        out.append("==== 即时校验报告（软问题，共%d条） ====" % len(self.reports))
        out.extend("  %d. %s" % (i, r) for i, r in enumerate(self.reports, 1))
        return "\n".join(out)


DEMO_INPUT = """\
# ---- 实体定义 ----
实体 甲
实体 乙
实体 丙
实体 丁

# ---- 初始关系表 ----
关系 配对 甲 乙
关系 引用 甲 丙
关系 映射 丙 丁

# ---- 操作流 ----
操作 增 引用 乙 丙            # 正常
操作 增 映射 甲 丙            # 正常（甲映射丙）
操作 增 映射 乙 丙            # 硬失败：目标丙已被甲映射（双向唯一），回滚
操作 增 配对 甲 丙            # 硬失败：甲已属于配对 甲<->乙，回滚
操作 增 引用 甲 戊            # 硬失败：实体戊不存在，回滚
操作 增 引用 甲 丙            # 硬失败：重复增（初始表已有），回滚
操作 增 引用 丙 丁            # 正常
操作 删 映射 丙 丁            # 正常删除，但留下 引用 丙->丁 => 残留引用报告
操作 删 引用 甲 丙            # 正常
操作 删 引用 甲 丙            # 硬失败：重复删除，关系不存在
操作 改 映射 甲 丙 丁         # 正常：甲->丙 改为 甲->丁
操作 增 引用 丁 甲            # 构成跨类环：甲-映射->丁-引用->甲，报告环
操作 改 映射 甲 丁 乙         # 交叉冲突：甲与乙配对又互为映射目标，软报告
操作 改 映射 甲 乙 戊         # 硬失败：新目标实体戊不存在，回滚（甲->乙保留）
操作 删 配对 甲 乙            # 正常删除配对；引用 乙->丙 等不受影响
"""


def main(argv):
    if "--demo" in argv:
        text = DEMO_INPUT
    elif len(argv) >= 2:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    eng = Engine()
    eng.run(text.splitlines())
    print(eng.render())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
