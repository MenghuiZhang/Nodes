# -*- coding: utf-8 -*-
"""应用日志落盘：按天轮转，保留 30 天。

日志目录默认 <web-data>/logs（web-data 在 compose 里挂载到宿主机 ./data/web），
可用 NODES_LOG_DIR 环境变量覆盖。write() 同时不负责 stdout——调用方自己决定
是否再打印，避免 CLI 场景重复输出。

跨天自动切换文件 nodes-YYYYMMDD.log；每天首次写入时顺带清理 30 天前的旧文件。
"""

import os
import time
import threading
from pathlib import Path

_RETAIN_DAYS = 30
_lock = threading.Lock()
_current_day = None
_fh = None

# 目录在首次写入时才创建，import 本模块不产生副作用
_dir = Path(os.environ.get("NODES_LOG_DIR")
            or Path(os.environ.get("NODES_WEB_DATA_DIR")
                    or Path(__file__).resolve().parent / "web-data") / "logs")


def logfile_for(day=None):
    return _dir / f"nodes-{day or time.strftime('%Y%m%d')}.log"


def _rotate_locked():
    """切到当天文件（持有 _lock 调用）。返回文件句柄，跨天时关闭旧句柄。"""
    global _current_day, _fh
    day = time.strftime("%Y%m%d")
    if _fh is not None and _current_day == day:
        return _fh
    if _fh is not None:
        try:
            _fh.close()
        except OSError:
            pass
    _dir.mkdir(parents=True, exist_ok=True)
    _fh = open(logfile_for(day), "a", encoding="utf-8")
    _current_day = day
    _cleanup_locked(day)
    return _fh


def _cleanup_locked(today):
    """删除超过保留期的旧日志文件（每天首次写入触发一次）。"""
    import re as _re

    cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - _RETAIN_DAYS * 86400))
    try:
        for p in _dir.glob("nodes-*.log"):
            mo = _re.match(r"nodes-(\d{8})\.log$", p.name)
            if mo and mo.group(1) < cutoff:
                p.unlink(missing_ok=True)
    except OSError:
        pass


def write(msg, tag=""):
    """追加一行到当天日志文件。写盘失败静默（日志不能拖垮业务）。"""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]{tag} {msg}\n"
    try:
        with _lock:
            fh = _rotate_locked()
            fh.write(line)
            fh.flush()
    except OSError:
        pass


def close():
    with _lock:
        global _fh
        if _fh is not None:
            try:
                _fh.close()
            except OSError:
                pass
            _fh = None
