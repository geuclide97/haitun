# -*- coding: utf-8 -*-
"""从 geuclide97/zjys9-py 仓库生成 TVBox 配置文件 zjys9.json。

- 读取 zjys9-py 仓库根目录的所有 .py/.js 爬虫源（排除 sync.py 等非爬虫文件）
- 每个源生成一个 site，api 指向 zjys9-py 的 raw 链接
- 输出 zjys9.json（与仓库同目录）

本地运行：  python gen_zjys9.py
GitHub Actions 自动运行（见 .github/workflows/sync-zjys9.yml）
"""
import json
import os
import sys
import urllib.parse

import requests

# 源仓库与配置文件输出
SRC_REPO = "geuclide97/zjys9-py"
SRC_BRANCH = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{SRC_REPO}/{SRC_BRANCH}/"

# 本仓库的 python 版 spider jar（haitun 仓库根目录）
SPIDER_JAR = "https://raw.githubusercontent.com/geuclide97/haitun/main/9527.jar"
WALLPAPER = "http://tool.teyonds.com/api"

OUT_NAME = "zjys9.json"

HERE = os.path.dirname(os.path.abspath(__file__))

# 排除这些文件（非爬虫源）
EXCLUDE = {"sync.py", "gen_zjys9.py"}


def fetch_files():
    headers = {"User-Agent": "gen-zjys9", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{SRC_REPO}/contents/"
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def build_sites(items):
    sites = []
    for it in items:
        if it.get("type") != "file":
            continue
        name = it["name"]
        if name in EXCLUDE:
            continue
        low = name.lower()
        if not (low.endswith(".py") or low.endswith(".js")):
            continue
        # key = 去掉扩展名的文件名
        stem = os.path.splitext(name)[0]
        # URL 编码文件名，确保中文名能被客户端正确访问
        api = RAW_BASE + urllib.parse.quote(name)
        sites.append({
            "key": stem,
            "name": "🐬" + name,
            "type": 3,
            "api": api,
            "searchable": 1,
            "quickSearch": 1,
            "filterable": 1,
        })
    # 按文件名稳定排序，保证 diff 稳定
    sites.sort(key=lambda s: s["key"])
    return sites


def main():
    items = fetch_files()
    sites = build_sites(items)

    config = {
        "spider": SPIDER_JAR,
        "wallpaper": WALLPAPER,
        "sites": sites,
    }

    out_path = os.path.join(HERE, OUT_NAME)
    new_text = json.dumps(config, ensure_ascii=False, indent=2) + "\n"

    if os.path.exists(out_path):
        with open(out_path, "r", encoding="utf-8") as f:
            old_text = f.read()
        if old_text == new_text:
            print(f"无变更（{len(sites)} 个源），{OUT_NAME} 已是最新。")
            return 0
        changed = True
    else:
        changed = True

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(new_text)

    print(f"已生成 {OUT_NAME}，共 {len(sites)} 个源。")
    if changed:
        print("内容有变更。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
