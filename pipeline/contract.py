# -*- coding: utf-8 -*-
"""素材统一契约 + 流水线状态机。

所有素材来源（本地文件夹 / JSON / SQLite / HTTP API）都先归一成 Material，
后面的生成、投递、回写只认这一种结构，换素材系统时只改 sources.py 的适配器。

状态机： new -> composed -> drafted -> published
                   -> failed（可重试）
同一 id 只允许发布一次；publish 前必须查 state.json 去重。
"""
import json
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE, "state.json")

STATES = ("new", "composed", "drafted", "published", "failed")


@dataclass
class Material:
    id: str
    title: str
    body_md: str = ""
    summary: str = ""
    author: str = ""
    cover: str = ""                       # 本地路径或 http(s) URL
    images: list = field(default_factory=list)   # 与正文 {{IMG:n}} 对应
    tags: list = field(default_factory=list)
    source: str = ""                      # 来源系统标识，回写时用
    created_at: str = ""
    extra: dict = field(default_factory=dict)

    def validate(self):
        errs = []
        if not self.id.strip():
            errs.append("id 为空")
        if not self.title.strip():
            errs.append("title 为空")
        if len(self.title) > 64:
            errs.append("title 超过 64 字（微信限制）")
        if len(self.summary) > 120:
            errs.append("summary 超过 120 字（微信限制）")
        if not (self.body_md or "").strip() and not self.extra.get("body_html"):
            errs.append("body_md / body_html 均为空")
        return errs

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = {k: d.get(k) for k in cls.__dataclass_fields__ if k in d}
        m = cls(**known)
        m.id = str(m.id)
        m.title = str(m.title)
        m.images = list(m.images or [])
        m.tags = list(m.tags or [])
        m.extra = dict(m.extra or {})
        m.extra.update({k: v for k, v in d.items()
                        if k not in cls.__dataclass_fields__})
        return m


class State:
    """线程安全的发布台账。文件小、读写不频繁，直接用进程锁 + 原子替换。"""

    _lock = threading.Lock()

    def __init__(self, path=STATE_PATH):
        self.path = path
        self.data = {"version": 1, "items": {}, "updated_at": ""}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data = json.load(f)
                self.data.setdefault("items", {})
            except (ValueError, OSError):
                pass

    def get(self, mid):
        return self.data["items"].get(str(mid), {"status": "new"})

    def set(self, mid, status, **kw):
        if status not in STATES:
            raise ValueError("未知状态: %s" % status)
        with self._lock:
            rec = self.data["items"].get(str(mid), {})
            rec.update({"status": status, "updated_at": _now()})
            rec.update({k: v for k, v in kw.items() if v is not None})
            self.data["items"][str(mid)] = rec
            self._flush()
        return rec

    def pop(self, mid):
        """摘除并备份一条台账记录（删除素材时用），返回原记录或 None。"""
        with self._lock:
            rec = self.data["items"].pop(str(mid), None)
            if rec is not None:
                self._flush()
            return rec

    def put(self, mid, rec):
        """整条写回（回收站还原时用）。"""
        with self._lock:
            self.data["items"][str(mid)] = dict(rec)
            self._flush()
        return rec

    def count_published_on(self, day=None):
        day = day or datetime.now().strftime("%Y-%m-%d")
        return sum(1 for r in self.data["items"].values()
                   if r.get("status") == "published"
                   and str(r.get("published_at", "")).startswith(day))

    def _flush(self):
        self.data["updated_at"] = _now()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
