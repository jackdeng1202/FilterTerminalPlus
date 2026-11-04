# -*- coding: utf-8 -*-
"""
FilterTerminalPlus —— FilterTerminal.exe (v0.95191) 的重写版
=========================================================
原版是 2002 年 Borland C++Builder + CPort(TComPort) 写的 MDI 串口终端。
本版用 PySide6 重写，保留其核心使用体验，并修复原版三大顽疾：

1. 拔 USB 串口板 → 原版 EComPort 异常风暴弹框
   修复: errorOccurred(ResourceError) 自动关闭串口，状态栏提示，绝不弹框
2. adb/OTG 模式切换导致串口瞬间掉线 → 同上
   修复: 同 1，另加"自动重连"定时器，设备回来后自动恢复连接
3. 偶发异常 → 原版无全局兜底
   修复: 全局 excepthook + readyRead 逐段 try/except，异常写入日志与状态栏

过滤语法（兼容原版）:
    词1 词2 词3          —— 隐含 OR（词表过滤，最常用）
    a && b               —— AND
    a | b   (或 a || b)  —— OR
    !a      !(a|b)       —— NOT / 分组
    "含 空格 的词"        —— 引号包住含空格的词
    原版写法: 词以 \\ 结尾表示词终止（支持尾部空格），如 test==\\|platformReady \\|...
"""

import json
import os
import re
import sys
import time
import traceback
from collections import deque
from datetime import datetime

from PySide6.QtCore import QByteArray, QEvent, Qt, QTime, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QIcon, QKeySequence, QPalette,
                           QShortcut, QTextCharFormat, QTextCursor)
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                               QDockWidget, QFileDialog, QFormLayout,
                               QGridLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMainWindow, QMenu,
                               QPlainTextEdit, QPushButton, QSizePolicy,
                               QStatusBar, QToolBar, QVBoxLayout, QWidget)

APP_NAME = "FilterTerminalPlus"
# 配置/日志基准目录：打包成 exe 时放 exe 旁边，脚本运行时放脚本旁边
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_APP_DIR, "config.json")
LOG_DIR = _APP_DIR

BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600, 1000000, 1500000, 2000000, 3000000]

# 按词着色调色板（词序号 → 颜色，循环使用）
WORD_COLORS = ["#E24B4A", "#378ADD", "#639922", "#BA7517", "#7F77DD",
               "#1D9E75", "#D4537E", "#888780", "#185FA5", "#993C1D"]

LINE_LIMIT_DEFAULT = 100000
SRC_BUFFER_LIMIT = 50000  # 源行环形缓冲，供过滤窗"重新应用"

# 设备自己打印的内核时间戳（行首形如 "[ 27.152207]" / "[26.622]"），过滤匹配时可选择剔除
KERNEL_TS_RE = re.compile(r"^\s*\[\s*\d+(?:\.\d+)?\]\s*")


def strip_kernel_ts(line):
    """去掉行首的内核时间戳前缀（如 "[ 26.622] "），没有则原样返回。"""
    return KERNEL_TS_RE.sub("", line, count=1)

# 预制快捷命令默认值（与原版 FilterTerminal 注册表 Default 键 Edit00–Edit11 顺序一致）
DEFAULT_QUICK_CMDS = [
    "",                                    # Edit00（原版为空槽）
    "ls -lh",                              # Edit01
    "cat /userdata/nvm/Gplink/RK3568.ini", # Edit02
    "df -h",                               # Edit03
    "cat /sys/devices/platform/fe8a0000.usb2-phy/otg_mode",                       # Edit04
    'echo "peripheral" > /sys/devices/platform/fe8a0000.usb2-phy/otg_mode;/usr/bin/usbdevice start',  # Edit05
    "reboot loader",                       # Edit06
    "reboot",                              # Edit07
    "pkill -9 -f Launcher;./tmp/Launcher &",                                       # Edit08
    "pkill -9 -f Launcher;Launcher &",                                             # Edit09
    "top -b -n 1 -o RES | head -30",                                               # Edit10
    "PID=$(pgrep -f 'Launcher' | head -1); printf \"PID     : %s\\n\" \"$PID\"; "
    "printf \"fd      : %s\\n\" \"$(ls /proc/$PID/fd 2>/dev/null | wc -l)\"; "
    "printf \"threads : %s\\n\" \"$(ls /proc/$PID/task 2>/dev/null | wc -l)\"; "
    "printf \"VmRSS   : %s kB\\n\" \"$(grep VmRSS /proc/$PID/status 2>/dev/null | awk '{print $2}')\"; "
    "printf \"loadavg : %s\\n\" \"$(cat /proc/loadavg)\"",                          # Edit11
]


# ============================================================
# 过滤表达式：仅按 | 拆分为多个字面条件，条件原样匹配
# （空格、引号等都是条件内容的一部分；想要 OR 就写 |，无其他语法）
# ============================================================
class Node:
    __slots__ = ("kind", "kids", "word")
    def __init__(self, kind, kids=None, word=None):
        self.kind, self.kids, self.word = kind, kids or [], word


def split_or_expr(expr):
    """按 | 拆分表达式为多个字面条件：
    - 条件原样保留内部空格与引号等所有字符（"into home" 就是含空格的整体，
      '"66"' 匹配内容里真实存在的引号）
    - 仅去掉每个条件首尾的空白（写 'a | b' 与 'a|b' 等价）
    - 连续多个 |（如 a||b）视为一个 |"""
    parts = re.sub(r"\|{2,}", "|", expr).split("|")
    return [p.strip() for p in parts if p.strip()]


def _is_word_char(ch):
    """单词字符：字母/数字/下划线，另加 ':' 与 '.'。
    这样 10:22:33、192.168.1.22、3.22 里的 22 不会被"全词 22"命中
    （冒号/小数点紧贴的数字视为连在一起的一个词）。"""
    return ch.isalnum() or ch in "_:."


def _word_in_line(word, line, case_sensitive, whole_word):
    """word 是否出现在 line 中；whole_word 时要求命中处前后都不是单词字符
    （词本身首/尾若不是单词字符，如引号，则该侧不做边界要求）"""
    if not whole_word:
        if case_sensitive:
            return word in line
        return word.lower() in line.lower()
    h = line if case_sensitive else line.lower()
    n = word if case_sensitive else word.lower()
    if not n:
        return True
    ln = len(n)
    start = 0
    while True:
        i = h.find(n, start)
        if i < 0:
            return False
        before_ok = i == 0 or not _is_word_char(h[i - 1]) or not _is_word_char(n[0])
        j = i + ln
        after_ok = j >= len(h) or not _is_word_char(h[j]) or not _is_word_char(n[-1])
        if before_ok and after_ok:
            return True
        start = i + 1


def compile_filter(expr, case_sensitive=False, whole_word=False):
    """编译表达式为谓词函数(line)->bool；同时返回 (words, tree) 供着色用。
    语法：条件1 | 条件2 | 条件3 —— 命中任一条件即通过；每个条件是字面子串，
    空格/引号都按内容原样匹配（没有 AND、括号、引号分组等语法）。"""
    words = split_or_expr(expr)
    if not words:
        raise ValueError("空表达式")

    def ev(_node, line):
        return any(_word_in_line(w, line, case_sensitive, whole_word) for w in words)

    tree = Node("or", [Node("word", word=w) for w in words]) if len(words) > 1 \
        else Node("word", word=words[0])
    return ev, words, tree


def first_matching_word_index(words, line, case_sensitive, whole_word=False):
    """返回第一个命中词在 words 里的序号（用于按词着色），未命中返回 None。
    words 已按表达式出现顺序排好（compile_filter 收集），无需重复走树。"""
    for idx, w in enumerate(words):
        if _word_in_line(w, line, case_sensitive, whole_word):
            return idx
    return None


class FilterOutputView(QPlainTextEdit):
    """过滤窗输出区：点击 = 通知主窗口暂停自动滚动。"""
    on_press = None  # 由 FilterDock 注入回调

    def mousePressEvent(self, ev):
        if self.on_press:
            try:
                self.on_press()
            except Exception:
                pass
        super().mousePressEvent(ev)


