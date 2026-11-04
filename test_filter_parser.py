# -*- coding: utf-8 -*-
"""过滤表达式解析器/求值器单元测试（不依赖 Qt）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from filterterminal_plus import compile_filter  # noqa: E402

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

print("== 兼容原版 \\| 词表语法 ==")
ok, words = ev(r"test==\|platformReady \|into home\|STAT=", "xx platformReady yy")
check("原版 \\| OR 命中", ok and words == ["test==", "platformReady ", "into home", "STAT="])
ok, _ = ev(r"test==\|platformReady \|into home", "nothing here")
check("原版 \\| OR 不命中", not ok)
ok, words = ev(r"Bluetooth Manager\|HFPDEV", "HFPDEV state=2")
check("词含空格", ok and words == ["Bluetooth Manager", "HFPDEV"])

print("== 新语法 ==")
ok, words = ev("audio mic", "audio dump")
check("空格 AND：只含 audio 不命中", (not ok) and words == ["audio", "mic"])
ok, _ = ev("audio mic", "mic only")
check("空格 AND：只含 mic 不命中", not ok)
ok, _ = ev("audio mic", "audio and mic both")
check("空格 AND：两词都有命中", ok)
ok, _ = ev("!(this|that)", "has other")
check("NOT 分组命中", ok)
ok, _ = ev("!(this|that)", "has that")
check("NOT 分组不命中", not ok)
ok, _ = ev("platformReady ", "xx platformReady yy")  # 尾部空格词
check("尾部空格词（引号外 \\| 场景）", ok)

print("== 引号 = 字面字符 ==")
ok, words = ev('22|44|"66"', 'knob current controller: "carplay" 1 0')
check("时间戳外的 carplay 行不含 22/44/\"66\" → 不命中", not ok)
ok, _ = ev('"66"', 'x "66" y')
check("带引号 \"66\" 命中", ok)
ok, _ = ev('"66"', "x 66 y")
check("裸 66 不命中 \"66\"", not ok)
ok, _ = ev('a && "66"', 'a "66" b')
check("引号词 + AND", ok)
ok, _ = ev("a && b", "a x b")
check("AND 命中", ok)

print("== 混合优先级 ==")
ok, _ = ev("a b && c", "a b c")
check("a b && c 全有命中", ok)
ok, _ = ev("a b && c", "a && c")
check("a b && c 缺 b 不命中", not ok)
ok, _ = ev("(a|b) && c", "b && c")
check("(a|b)&&c 命中", ok)
ok, _ = ev("(a|b) && c", "d && c")
check("(a|b)&&c 不命中", not ok)
ok, _ = ev("a b && c", "b only")
check("(a|b)&&c 不命中", not ok)
ok, _ = ev("!a && b", "x b")
check("!a && b 命中", ok)
ok, _ = ev("!a && b", "a b")
check("!a && b 不命中", not ok)
ok, _ = ev("(a && b) | c", "just c")
check("显式括号 OR", ok)

print("== 大小写 ==")
ok, _ = ev("PlatformReady", "platformready", cs=False)
check("忽略大小写", ok)
ok, _ = ev("PlatformReady", "platformready", cs=True)
check("区分大小写", not ok)

print("== 错误处理 ==")
for bad in ["(a|b", "a &&", "&& b", "a & b"]:
    try:
        ev(bad, "x")
        check("拒绝 %r" % bad, False)
    except ValueError:
        check("拒绝 %r" % bad, True)

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
check("关全词: 子串仍命中(向后兼容)", ok)
ok, _ = ev('"66"', '-/bin/sh: "66": not found', ww=True)
check("全词: 带引号词照常工作", ok)
ok, _ = ev("platformReady", "xx platformReady;", ww=True)
check("全词: 标点边界命中", ok)
ok, _ = ev("66", "端口66", ww=True)
check("全词: 中文邻接按粘连处理", not ok)

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
