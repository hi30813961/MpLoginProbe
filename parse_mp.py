# -*- coding: utf-8 -*-
"""把 MpLoginProbe 抓到的 /cgi-bin/ 响应解析成结构化数据（纯只读，不发任何请求）。

输入：responses/all.jsonl、nav_history.txt、status.txt、events-v1.log（可选）
输出：parsed/ 下的 articles.json / drafts.json / accounts.json / profile.json
      columns.json / summary.md

用法：python parse_mp.py
"""
import ast
import datetime
import json
import os
import re
from collections import OrderedDict, defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "parsed")
os.makedirs(OUT, exist_ok=True)

COLUMN_NAMES = [
    ("/cgi-bin/home", "首页"),
    ("/cgi-bin/appmsgpublish", "已发表 / 发表记录"),
    ("/cgi-bin/appmsg?", "草稿箱 / 图文素材"),
    ("/cgi-bin/filepage", "素材库"),
    ("/misc/appmsgcomment", "留言管理"),
    ("/cgi-bin/mpunderline", "划线管理"),
    ("/cgi-bin/message", "消息管理"),
    ("/cgi-bin/atfunc", "被 @ 提醒"),
    ("/cgi-bin/frame", "通知中心"),
    ("/cgi-bin/settingpage", "账号设置"),
    ("/cgi-bin/component_unauthorize", "第三方授权管理"),
    ("/acct/wxverifyorder", "微信认证 / 年审"),
]