class QuickPanel(QWidget):
    """快捷命令面板：点击空白处（行间隙/底部空白/F 标签）取消命令输入框的焦点。"""

    def mousePressEvent(self, ev):
        fw = QApplication.focusWidget()
        if fw is not None:
            try:
                fw.clearFocus()
            except RuntimeError:
                pass
        super().mousePressEvent(ev)


# ============================================================
# 过滤窗（Dock）
# ============================================================
class FilterDock(QDockWidget):
    """一个过滤窗 = 表达式 + 开关 + 着色输出。行增量过滤，可全量重放。"""

    def __init__(self, number, parent=None, expr="", case_sensitive=False, whole=False):
        super().__init__("Filter %d" % number, parent)
        self.number = number
        self.setObjectName("filterDock%d" % number)  # QMainWindow.saveState 靠 objectName 匹配布局
        self._main = parent  # 主窗口引用（浮动/全屏时 self.window() 会变成 Dock 自己）
        self._closed = False  # 用户点了标题栏 X
        self.setAllowedAreas(Qt.AllDockWidgetAreas)
        self.setFeatures(QDockWidget.DockWidgetMovable |
                         QDockWidget.DockWidgetClosable |
                         QDockWidget.DockWidgetFloatable)

        self.expr_edit = QLineEdit(expr)
        self.expr_edit.setPlaceholderText("条件1 | 条件2 | 条件3（| 为 OR；条件原样匹配，空格/引号算内容）")
        self.case_chk = QCheckBox("区分大小写")
        self.case_chk.setChecked(case_sensitive)
        self.whole_chk = QCheckBox("全词")
        self.whole_chk.setChecked(whole)
        self.whole_chk.setToolTip(
            "全词匹配：命中处两侧不能是字母/数字/下划线/冒号/小数点\n"
            "（如全词 22 不命中 abc661、10:22:33、192.168.1.22，但命中 22、(22)、count=22）")
        self.reapply_btn = QPushButton("重放历史")
        self.reapply_btn.setToolTip("从源缓冲（最近 5 万行）按当前表达式全量重建本过滤窗")
        self.reapply_btn.setFixedSize(64, 22)
        self.fs_btn = QPushButton("全屏")
        self.fs_btn.setToolTip("本过滤窗独占主窗口右侧（其他过滤窗临时隐藏）；再次点击还原排列")
        self.fs_btn.setFixedSize(44, 22)

        bar = QWidget()
        vbox = QVBoxLayout(bar)
        vbox.setContentsMargins(4, 4, 4, 2)
        vbox.setSpacing(3)
        # 第一行：表达式铺满标题右侧剩余空间
        row1 = QHBoxLayout()
        row1.setSpacing(6)
        row1.addWidget(QLabel("表达式:"))
        row1.addWidget(self.expr_edit, 1)
        # 第二行：区分大小写与全词紧挨着；重放历史/全屏固定小尺寸靠右
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        row2.addWidget(self.case_chk)
        row2.addWidget(self.whole_chk)
        row2.addStretch(1)
        row2.addWidget(self.reapply_btn)
        row2.addWidget(self.fs_btn)
        vbox.addLayout(row1)
        vbox.addLayout(row2)

        self.out = FilterOutputView()
        self.out.setReadOnly(True)
        self.out.on_press = self._notify_press  # 点击输出区 = 暂停自动滚动
        self.out.setMaximumBlockCount(LINE_LIMIT_DEFAULT)
        f = QFont("Consolas", 10)
        f.setStyleHint(QFont.Monospace)
        self.out.setFont(f)

        lay = QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(bar)
        lay.addWidget(self.out)
        w = QWidget()
        w.setLayout(lay)
        self.setWidget(w)

        self.compiled = None   # (ev, words, tree)
        self._fs_restore = None  # 全屏前是否浮动
        self._fs_geo = None      # 全屏前的浮动几何
        self.recompile()
        self._last_sig = (self.expr_edit.text(), self.case_chk.isChecked(),
                          self.whole_chk.isChecked())  # 变更检测基线

        self.expr_edit.returnPressed.connect(self.on_expr_changed)
        self.expr_edit.editingFinished.connect(self.on_expr_changed)
        self.case_chk.toggled.connect(self.on_expr_changed)
        self.whole_chk.toggled.connect(self.on_expr_changed)
        self.reapply_btn.clicked.connect(self.request_reapply)
        self.fs_btn.clicked.connect(self.toggle_fullscreen)

    def toggle_fullscreen(self):
        """全屏 = 本过滤窗独占主窗口右侧（其他过滤窗临时隐藏）；
        还原 = 恢复之前的排列模式（上下分割/标签），若是全屏前浮动的则回到浮动状态。"""
        win = self._main
        if not isinstance(win, MainWindow):
            return
        if self._fs_restore is None:
            try:
                was_floating = self.isFloating()
                geo = self.saveGeometry() if was_floating else None
            except Exception:
                was_floating, geo = False, None
            self._fs_restore = was_floating
            if was_floating and geo:
                self._fs_geo = geo
                self.setFloating(False)
                win.addDockWidget(Qt.RightDockWidgetArea, self)
            for d in win.filter_docks():
                if d is not self:
                    d.hide()
            self.show()
            # 独占右侧整列高度
            win.resizeDocks([self], [max(win.height(), 200)], Qt.Vertical)
            self.fs_btn.setText("还原")
        else:
            was_floating = self._fs_restore
            self._fs_restore = None
            # 重新按当前排列模式布局全部过滤窗（隐藏的会重新显示）
            win._apply_arrangement()
            if was_floating and getattr(self, "_fs_geo", None):
                self.setFloating(True)
                self.restoreGeometry(self._fs_geo)
                scr = QApplication.primaryScreen().availableGeometry()
                g = self.geometry().intersected(scr)
                if g.size() != self.size():
                    self.setGeometry(g)
                self.show()
            self.fs_btn.setText("全屏")

    # ---- 编译与过滤 ----
    def recompile(self):
        expr = self.expr_edit.text().strip()
        # 不再显示"OK: N 个词"提示；表达式错误用红框 + 悬停提示表达
        if not expr:
            self.compiled = None
            self._set_expr_error("")
            return True
        try:
            ev, words, tree = compile_filter(expr, self.case_chk.isChecked(), self.whole_chk.isChecked())
            self.compiled = (ev, words, tree)
            self._set_expr_error("")
            return True
        except ValueError as e:
            self.compiled = None
            self._set_expr_error("表达式错误: %s" % e)
            return False

    def _set_expr_error(self, msg):
        if msg:
            self.expr_edit.setToolTip(msg)
            self.expr_edit.setStyleSheet("border: 1px solid #E24B4A;")
        else:
            self.expr_edit.setToolTip("")
            self.expr_edit.setStyleSheet("")

    def on_expr_changed(self):
        # 重编译总是执行（很便宜，同时刷新错误红框）；
        # 但只有表达式/开关真的变化才重放——否则点一下别处（editingFinished）就全量重放 5 万行
        sig = (self.expr_edit.text(), self.case_chk.isChecked(),
               self.whole_chk.isChecked())
        changed = sig != getattr(self, "_last_sig", None)
        if self.recompile():
            self._last_sig = sig
            if changed:
                self.request_reapply()

    def closeEvent(self, ev):
        # 用户点标题栏 X 关闭过滤窗 → 从存活列表移除（不再喂数据/不占过滤窗配额/不存进配置）
        self._closed = True
        win = self._main
        if isinstance(win, MainWindow):
            win._dock_keep = [d for d in win._dock_keep if d is not self]
        super().closeEvent(ev)

    def request_reapply(self):
        win = self._main
        if isinstance(win, MainWindow):
            win.reapply_filter(self)

    def _notify_press(self):
        win = self._main
        if isinstance(win, MainWindow):
            win._pause_follow()

    def feed(self, raw_text, typed_text):
        """raw_text: 原始日志内容（无 FTPlus 时间戳前缀，用于匹配）；
        typed_text: 带时间戳的显示行。匹配前剔除行首内核时间戳（如 "[ 26.622] "），
        只对真正内容过滤。增量过滤，批量缓冲刷新。"""
        win = self._main
        if self.compiled is None:
            item = (typed_text, None)
        else:
            ev, words, tree = self.compiled
            mtext = strip_kernel_ts(raw_text)
            if not ev(tree, mtext):
                return
            idx = first_matching_word_index(words, mtext,
                                            self.case_chk.isChecked(), self.whole_chk.isChecked())
            item = (typed_text, WORD_COLORS[idx % len(WORD_COLORS)] if idx is not None else None)
        if isinstance(win, MainWindow):
            win._push_to_view(self.out, item)  # 批量缓冲（高波特率下逐行插入 UI 会拖垮接收）
        else:
            self.append_colored(item[0], item[1])

    def _match_text(self, raw_text):
        """返回参与匹配的文本：剔除行首内核时间戳，只过滤真正内容。"""
        return strip_kernel_ts(raw_text)

    def append_colored(self, text, word_idx):
        color = WORD_COLORS[word_idx % len(WORD_COLORS)] if word_idx is not None else None
        cur = self.out.textCursor()
        cur.movePosition(QTextCursor.End)
        if color:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            cur.insertText(text + "\n", fmt)
        else:
            cur.insertText(text + "\n")
        self.out.setTextCursor(cur)


