# -*- coding: utf-8 -*-
"""过滤表达式解析器/求值器单元测试（不依赖 Qt）

语法（简化版）：条件1 | 条件2 | 条件3 —— 只按 | 拆分，命中任一条件即通过；
每个条件是字面子串，空格/引号都按内容原样匹配，没有其他语法。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from filterterminal_plus import compile_filter, split_or_expr  # noqa: E402

PASS, FAIL = 0, 0

def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s" % name)

def ev(expr, line, cs=False, ww=False):
    e, words, tree = compile_filter(expr, cs, ww)
    return e(tree, line), words

print("== | 拆分：OR 语义 ==")
ok, words = ev("a|b|c", "has b")
check("a|b|c 命中 b", ok and words == ["a", "b", "c"])
ok, _ = ev("a|b|c", "nothing here")
check("a|b|c 全不含不命中", not ok)
ok, _ = ev("a | b | c", "has c")
check("竖线两侧空格等价", ok)
ok, _ = ev("a||b", "has a")
check("连续 || 视为一个 |", ok)
ok, words = ev("  a  |  b  ", "has b")
check("条件首尾空白被去掉", ok and words == ["a", "b"])

print("== 空格 = 条件内容的一部分（不再拆词/AND/OR） ==")
ok, words = ev("into home", "xx into home yy")
check("含空格整体命中", ok and words == ["into home"])
ok, _ = ev("into home", "xx into  home yy")
check("两个空格的内容不命中单个空格条件", not ok)
ok, _ = ev("into home", "home only")
check("只有 home 不命中 'into home'", not ok)
ok, _ = ev("into home", "into only")
check("只有 into 不命中 'into home'", not ok)
ok, _ = ev("词1 词2 词3", "xx 词1 词2 词3 yy")
check("三个词带空格作为整体命中", ok)
ok, _ = ev("词1 词2 词3", "词1词2词3")
check("无空格粘连不命中（空格是内容）", not ok)
ok, _ = ev("Bluetooth Manager|HFPDEV", "HFPDEV state=2")
check("带空格条件与其他条件 OR 共存", ok)

print("== 引号 = 字面字符 ==")
ok, words = ev('22|44|"66"', 'knob current controller: "carplay" 1 0')
check("不含 22/44/\"66\" 的行不命中", not ok)
ok, _ = ev('"66"', 'x "66" y')
check('带引号 "66" 命中', ok)
ok, _ = ev('"66"', "x 66 y")
check('裸 66 不命中 "66"（引号是内容）', not ok)
ok, _ = ev('"66"', '-/bin/sh: "66": not found')
check("报错行里的真实引号命中", ok)

print("== 无特殊语法符号 ==")
ok, _ = ev("a && b", "x a && b y")
check("&& 按字面匹配", ok)
ok, _ = ev("a && b", "x a y b")
check("仅含 a、b 不命中字面 'a && b'", not ok)
ok, _ = ev("!a", "x !a y")
check("! 按字面匹配", ok)
ok, _ = ev("test==|platformReady", "xx platformReady yy")
check("原旧写法 \\| 场景下纯 | 表达式仍正常", ok)

print("== 空表达式 ==")
for bad in ["", "   ", "|||", " | "]:
    try:
        ev(bad, "x")
        check("拒绝空表达式 %r" % bad, False)
    except ValueError:
        check("拒绝空表达式 %r" % bad, True)

print("== 大小写 ==")
ok, _ = ev("PlatformReady", "platformready", cs=False)
check("忽略大小写", ok)
ok, _ = ev("PlatformReady", "platformready", cs=True)
check("区分大小写", not ok)

print("== 全词匹配 ==")
ok, _ = ev("66", "abc66 def", ww=True)
check("全词: 前缀粘连不命中", not ok)
ok, _ = ev("66", "abc 661", ww=True)
check("全词: 后缀粘连不命中", not ok)
ok, _ = ev("66", "time .661 end", ww=True)
check("全词: 时间戳 .661 不命中", not ok)
ok, _ = ev("66", "port 66 ok", ww=True)
check("全词: 独立 66 命中", ok)
ok, _ = ev("66", "666", ww=True)
check("全词: 纯粘连不命中", not ok)
ok, _ = ev("66", "abc66 def")
check("关全词: 子串仍命中", ok)
ok, _ = ev("66|22", "value :22 end", ww=True)
check("全词: :22 中 22 不命中（冒号粘连）", not ok)
ok, _ = ev("platformReady", "xx platformReady;", ww=True)
check("全词: 标点边界命中", ok)
ok, _ = ev("66", "端口66", ww=True)
check("全词: 中文邻接按粘连处理", not ok)

print("== 条件 66 与带引号内容 ==")
ok, _ = ev("66", "port 66 ok", ww=True)
check("全词ON: 裸 66 命中", ok)
ok, _ = ev("66", 'log: "66" found', ww=True)
check("全词ON: 带引号 \"66\" 命中（引号不是单词字符）", ok)
ok, _ = ev("66", "value abc66 end", ww=True)
check("全词ON: abc66 粘连不命中", not ok)
ok, _ = ev("66", "value 661 end", ww=True)
check("全词ON: 661 后缀粘连不命中", not ok)
ok, _ = ev("66", "port 66 ok")
check("全词OFF: 裸 66 命中", ok)
ok, _ = ev("66", 'log: "66" found')
check("全词OFF: 带引号 \"66\" 命中", ok)
ok, _ = ev("66", "value abc66 end")
check("全词OFF: abc66 子串命中", ok)

print("== split_or_expr 边界 ==")
check("纯 | 拆分", split_or_expr("a|b|c") == ["a", "b", "c"])
check("|| 合并", split_or_expr("a||b") == ["a", "b"])
check("首尾空格去除", split_or_expr(" a | b ") == ["a", "b"])
check("内部空格保留", split_or_expr("into home|log") == ["into home", "log"])
check("空段过滤", split_or_expr("|a||b|") == ["a", "b"])

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