def ts(v):
    """Unix 秒 -> 本地时间字符串；异常返回空串。"""
    try:
        n = int(v)
        if n <= 0 or n < 1_000_000_000:
            return ""
        return datetime.datetime.fromtimestamp(n).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def dump(name, obj):
    p = os.path.join(OUT, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return p


def load_records():
    recs = []
    p = os.path.join(BASE, "responses", "all.jsonl")
    if not os.path.exists(p):
        return recs
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                b = json.loads(r["body"])
                r["data"] = b
                recs.append(r)
            except Exception:
                continue
    return recs


def pick(recs, api, pred=None):
    """取某个接口最近一次满足条件的响应。"""
    for r in reversed(recs):
        if r.get("api") == api and (pred is None or pred(r)):
            return r["data"]
    return None


def as_obj(v):
    """publish_page / publish_info 等字段常被再包一层 JSON 字符串。"""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v if isinstance(v, (dict, list)) else {}


def as_list(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            try:
                v = ast.literal_eval(v)   # 微信后台有些字段是 Python repr
            except Exception:
                return []
    return v if isinstance(v, list) else []


def main():
    recs = load_records()

    # ── 已发表 ──
    articles = []
    pub = pick(recs, "appmsgpublish", lambda r: "sub=list" in r.get("url", ""))
    if pub:
        page = as_obj(pub.get("publish_page"))
        for item in as_list(page.get("publish_list")):
            outer = as_obj(item.get("publish_info"))
            inner = as_obj(outer.get("publish_info"))
            articles.append({
                "msgid": inner.get("msgid") or outer.get("msgid"),
                "draft_msgid": inner.get("draft_msgid"),
                "publish_type": inner.get("publish_type"),
                "publish_status": inner.get("publish_status"),
                "service_type": inner.get("service_type"),
                "index_count": inner.get("index_count"),
                "create_time": ts(inner.get("create_time")),
                "update_time": ts(inner.get("update_time")),
                "title": "",
            })
    titles = {}
    # 列表接口里带标题（v1 日志、以及后台偶尔主动请求的 list_sent_appmsg）
    for r in recs:
        if r.get("api") == "appmsg" and "list_sent_appmsg" in r.get("url", ""):
            for it in as_list(r["data"].get("list")):
                titles[str(it.get("appmsgid", ""))] = it.get("title", "")
    v1 = os.path.join(BASE, "events-v1.log")
    if os.path.exists(v1):
        with open(v1, encoding="utf-8") as f:
            for line in f:
                if "list_sent_appmsg" not in line:
                    continue
                m = re.search(r'"body":"((?:[^"\\]|\\.)*)"', line)
                if not m:
                    continue
                try:
                    b = json.loads('"' + m.group(1) + '"')
                    for it in as_list(json.loads(b).get("list")):
                        titles[str(it.get("appmsgid", ""))] = it.get("title", "")
                except Exception:
                    pass
    for a in articles:
        a["title"] = titles.get(str(a["msgid"]), "")

    # ── 草稿 / 图文素材 ──
    drafts = []
    am = pick(recs, "appmsg", lambda r: r["data"].get("app_msg_info", {}).get("item"))
    if am:
        info = am.get("app_msg_info", {})
        for it in info.get("item", []):
            mi = as_list(it.get("multi_item"))
            sub = []
            for x in mi:
                if not isinstance(x, dict):
                    continue
                sub.append({
                    "title": x.get("title", ""),
                    "digest": (x.get("digest", "") or "")[:200],
                    "cover": x.get("cover", ""),
                    "file_id": x.get("file_id"),
                    "share_page_type": x.get("share_page_type"),
                })
            drafts.append({
                "app_id": it.get("app_id"),
                "title": it.get("title", ""),
                "digest": (it.get("digest", "") or "")[:200],
                "create_time": ts(it.get("create_time")),
                "update_time": ts(it.get("update_time")),
                "img_url": it.get("img_url", ""),
                "item_count": len(sub),
                "items": sub,
            })
    # 用草稿标题补已发表文章标题
    for a in articles:
        if not a["title"] and drafts:
            a["title"] = drafts[0]["title"]

    counts = (am or {}).get("app_msg_info", {}).get("file_cnt", {})

    # ── 可切换账号 ──
    accounts = []
    sw = pick(recs, "switchacct", lambda r: r["data"].get("biz_list", {}).get("list"))
    if sw:
        for x in sw.get("biz_list", {}).get("list", []):
            accounts.append({
                "nickname": x.get("nickname"),
                "username": x.get("username"),
                "bizuin": x.get("bizuin"),
                "acct_type": x.get("acct_type"),
                "is_admin": x.get("is_admin"),
                "is_test_acct": x.get("is_test_acct"),
                "last_login_time": ts(x.get("last_login_time")),
                "headimgurl": x.get("headimgurl"),
            })

    # ── 账号画像（只取白名单字段，绝不写入 ticket / 凭据）──
    profile = {}
    at = pick(recs, "atfunc", lambda r: r["data"].get("user_info"))
    if at:
        u = at.get("user_info", {})
        keep = ["nick_name", "real_nick_name", "user_name", "appid", "fake_id",
                "service_type", "is_wx_verify", "verify_status", "user_role",
                "mass_send_left", "realname_status", "create_time", "account_version"]
        for k in keep:
            v = u.get(k)
            if k == "create_time":
                v = ts(v)
            profile[k] = v
        acl = at.get("user_acl", {}).get("base_acl", {})
        profile["acl_highlights"] = {k: acl.get(k) for k in
                                     ["can_mass_send", "can_use_advanced_func",
                                      "can_use_selfmenu", "can_use_comment",
                                      "can_use_reward", "can_use_album",
                                      "can_use_copyright", "can_dev_apply"]
                                     if k in acl}
    profile["type_note"] = ("service_type=1 订阅号；is_wx_verify=0 未认证。"
                            "按微信规则，未认证/个人主体的 freepublish 发布接口权限已被回收，"
                            "因此 WebView 人机协作是主要发布通道。")

    # ── 栏目访问记录 ──
    nav = []
    for line in open(os.path.join(BASE, "nav_history.txt"), encoding="utf-8"):
        parts = line.rstrip("\n").split("\t", 1)
        if len(parts) != 2:
            continue
        t, url = parts
        name = next((n for pat, n in COLUMN_NAMES if pat in url), "其他")
        path = url.split("?")[0].replace("https://mp.weixin.qq.com", "")
        nav.append({"time": t, "column": name, "path": path, "url": url})
    stat = OrderedDict()
    for x in nav:
        k = x["column"]
        if k not in stat:
            stat[k] = {"visits": 0, "first": x["time"], "last": x["time"], "paths": set()}
        s = stat[k]
        s["visits"] += 1
        s["last"] = x["time"]
        s["paths"].add(x["path"])
    columns = [{"column": k, "visits": v["visits"], "first": v["first"],
                "last": v["last"], "paths": sorted(v["paths"])} for k, v in stat.items()]

    # ── 接口清单 ──
    api_hits = defaultdict(int)
    for r in recs:
        api_hits[r.get("api", "?")] += 1

    dump("articles.json", {"count": len(articles), "items": articles})
    dump("drafts.json", {"file_cnt": counts, "count": len(drafts), "items": drafts})
    dump("accounts.json", {"count": len(accounts), "items": accounts})
    dump("profile.json", profile)
    dump("columns.json", {"total_nav": len(nav), "columns": columns, "history": nav})
    dump("api_hits.json", dict(sorted(api_hits.items(), key=lambda x: -x[1])))

    # ── 可读摘要 ──
    L = []
    L.append("# 公众号后台只读抓包摘要\n")
    L.append("生成时间：%s   （数据源：responses/all.jsonl + nav_history.txt，纯本地只读解析）\n"
             % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    L.append("\n## 账号\n")
    L.append("| 项 | 值 |\n|---|---|")
    for k, label in [("nick_name", "昵称"), ("user_name", "原始 ID"),
                     ("appid", "AppID"), ("service_type", "service_type"),
                     ("is_wx_verify", "微信认证"), ("verify_status", "认证状态"),
                     ("mass_send_left", "剩余群发次数"), ("create_time", "注册时间")]:
        L.append("| %s | %s |" % (label, profile.get(k, "")))
    L.append("\n> %s\n" % profile["type_note"])
    L.append("\n## 可切换账号（%d）\n" % len(accounts))
    L.append("| 昵称 | 原始 ID | bizuin | 管理员 |\n|---|---|---|---|")
    for a in accounts:
        L.append("| %s | %s | %s | %s |" % (a["nickname"], a["username"],
                                            a["bizuin"], a["is_admin"]))
    L.append("\n## 已发表（%d）\n" % len(articles))
    L.append("| 标题 | msgid | 发表时间 | 状态 |\n|---|---|---|---|")
    for a in articles:
        L.append("| %s | %s | %s | %s |" % (a["title"] or "(标题未抓到)",
                                            a["msgid"], a["create_time"],
                                            a["publish_status"]))
    L.append("\n## 草稿 / 图文素材（%d）\n" % len(drafts))
    L.append("| 标题 | 创建时间 | 更新时间 | 图文篇数 |\n|---|---|---|---|")
    for d in drafts:
        L.append("| %s | %s | %s | %d |" % (d["title"], d["create_time"],
                                            d["update_time"], d["item_count"]))
    if counts:
        L.append("\n素材计数：`%s`\n" % json.dumps(counts, ensure_ascii=False))
    L.append("\n## 栏目访问（%d 次导航 / %d 个栏目）\n" % (len(nav), len(columns)))
    L.append("| 栏目 | 次数 | 首次 | 末次 |\n|---|---|---|---|")
    for c in columns:
        L.append("| %s | %d | %s | %s |" % (c["column"], c["visits"],
                                            c["first"][11:], c["last"][11:]))
    L.append("\n## 接口命中（共 %d 条响应）\n" % len(recs))
    L.append("| 接口 | 次数 |\n|---|---|")
    for k, v in sorted(api_hits.items(), key=lambda x: -x[1]):
        L.append("| `%s` | %d |" % (k, v))
    L.append("\n---\n本文件不含 Cookie / token / ticket 等凭据；凭据另存于 session.json，请勿外传。\n")
    with open(os.path.join(OUT, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    print("解析完成：%d 条响应 / %d 次导航 / %d 个栏目" % (len(recs), len(nav), len(columns)))
    print("已发表 %d 篇，草稿 %d 条，可切换账号 %d 个" % (len(articles), len(drafts), len(accounts)))
    print("输出目录：", OUT)


if __name__ == "__main__":
    main()