# ============================================================
# 终端视图：连接状态下键盘直通设备（q 退出 top/less、Ctrl+C 中断、方向键翻历史）
# ============================================================
class TerminalView(QPlainTextEdit):
    sendBytes = Signal(bytes)

    def mousePressEvent(self, ev):
        # 点击日志区 = 暂停自动滚动（方便回看/选中文本），点命令输入框恢复
        win = self.window()
        if isinstance(win, MainWindow):
            win._pause_follow()
        super().mousePressEvent(ev)

    def keyPressEvent(self, ev):
        key, mods = ev.key(), ev.modifiers()
        # Ctrl+D = 本地清屏（无论是否连接），不再当 EOF 发给设备；过滤窗输出一并清空
        if (mods & Qt.ControlModifier) and key == Qt.Key_D:
            win = self.window()
            if isinstance(win, MainWindow):
                win._clear_screens()
            else:
                self.clear()
            return
        win = self.window()
        # Shift+ESC = 把真正的 ESC 字节发给设备（vim/less 退出用）；
        # 普通 ESC 的连接/断开互切由主窗口全局快捷键处理（任何焦点位置都生效）
        if key == Qt.Key_Escape and (mods & Qt.ShiftModifier) and not (mods & Qt.ControlModifier):
            if isinstance(win, MainWindow) and win.connected:
                self.sendBytes.emit(b"\x1b")
            return
        if not getattr(win, "connected", False):
            super().keyPressEvent(ev)
            return
        text = ev.text()
        if mods & Qt.ControlModifier:
            if key == Qt.Key_C and (mods & Qt.ShiftModifier):
                self.copy()  # 复制选中文本
                return
            if key == Qt.Key_V:  # 粘贴：把剪贴板文本作为输入发给设备（换行转回车）
                clip = QApplication.clipboard().text()
                if clip:
                    clip = clip.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r")
                    self.sendBytes.emit(clip.encode("utf-8", "replace"))
                return
            ctrl_map = {Qt.Key_C: b"\x03",  # SIGINT
                        Qt.Key_U: b"\x15",  # 清行
                        Qt.Key_L: b"\x0c",  # 清屏
                        Qt.Key_Z: b"\x1a",  # SIGTSTP
                        Qt.Key_A: b"\x01",  # 行首
                        Qt.Key_E: b"\x05",  # 行尾
                        Qt.Key_W: b"\x17",  # 删前一个词
                        Qt.Key_K: b"\x0b",  # 删到行尾
                        Qt.Key_B: b"\x02",  # 左移一字
                        Qt.Key_F: b"\x06"}  # 右移一字
            if key in ctrl_map:
                self.sendBytes.emit(ctrl_map[key])
                return
            super().keyPressEvent(ev)
            return
        if Qt.Key_F1 <= key <= Qt.Key_F12:
            win._quick_send(key - Qt.Key_F1)
            return
        key_map = {Qt.Key_Return: b"\r", Qt.Key_Enter: b"\r",
                   Qt.Key_Backspace: b"\x7f",
                   Qt.Key_Up: b"\x1b[A", Qt.Key_Down: b"\x1b[B",
                   Qt.Key_Right: b"\x1b[C", Qt.Key_Left: b"\x1b[D",
                   Qt.Key_Home: b"\x1b[H", Qt.Key_End: b"\x1b[F",
                   Qt.Key_PageUp: b"\x1b[5~", Qt.Key_PageDown: b"\x1b[6~",
                   Qt.Key_Delete: b"\x1b[3~", Qt.Key_Insert: b"\x1b[2~"}
        if key in key_map:
            self.sendBytes.emit(key_map[key])
            return
        if text:
            self.sendBytes.emit(text.encode("utf-8"))
            return
        super().keyPressEvent(ev)


# ============================================================
# 主窗口
# ============================================================
class HistoryCombo(QComboBox):
    """命令历史下拉框：弹出的列表加淡蓝色边框，并与命令框留出间距。
    ↑/↓ 在任何焦点路径（输入框/弹出列表/下拉框本身）下行为一致。"""

    on_arrow_up = None    # 由 MainWindow 注入：↑ = 翻历史
    on_arrow_down = None  # 由 MainWindow 注入：↓ = 反向翻回新命令

    def __init__(self):
        super().__init__()
        self.view().setStyleSheet(
            "QListView { border: 2px solid #86B7E8; border-radius: 4px;"
            "            padding: 3px; outline: none;"
            "            background: palette(base); color: palette(text); }")

    def keyPressEvent(self, ev):
        # 焦点落在下拉框本体（如点过箭头后）时，↑/↓ 默认会移动 currentIndex——
        # 与历史导航冲突，这里统一接管
        if ev.key() == Qt.Key_Up and self.on_arrow_up:
            self.on_arrow_up()
            return
        if ev.key() == Qt.Key_Down and self.on_arrow_down:
            self.on_arrow_down()
            return
        super().keyPressEvent(ev)

    def showPopup(self):
        super().showPopup()
        vp = self.view()
        # 列表与命令框之间留 6px 间距。注意：必须移动弹出的顶层容器框，
        # 不能移动容器内部的视图（视图被推出容器边框会把首行/末行裁掉）
        container = vp.parentWidget()
        scr = QApplication.primaryScreen().availableGeometry()
        c_top = container.mapToGlobal(container.rect().topLeft()).y()
        c_bottom = container.mapToGlobal(container.rect().bottomLeft()).y()
        combo_top = self.mapToGlobal(self.rect().topLeft()).y()
        combo_bottom = self.mapToGlobal(self.rect().bottomLeft()).y()
        if c_top >= combo_bottom - 2 and c_bottom + 6 <= scr.bottom() + 1:
            container.move(container.x(), container.y() + 6)   # 下方弹出：整体下让 6px
        elif c_bottom <= combo_top + 2 and c_top - 6 >= scr.top():
            container.move(container.x(), container.y() - 6)   # 上方弹出：整体上让 6px


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        # 主窗口不超过屏幕可用区域
        scr = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1280, scr.width()), min(800, scr.height()))
        self._first_show_done = False

        self.serial = None
        self.connected = False
        self.t_start = None
        self.rx_buf = b""
        self.src_buffer = deque(maxlen=SRC_BUFFER_LIMIT)  # (raw, sent, rel, wall) 供重放/重格式化
        self.log_file = None
        self.pending_views = {}   # view -> [lines] 批量刷新缓冲
        self.filter_counter = 0
        self.quick_cmds = list(DEFAULT_QUICK_CMDS)
        self.hist_idx = -1
        self.dark_theme = True
        self.rx_bytes = 0
        self.rx_first_logged = False
        self.last_rx_time = None
        self.idle_warned = False
        self._status_tick = 0
        self._dock_keep = []   # 持有过滤窗的 Python 引用，防止包装器被 GC 连带删除 C++ 对象
        self._auto_follow = True  # 自动滚动跟随最新；点击日志区暂停，点命令输入框恢复

        self._build_ui()
        self._build_serial()
        self.load_config()

        # 批量刷新定时器：高波特率下把 N 行合并成一次 UI 更新
        self.flush_timer = QTimer(self)
        self.flush_timer.setInterval(60)
        self.flush_timer.timeout.connect(self._flush_views)
        self.flush_timer.start()

        self.reconn_timer = QTimer(self)
        self.reconn_timer.setInterval(2000)
        self.reconn_timer.timeout.connect(self._try_reconnect)
        self.reconn_timer.start()  # 常驻轮询，内部自行判断是否需要重连
        self._manual_closed = True   # 初始未连接视为手动状态，不自动重连
        self._reconnect_attempts = 0

    # ---------- UI ----------
    def _build_ui(self):
        # 终端控件先建，工具栏信号才能引用
        self.term = TerminalView()
        self.term.sendBytes.connect(self.send_raw)
        self.term.setReadOnly(True)
        self.term.setMaximumBlockCount(LINE_LIMIT_DEFAULT)
        f = QFont("Consolas", 10)
        f.setStyleHint(QFont.Monospace)
        self.term.setFont(f)

        tb = QToolBar("连接")
        tb.setMovable(False)
        self.addToolBar(tb)

        self.port_combo = QComboBox(); self.port_combo.setMinimumWidth(110)
        self.baud_combo = QComboBox(); self.baud_combo.addItems([str(b) for b in BAUD_RATES])
        self.baud_combo.setCurrentText("115200")
        self.data_combo = QComboBox(); self.data_combo.addItems(["8", "7", "6", "5"])
        self.parity_combo = QComboBox(); self.parity_combo.addItems(["无", "偶", "奇"])
        self.stop_combo = QComboBox(); self.stop_combo.addItems(["1", "2"])
        self.btn_refresh = QPushButton("刷新端口")
        self.btn_connect = QPushButton("连接")
        self.ts_chk = QCheckBox("时间戳(相对)")
        self.ts_abs_chk = QCheckBox("绝对")
        self.ts_abs_chk.setChecked(True)
        self.reconn_chk = QCheckBox("自动重连")
        self.btn_log = QPushButton("开始记录日志")
        self.btn_filter = QPushButton("+ 过滤窗")
        self.btn_arrange = QPushButton("排列:上下")
        self.btn_arrange.setToolTip("切换右侧过滤窗排列：上下分割 ↔ 标签式（每个独占右侧）")
        self.btn_clear = QPushButton("清屏")
        self.btn_export = QPushButton("导出配置")
        self.btn_import = QPushButton("导入配置")
        self.btn_theme = QPushButton("主题:暗")
        self.font_up = QPushButton("A+"); self.font_dn = QPushButton("A-")

        for wdg in (QLabel("端口:"), self.port_combo, QLabel("波特:"), self.baud_combo,
                    QLabel("数据位:"), self.data_combo, QLabel("校验:"), self.parity_combo,
                    QLabel("停止位:"), self.stop_combo,
                    self.btn_refresh, self.btn_connect, self.ts_chk, self.ts_abs_chk,
                    self.reconn_chk, self.btn_log, self.btn_filter, self.btn_arrange, self.btn_clear,
                    self.btn_export, self.btn_import, self.btn_theme,
                    self.font_up, self.font_dn):
            tb.addWidget(wdg)

        self.btn_refresh.clicked.connect(self.refresh_ports)
        self.btn_connect.clicked.connect(self.toggle_connect)
        self.btn_log.clicked.connect(self.toggle_log)
        self.btn_filter.clicked.connect(self.add_filter_dock)
        self.btn_arrange.clicked.connect(self.toggle_arrange)
        self.btn_clear.clicked.connect(self._clear_screens)
        self.btn_export.clicked.connect(self.export_config)
        self.btn_import.clicked.connect(self.import_config)
        self.btn_theme.clicked.connect(self.toggle_theme)
        self.font_up.clicked.connect(lambda: self._bump_font(+1))
        self.font_dn.clicked.connect(lambda: self._bump_font(-1))
        self.reconn_chk.toggled.connect(lambda *_: None)  # 状态由 _try_reconnect 直接读 reconn_chk
        self.ts_chk.toggled.connect(lambda *_: self.reapply_all())

        # 命令行 + 历史
        self.cmd_combo = HistoryCombo()
        self.cmd_combo.setEditable(True)
        self.cmd_combo.setMaxCount(40)           # 最多保存 40 条
        self.cmd_combo.setMaxVisibleItems(10)    # 下拉列表最多显示 10 行，超出滚动
        self.cmd_combo.setInsertPolicy(QComboBox.NoInsert)
        self.cmd_edit = self.cmd_combo.lineEdit()
        self.cmd_edit.setCompleter(None)   # 关闭自动补齐：候选弹层会抢走 ↑/Enter 键，导致历史导航错乱
        self.cmd_combo.setCompleter(None)
        self.cmd_edit.setPlaceholderText("输入命令回车发送（↑ 翻旧 / ↓ 翻新，点右侧箭头看列表）")
        self.cmd_edit.returnPressed.connect(self.send_cmd)
        self.cmd_combo.activated.connect(self._on_hist_picked)  # 鼠标点选列表项后，↑ 从该项继续往旧翻
        self.cmd_combo.on_arrow_up = self._hist_up              # 焦点在下拉框本体时的 ↑
        self.cmd_combo.on_arrow_down = self._hist_down          # 焦点在下拉框本体时的 ↓
        self.cmd_combo.installEventFilter(self)             # 点下拉框本体也能恢复自动滚动
        self.cmd_combo.view().installEventFilter(self)      # 列表弹开时也接管 ↑/↓，行为与输入框一致
        self.btn_send = QPushButton("发送")

        bottom = QWidget()
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(4, 2, 4, 2)
        bl.addWidget(QLabel("命令:"))
        bl.addWidget(self.cmd_combo, 1)
        bl.addWidget(self.btn_send)
        self.btn_send.clicked.connect(self.send_cmd)

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.term, 1)
        v.addWidget(bottom)
        self.setCentralWidget(central)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("未连接")

        self.cmd_edit.installEventFilter(self)  # ↑/↓ 历史导航
        self._build_quick_dock()
        self.refresh_ports()

    def _bump_font(self, d):
        f = self.term.font()
        f.setPointSize(max(6, min(24, f.pointSize() + d)))
        self.term.setFont(f)
        for d2 in self.findChildren(FilterDock):
            d2.out.setFont(f)

    # ---------- 窗口尺寸约束 ----------
    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._first_show_done:
            self._first_show_done = True
            # 无保存布局时才套默认宽度（快捷命令面板 = 主窗口 1/6）；有保存布局则原样恢复
            if not getattr(self, "_saved_state", None):
                QTimer.singleShot(0, lambda: self.resizeDocks(
                    [self.quick_dock], [max(160, self.width() // 6)], Qt.Horizontal))
            self._clamp_to_screen()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        # 预置命令面板最大宽度 = 主窗口 1/3（小窗口下也不能超过 1/3）
        if getattr(self, "quick_dock", None) is not None and self.quick_dock.widget():
            self.quick_dock.widget().setMaximumWidth(max(100, self.width() // 3))

    def _clamp_to_screen(self):
        scr = QApplication.primaryScreen().availableGeometry()
        g = self.geometry().intersected(scr)
        if g.size() != self.size():
            self.setGeometry(g)

    # ---------- 串口（修复点 1/2：掉线自动关闭，零弹框） ----------
    def _build_serial(self):
        self.serial = QSerialPort(self)
        self.serial.readyRead.connect(self.on_ready_read)
        self.serial.errorOccurred.connect(self.on_serial_error)

    def refresh_ports(self):
        cur = self.port_combo.currentText()
        self.port_combo.clear()
        for info in QSerialPortInfo.availablePorts():
            self.port_combo.addItem(info.portName())
        if cur:
            self.port_combo.setCurrentText(cur)

    def toggle_connect(self):
        if self.connected:
            self.close_port("手动断开")
        else:
            self.open_port()

    def open_port(self, quiet=False):
        try:
            name = self.port_combo.currentText().strip()
            if not name:
                self.refresh_ports()
                if not quiet:
                    self.status.showMessage("没有可用串口")  # 常驻，直到下一次操作
                    self._applog("open fail: no ports")
                return False
            avail = [i.portName() for i in QSerialPortInfo.availablePorts()]
            if name not in avail:
                if not quiet:
                    self.status.showMessage("端口 %s 不存在（设备可能已拔出）" % name)
                    self._applog("open fail: %s not in %s" % (name, avail))
                return False
            self.serial.setPortName(name)
            self.serial.setBaudRate(int(self.baud_combo.currentText()))
            self.serial.setDataBits({"8": QSerialPort.Data8, "7": QSerialPort.Data7,
                                     "6": QSerialPort.Data6, "5": QSerialPort.Data5}[self.data_combo.currentText()])
            self.serial.setParity({"无": QSerialPort.NoParity, "偶": QSerialPort.EvenParity,
                                   "奇": QSerialPort.OddParity}[self.parity_combo.currentText()])
            self.serial.setStopBits({"1": QSerialPort.OneStop, "2": QSerialPort.TwoStop}[self.stop_combo.currentText()])
            self.serial.setFlowControl(QSerialPort.NoFlowControl)
            if not self.serial.open(QSerialPort.ReadWrite):
                err = "%s (error=%s)" % (self.serial.errorString(), self.serial.error())
                if not quiet:
                    self.status.showMessage("打开 %s 失败: %s" % (name, err))  # 常驻
                    self._applog("open fail: %s: %s" % (name, err))
                return False
            self.serial.clear()
            self.rx_buf = b""  # 清空上次连接的半行残留，避免拼进新连接开头
            self.connected = True
            self._manual_closed = False
            self._reconnect_attempts = 0
            self.t_start = time.monotonic()
            self.rx_bytes = 0
            self.rx_first_logged = False
            self.last_rx_time = None
            self.idle_warned = False
            self._status_tick = 0
            self.btn_connect.setText("断开")
            self.status.showMessage("已连接 %s @ %s" % (name, self.baud_combo.currentText()), 5000)
            self._applog("connected %s @ %s" % (name, self.baud_combo.currentText()))
            return True
        except Exception:
            self._report_exception("open_port")
            self.status.showMessage("打开串口时内部异常，详见 ftplus_crash.log")
            return False

    def close_port(self, reason, manual=True):
        if self.serial is not None and getattr(self.serial, "isOpen", lambda: False)():
            try:
                self.serial.close()
            except Exception:
                pass
        self.connected = False
        self._manual_closed = manual  # 手动断开 → 不自动重连；设备消失 → 允许自动重连
        self.btn_connect.setText("连接")
        self.status.showMessage("%s（串口已关闭）" % reason)
        self.log_line("[FTPlus] disconnected: %s" % reason)

    def on_serial_error(self, error):
        # 修复核心：资源错误（拔板/OTG切换/驱动卸载）→ 自动关闭，不弹任何框
        try:
            if error in (QSerialPort.ResourceError, QSerialPort.DeviceNotFoundError,
                         QSerialPort.PermissionError):
                if self.connected or self.serial.isOpen():
                    self.close_port("串口设备已消失（拔出/驱动重枚举）", manual=False)
                return
            if error != QSerialPort.NoError and self.connected:
                err = self.serial.errorString()
                self.status.showMessage("串口错误: %s" % err, 5000)
                self.log_line("[FTPlus] serial error: %s" % err)
        except Exception:
            self._report_exception("on_serial_error")

    def _try_reconnect(self):
        if self.connected or self._manual_closed:
            return
        if not self.reconn_chk.isChecked():
            return
        self._reconnect_attempts += 1
        self.open_port(quiet=True)  # 后台重连：不刷状态栏/不刷日志
        if self.connected:
            self._reconnect_attempts = 0
        elif self._reconnect_attempts % 5 == 0:
            self.status.showMessage("自动重连中…（第 %d 次）" % self._reconnect_attempts)
            self._applog("auto-reconnect attempt #%d failed" % self._reconnect_attempts)

    # ---------- 数据流 ----------
    def on_ready_read(self):
        try:
            data = bytes(self.serial.readAll())
            if data:
                self.rx_bytes += len(data)
                self.last_rx_time = time.monotonic()
                self.idle_warned = False
                if not self.rx_first_logged:
                    self.rx_first_logged = True
                    self._applog("first data after connect: %d bytes" % len(data))
            self.rx_buf += data
            while b"\n" in self.rx_buf:
                raw, self.rx_buf = self.rx_buf.split(b"\n", 1)
                line = raw.rstrip(b"\r").decode("utf-8", "replace")
                self.process_line(line, sent=False)
        except Exception:
            self._report_exception("on_ready_read")

    def _fmt_line(self, raw, sent, rel, wall):
        """按当前时间戳设置格式化一行。rel/wall 是收到该行时的时间快照。"""
        if self.ts_chk.isChecked() and rel is not None:
            ts = rel + " "
        elif self.ts_abs_chk.isChecked() and wall:
            ts = wall + " "
        else:
            ts = ""
        return ts + (">> " if sent else "") + raw

    def process_line(self, text, sent=False):
        t_mono = time.monotonic()
        rel = ("%8.2f" % (t_mono - self.t_start)) if self.t_start is not None else None
        wall = QTime.currentTime().toString("HH:mm:ss.zzz")
        typed = self._fmt_line(text, sent, rel, wall)
        self.src_buffer.append((text, sent, rel, wall))  # 元组，重放时按当前设置重新格式化
        self._push_to_view(self.term, (typed, None))
        for dock in self.filter_docks():
            dock.feed(text, typed)
        self.log_line(text)

    def _push_to_view(self, view, item):
        """item = (行文本, 颜色或 None)。批量缓冲，由 _flush_views 统一插入 UI。"""
        self.pending_views.setdefault(view, []).append(item)

    # ---------- 自动滚动暂停/恢复 ----------
    def _pause_follow(self):
        if self._auto_follow:
            self._auto_follow = False
            self.status.showMessage("自动滚动已暂停（点击命令输入框恢复）", 4000)

    def _resume_follow(self):
        self._auto_follow = True
        self._jump_to_bottom()  # 立即跳到最新，不等下一条日志

    def _jump_to_bottom(self):
        """终端 + 全部过滤窗立即滚到最新。"""
        views = [self.term] + [d.out for d in self.filter_docks()]
        for v in views:
            try:
                cur = v.textCursor()
                cur.movePosition(QTextCursor.End)
                v.setTextCursor(cur)
                sb = v.verticalScrollBar()
                sb.setValue(sb.maximum())
            except RuntimeError:
                continue

    def _clear_screens(self):
        """清空终端 + 全部过滤窗，并丢弃批量缓冲中尚未刷新的行
        （否则已清掉的行会在下一次 flush 时复活）。"""
        self.pending_views.pop(self.term, None)
        self.term.clear()
        for d in self.filter_docks():
            self.pending_views.pop(d.out, None)
            d.out.clear()

    def _batch_append(self, view, pairs):
        """批量把 (行文本, 颜色) 追加到视图；只在自动滚动跟随时光标移到底部。"""
        cur = view.textCursor()
        cur.movePosition(QTextCursor.End)
        for line, color in pairs:
            if color:
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(color))
                cur.insertText(line + "\n", fmt)
            else:
                cur.insertText(line + "\n")
        if self._auto_follow:
            view.setTextCursor(cur)

    def _flush_views(self):
        if self.pending_views:
            for view, items in list(self.pending_views.items()):
                self.pending_views.pop(view, None)
                try:
                    sb = view.verticalScrollBar()
                    pos = sb.value()  # 暂停滚动时保持当前位置
                    if view is self.term:
                        cur = self.term.textCursor()
                        cur.movePosition(QTextCursor.End)
                        cur.insertText("\n".join(l for l, _ in items) + "\n")
                        if self._auto_follow:
                            self.term.setTextCursor(cur)  # 跟随到底部
                        else:
                            sb.setValue(pos)  # 暂停：内容照常追加但不滚动
                    else:
                        # 过滤窗输出（带颜色），同样批量插入（高波特率下性能关键）
                        cur = view.textCursor()
                        cur.movePosition(QTextCursor.End)
                        for line, color in items:
                            if color:
                                fmt = QTextCharFormat()
                                fmt.setForeground(QColor(color))
                                cur.insertText(line + "\n", fmt)
                            else:
                                cur.insertText(line + "\n")
                        if self._auto_follow:
                            view.setTextCursor(cur)
                        else:
                            sb.setValue(pos)
                except RuntimeError:
                    continue  # 视图已被销毁（删过滤窗等），丢弃缓冲
        if self.log_file:
            try:
                self.log_file.flush()  # 日志也改为批量刷盘（原来每行 flush 一次，高波特率下拖慢接收）
            except Exception:
                pass
        # 连接状态下的接收计数（约 1s 刷一次，避免闪烁）
        if self.connected:
            self._status_tick += 1
            if self._status_tick % 17 == 0:
                idle = None
                if self.last_rx_time is not None:
                    idle = time.monotonic() - self.last_rx_time
                elif self.t_start is not None:
                    idle = time.monotonic() - self.t_start
                rx = self.rx_bytes
                text = "已连接 %s @ %s | 收到 %s" % (
                    self.port_combo.currentText(), self.baud_combo.currentText(),
                    ("%.1f KB" % (rx / 1024)) if rx < 1024 * 1024 else ("%.2f MB" % (rx / 1024 / 1024)))
                if idle is not None and idle > 10:
                    text += " | 已 %.0f 秒无数据" % idle
                    if not self.idle_warned:
                        self.idle_warned = True
                        self._applog("no data for >10s on %s (检查波特率/设备是否在发)" % self.port_combo.currentText())
                self.status.showMessage(text)

    def filter_docks(self):
        """存活的过滤窗（引用序列里的；已被 deleteLater 且包装器失效的自动剔除）"""
        alive = []
        for d in getattr(self, "_dock_keep", []):
            try:
                d.windowTitle()
                alive.append(d)
            except RuntimeError:
                continue
        return alive

    def reapply_filter(self, dock):
        """从源缓冲全量重放（匹配用原始行；显示行按当前时间戳设置重新格式化）。
        批量插入：5 万行逐行 setTextCursor 会卡死 UI；且丢弃缓冲避免重复。"""
        self.pending_views.pop(dock.out, None)  # 丢弃缓冲中未刷新的行，避免重放后末尾重复
        dock.out.clear()
        pairs = []
        if dock.compiled is None:
            for raw, sent, rel, wall in self.src_buffer:
                pairs.append((self._fmt_line(raw, sent, rel, wall), None))
        else:
            ev, words, tree = dock.compiled
            cs = dock.case_chk.isChecked()
            ww = dock.whole_chk.isChecked()
            for raw, sent, rel, wall in self.src_buffer:
                mtext = strip_kernel_ts(raw)  # 匹配前剔除内核时间戳，只对真正内容过滤
                if ev(tree, mtext):
                    idx = first_matching_word_index(words, mtext, cs, ww)
                    pairs.append((self._fmt_line(raw, sent, rel, wall),
                                  WORD_COLORS[idx % len(WORD_COLORS)] if idx is not None else None))
        if pairs:
            self._batch_append(dock.out, pairs)

    def reapply_all(self):
        for d in self.filter_docks():
            self.reapply_filter(d)

    def add_filter_dock(self):
        if len(self.filter_docks()) >= 8:
            self.status.showMessage("过滤窗太多会影响性能（原版同款限制）", 4000)
            return
        self.filter_counter += 1
        n = self.filter_counter
        dock = FilterDock(n, self)
        self._dock_keep.append(dock)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        self.apply_theme(self.dark_theme)  # 新过滤窗跟随当前主题
        self._apply_arrangement()          # 融入当前排列模式
        self.status.showMessage("已新建 Filter %d" % n, 3000)

    # ---------- 过滤窗排列模式：上下分割 ↔ 右侧标签独占 ----------
    def toggle_arrange(self):
        self.arrange_mode = "tabs" if getattr(self, "arrange_mode", "split") == "split" else "split"
        self.btn_arrange.setText("排列:标签" if self.arrange_mode == "tabs" else "排列:上下")
        self._apply_arrangement()

    def _apply_arrangement(self):
        """把全部过滤窗按当前模式重排进右侧：split=上下分割；tabs=每个独占右侧（标签切换）"""
        docks = self.filter_docks()
        if not docks:
            return
        for d in docks:
            d.setFloating(False)
            self.removeDockWidget(d)
        self.addDockWidget(Qt.RightDockWidgetArea, docks[0])
        if getattr(self, "arrange_mode", "split") == "tabs":
            for d in docks[1:]:
                self.tabifyDockWidget(docks[0], d)
        else:
            for d in docks[1:]:
                self.splitDockWidget(docks[0], d, Qt.Vertical)
        for d in docks:
            d.show()
        docks[0].raise_()

    # ---------- 发送 ----------
    def send_cmd(self):
        text = self.cmd_edit.text()
        self.hist_idx = -1
        if not text:
            return
        ok = self._send_text(text, add_history=True)
        if ok:
            self.cmd_combo.setCurrentText("")

    def _send_text(self, text, add_history):
        """发送一行命令；add_history=False 时不写入历史列表（快捷键命令不打扰历史）。
        只有真正发出去的命令才进历史（未连接/写入失败不记录）。"""
        self._resume_follow()  # 发送命令 = 回到最新（点发送按钮/快捷键时焦点不在输入框）
        if not self.connected:
            self.status.showMessage("未连接，命令未发送", 3000)
            return False
        payload = (text + "\r\n").encode("utf-8", "replace")
        try:
            written = self.serial.write(payload)
        except Exception:
            self._report_exception("_send_text")
            self.status.showMessage("写入失败", 4000)
            return False
        if written < 0:
            self.status.showMessage("写入失败: %s" % self.serial.errorString(), 4000)
            return False
        for ln in text.split("\n"):
            self.process_line(ln, sent=True)
        if add_history:
            # 历史去重 + 置底（最近发送的排在最后，↑ 第一档就是它）
            dup = self.cmd_combo.findText(text)
            if dup >= 0:
                self.cmd_combo.removeItem(dup)
            while self.cmd_combo.count() >= self.cmd_combo.maxCount():
                self.cmd_combo.removeItem(0)  # 超过 40 条时移除最旧的
            self.cmd_combo.addItem(text)
        return True

    # ---------- 日志 ----------
    def toggle_log(self):
        if self.log_file:
            self.log_file.close()
            self.log_file = None
            self.btn_log.setText("开始记录日志")
            self.status.showMessage("日志已停止", 3000)
        else:
            name = os.path.join(LOG_DIR, datetime.now().strftime("%Y%m%d-%H%M%S") + "-ftplus.log")
            try:
                self.log_file = open(name, "a", encoding="utf-8")
            except Exception:
                self.log_file = None
                self._report_exception("toggle_log")
                self.status.showMessage("无法创建日志文件，记录未开始", 6000)
                return
            self.btn_log.setText("停止日志")
            self.status.showMessage("记录日志: %s" % name, 5000)

    def log_line(self, text):
        if self.log_file:
            try:
                self.log_file.write(text + "\n")
                # 不逐行 flush：_flush_views 每 60ms 批量刷盘，高波特率下逐行刷盘会拖慢接收
            except Exception:
                pass

    def _applog(self, msg):
        """常驻诊断日志：不管是否开了'记录日志'，关键事件都写 ftplus_app.log"""
        try:
            with open(os.path.join(LOG_DIR, "ftplus_app.log"), "a", encoding="utf-8") as fp:
                fp.write(datetime.now().strftime("%H:%M:%S ") + msg + "\n")
        except Exception:
            pass

    # ---------- 命令历史 ↑/↓ 导航（行内切换，不弹列表；下拉列表只靠箭头打开） ----------
    def _hist_show(self, idx):
        """直接把历史第 idx 条写进输入框。不依赖 setCurrentIndex——
        目标 index 与当前 index 相同时它是 no-op，文本不更新，会导致方向键错乱。"""
        self.hist_idx = idx
        text = self.cmd_combo.itemText(idx)
        self.cmd_edit.setText(text)
        self.cmd_combo.setCurrentIndex(idx)  # 仅同步下拉列表高亮，no-op 也无妨

    def _hist_up(self):
        """↑ = 前一个输入的命令，每按一次更旧一条（严格最新→最旧）。
        当前文本若已不是导航所选项（中途手改过/刚发送），重新从最新开始。"""
        n = self.cmd_combo.count()
        if not n:
            return
        if not (0 <= self.hist_idx < n) or self.cmd_edit.text() != self.cmd_combo.itemText(self.hist_idx):
            self._hist_show(n - 1)
        else:
            self._hist_show(max(0, self.hist_idx - 1))

    def _hist_down(self):
        """↓ = 滚轮反向：往更新的命令翻，越过最新一条 → 清空回输入状态。"""
        if not (0 <= self.hist_idx < self.cmd_combo.count()):
            return  # 不在导航中（输入状态/刚发送），没有"更新"可翻
        nxt = self.hist_idx + 1
        if nxt >= self.cmd_combo.count():
            self.hist_idx = -1
            self.cmd_edit.clear()
            self.cmd_combo.setCurrentIndex(-1)
        else:
            self._hist_show(nxt)

    def _on_hist_picked(self, idx):
        """鼠标在下拉列表里点选了一条历史 → 同步导航位置，↑ 从这条继续往旧翻。"""
        self.hist_idx = idx

    def eventFilter(self, obj, ev):
        # 点击命令框（输入框或下拉框本体）获得焦点 = 恢复自动滚动
        if ev.type() == QEvent.FocusIn and obj in (self.cmd_edit, self.cmd_combo):
            self._resume_follow()
        if obj is self.cmd_edit and ev.type() == QEvent.KeyPress:
            if ev.key() == Qt.Key_Up:
                self._hist_up()
                return True
            if ev.key() == Qt.Key_Down:
                self._hist_down()  # 滚轮反向：往更新的命令翻，越过最新清空输入框
                return True
            if ev.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape):
                self.hist_idx = -1
        elif obj is self.cmd_combo.view() and ev.type() == QEvent.KeyPress:
            # 下拉列表弹开时，↑/↓ 默认由列表接管（只移动高亮条并改输入框文本），
            # 与历史导航冲突。这里统一接管：↑/↓ = 关列表并按滚轮方式翻历史。
            if ev.key() == Qt.Key_Up:
                self.cmd_combo.hidePopup()
                self._hist_up()
                return True
            if ev.key() == Qt.Key_Down:
                self.cmd_combo.hidePopup()
                self._hist_down()
                return True
        return super().eventFilter(obj, ev)

    # ---------- 原始字节发送（终端键盘透传用） ----------
    def send_raw(self, data):
        if not self.connected:
            self.status.showMessage("未连接，按键未发送", 2000)
            return
        try:
            if self.serial.write(data) < 0:
                self.status.showMessage("写入失败: %s" % self.serial.errorString(), 4000)
        except Exception:
            self._report_exception("send_raw")

    # ---------- 预制快捷命令面板 ----------
    def _build_quick_dock(self):
        dock = QDockWidget("快捷命令 (F1–F12)", self)
        dock.setObjectName("quickDock")  # 布局保存/恢复用
        dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable)
        w = QuickPanel()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(6)  # 行间距
        self.quick_btns = []
        for i in range(12):
            # 一行一个命令：F 键标签 + 按钮
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(4)
            lab = QLabel("F%d" % (i + 1))
            lab.setMinimumWidth(24)
            btn = QPushButton()
            btn.setToolTip("点击或按 F%d 发送；右键编辑/清空" % (i + 1))
            btn.setStyleSheet("text-align: left; padding: 5px 6px;")  # 命令文本左对齐 + 上下加高
            btn.setMinimumHeight(28)
            # 横向 Ignored：长命令不撑大面板最小宽度（否则 300 字符命令会把窗口顶出屏幕）
            btn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            btn.setMinimumWidth(48)
            btn.clicked.connect(lambda _=False, idx=i: self._quick_send(idx))
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(lambda pos, idx=i, b=btn: self._quick_menu(idx, b))
            h.addWidget(lab)
            h.addWidget(btn, 1)
            v.addWidget(row)
            self.quick_btns.append(btn)
            # F1–F12 全局快捷键（终端内由 TerminalView 转发，其他焦点走这里）
            sc = QShortcut(QKeySequence("F%d" % (i + 1)), self)
            sc.activated.connect(lambda idx=i: self._quick_send(idx))
        v.addStretch(1)
        dock.setWidget(w)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self.quick_dock = dock
        self._update_quick_btns()
        # ESC = 连接/断开互切（窗口级全局快捷键）：焦点在快捷面板空白处、过滤窗、
        # 输入框等任何位置都生效；Shift+ESC 发送 ESC 字节仍在终端内处理
        sc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        sc.activated.connect(self.toggle_connect)

    def _quick_label(self, cmd):
        cmd = cmd.strip()
        return cmd or "(空)"   # 不打点，显示全文；超宽由面板自适应/悬停提示兜底

    def _update_quick_btns(self):
        for i, btn in enumerate(self.quick_btns):
            cmd = self.quick_cmds[i] if i < len(self.quick_cmds) else ""
            text = self._quick_label(cmd)
            btn.setText(text)
            btn.setToolTip((cmd.strip() or "(空)") + "\n\n点击或按 F%d 发送；右键编辑/清空" % (i + 1))
        # 不锁最小宽度：面板可自由拖窄/拖宽；窄时按钮文本被 Qt 自动省略，悬停看全文

    def _quick_send(self, idx):
        cmd = self.quick_cmds[idx] if idx < len(self.quick_cmds) else ""
        if not cmd:
            self._quick_menu(idx, self.quick_btns[idx])
            return
        # 快捷键命令：直接发送，不写入历史列表、不进 ↑ 回放，也不动输入框内容
        self._send_text(cmd, add_history=False)

    def _quick_menu(self, idx, btn):
        menu = QMenu(self)
        menu.addAction("编辑命令…", lambda: self._quick_edit(idx))
        menu.addAction("清空", lambda: self._quick_clear(idx))
        menu.exec(btn.mapToGlobal(btn.rect().center()))

    def _quick_edit(self, idx):
        cur = self.quick_cmds[idx] if idx < len(self.quick_cmds) else ""
        text, ok = QInputDialog.getText(self, "快捷命令 %d" % (idx + 1),
                                        "点击按钮即发送的命令:", text=cur)
        if ok:
            while len(self.quick_cmds) <= idx:
                self.quick_cmds.append("")
            self.quick_cmds[idx] = text.strip()
            self._update_quick_btns()
            self.save_config()  # 编辑后立即持久化

    def _quick_clear(self, idx):
        if idx < len(self.quick_cmds):
            self.quick_cmds[idx] = ""
            self._update_quick_btns()
            self.save_config()

    # ---------- 主题 ----------
    def toggle_theme(self):
        self.apply_theme(not self.dark_theme)

    def _set_term_colors(self, view, bg, fg):
        pal = view.palette()
        pal.setColor(QPalette.Base, QColor(bg))
        pal.setColor(QPalette.Text, QColor(fg))
        view.setPalette(pal)

    def apply_theme(self, dark):
        self.dark_theme = dark
        app = QApplication.instance()
        app.setStyle("Fusion")
        if dark:
            p = QPalette()
            p.setColor(QPalette.Window, QColor("#2b2d31"))
            p.setColor(QPalette.WindowText, QColor("#e3e5e8"))
            p.setColor(QPalette.Base, QColor("#17191d"))
            p.setColor(QPalette.AlternateBase, QColor("#202226"))
            p.setColor(QPalette.Text, QColor("#dfe3e8"))
            p.setColor(QPalette.Button, QColor("#2f3136"))
            p.setColor(QPalette.ButtonText, QColor("#e3e5e8"))
            p.setColor(QPalette.Highlight, QColor("#378ADD"))
            p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
            p.setColor(QPalette.ToolTipBase, QColor("#2b2d31"))
            p.setColor(QPalette.ToolTipText, QColor("#e3e5e8"))
            p.setColor(QPalette.Link, QColor("#85B7EB"))
            app.setPalette(p)
            bg, fg = "#101418", "#D6E2EE"
            self.btn_theme.setText("主题:暗")
        else:
            app.setPalette(app.style().standardPalette())
            bg, fg = "#ffffff", "#14181c"
            self.btn_theme.setText("主题:亮")
        self._set_term_colors(self.term, bg, fg)
        for d in self.filter_docks():
            self._set_term_colors(d.out, bg, fg)

    # ---------- 配置导出/导入 ----------
    def export_config(self):
        name, _ = QFileDialog.getSaveFileName(self, "导出配置", "ftplus_config.json",
                                              "JSON (*.json)")
        if not name:
            return
        try:
            with open(name, "w", encoding="utf-8") as fp:
                json.dump(self._config_dict(), fp, ensure_ascii=False, indent=2)
            self.status.showMessage("配置已导出: %s" % name, 5000)
        except Exception:
            self._report_exception("export_config")

    def import_config(self):
        name, _ = QFileDialog.getOpenFileName(self, "导入配置", "",
                                              "JSON (*.json);;所有文件 (*)")
        if not name:
            return
        try:
            with open(name, "r", encoding="utf-8") as fp:
                cfg = json.load(fp)
            if not isinstance(cfg, dict):
                raise ValueError("配置文件根节点必须是 JSON 对象")
        except Exception as e:
            self.status.showMessage("导入失败: %s" % e, 6000)
            return
        try:
            self.apply_config(cfg)
        except Exception:
            self._report_exception("import_config")
            self.status.showMessage("导入失败：配置内容无效，详见 ftplus_crash.log", 6000)
            return
        self.save_config()
        self.status.showMessage("配置已导入: %s" % name, 5000)

    # ---------- 配置 ----------
    def _config_dict(self):
        return {
            "port": self.port_combo.currentText(),
            "baud": self.baud_combo.currentText(),
            "ts_relative": self.ts_chk.isChecked(),
            "ts_absolute": self.ts_abs_chk.isChecked(),
            "auto_reconnect": self.reconn_chk.isChecked(),
            "dark_theme": self.dark_theme,
            "history": [self.cmd_combo.itemText(i) for i in range(self.cmd_combo.count())],
            "quick_commands": list(self.quick_cmds),
            "filter_layout": getattr(self, "arrange_mode", "split"),
            "win_geometry": bytes(self.saveGeometry().toBase64()).decode("ascii"),
            "win_state": bytes(self.saveState().toBase64()).decode("ascii"),
            "filters": [{"expr": d.expr_edit.text(), "case": d.case_chk.isChecked(),
                         "whole": d.whole_chk.isChecked()}
                        for d in self.filter_docks()],
        }

    def apply_config(self, cfg):
        self.port_combo.setCurrentText(cfg.get("port", ""))
        self.baud_combo.setCurrentText(str(cfg.get("baud", "115200")))
        self.ts_chk.setChecked(cfg.get("ts_relative", False))
        self.ts_abs_chk.setChecked(cfg.get("ts_absolute", True))
        self.reconn_chk.setChecked(cfg.get("auto_reconnect", True))
        self.hist_idx = -1
        self.cmd_combo.clear()
        # 历史去重（保留最后出现位置 = 最新），保证 ↑ 从最新到最旧严格有序
        hist = cfg.get("history", [])
        seen, dedup = set(), []
        for h in reversed(hist):
            if h and h not in seen:
                seen.add(h)
                dedup.append(h)
        self.cmd_combo.addItems(list(reversed(dedup)))
        qc = cfg.get("quick_commands")
        self.quick_cmds = (list(qc) + [""] * 12)[:12] if qc else list(DEFAULT_QUICK_CMDS)
        self._update_quick_btns()
        for d in self.filter_docks():
            self.removeDockWidget(d)
            d.deleteLater()
        self._dock_keep = []
        self.filter_counter = 0
        for i, fcfg in enumerate(cfg.get("filters", []), start=1):
            self.filter_counter = i
            dock = FilterDock(i, self, fcfg.get("expr", ""), fcfg.get("case", False),
                              fcfg.get("whole", False))
            self._dock_keep.append(dock)
            self.addDockWidget(Qt.RightDockWidgetArea, dock)
        self.arrange_mode = cfg.get("filter_layout", "split")
        self.btn_arrange.setText("排列:标签" if self.arrange_mode == "tabs" else "排列:上下")
        self._apply_arrangement()
        if not self.filter_docks():
            self.add_filter_dock()
        self.apply_theme(bool(cfg.get("dark_theme", True)))
        # 恢复窗口大小/位置与全部停靠布局（含过滤窗位置、大小、浮动状态、工具栏）
        self._saved_state = None
        geo = cfg.get("win_geometry", "")
        st = cfg.get("win_state", "")
        if geo:
            try:
                self.restoreGeometry(QByteArray.fromBase64(geo.encode("ascii")))
            except Exception:
                pass
        if st:
            try:
                self._saved_state = QByteArray.fromBase64(st.encode("ascii"))
                self.restoreState(self._saved_state)
                self._sync_arrange_btn()  # 布局可能被 state 覆盖，重新探测排列模式
            except Exception:
                self._saved_state = None

    def _sync_arrange_btn(self):
        """根据当前过滤窗是否处于标签模式，同步排列按钮的文字与内部状态。"""
        docks = self.filter_docks()
        tabbed = False
        if len(docks) > 1:
            for d in docks:
                if any(t in docks for t in self.tabifiedDockWidgets(d)):
                    tabbed = True
                    break
        self.arrange_mode = "tabs" if tabbed else "split"
        self.btn_arrange.setText("排列:标签" if tabbed else "排列:上下")

    def load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
                cfg = json.load(fp)
        except Exception:
            cfg = {}
        self.apply_config(cfg)

    def save_config(self):
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return  # offscreen 是自动化测试环境，绝不污染真实 config.json
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
                json.dump(self._config_dict(), fp, ensure_ascii=False, indent=2)
        except Exception:
            self._report_exception("save_config")

    # ---------- 异常兜底（修复点 3） ----------
    def _report_exception(self, where):
        try:
            info = "".join(traceback.format_exception(*sys.exc_info()))
            self._applog("[EXC] %s\n%s" % (where, info))
            self.log_line("[FTPlus][EXC] %s\n%s" % (where, info))
            self.status.showMessage("内部异常(%s)，已记录日志，程序继续运行" % where, 8000)
        except Exception:
            pass

    def closeEvent(self, ev):
        self.save_config()
        self.flush_timer.stop()
        self.reconn_timer.stop()
        if self.log_file:
            self.log_file.close()
            self.log_file = None
        if self.serial and self.serial.isOpen():
            self.serial.close()
        super().closeEvent(ev)


def global_excepthook(etype, value, tb):
    try:
        with open(os.path.join(LOG_DIR, "ftplus_crash.log"), "a", encoding="utf-8") as fp:
            fp.write("".join(traceback.format_exception(etype, value, tb)) + "\n" + "=" * 60 + "\n")
    except Exception:
        pass


def main():
    sys.excepthook = global_excepthook
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
