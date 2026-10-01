#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
超级直播 (AliIPTV) 海外直连 —— 长期维护脚本
=================================================
原理：AliIPTV 的 CDN 直接暴露 m3u8 裸地址，无需 verify token / uuid。
- p-drm.aliiptv.com    : CCTV + 免费频道
- play-drm.aliiptv.com : 凤凰/东森/HBO/电影等频道

功能：
 1. 解析频道清单（channel_list_full.txt 或内置兜底）
 2. 自动探测每个频道在哪个 CDN 域名下可播（多候选自动切换）
 3. 生成 M3U 播放列表 + JSON 映射
 4. 单次模式 / daemon 定时模式

用法：
    python aliiptv_live.py               # 单次生成 M3U
    python aliiptv_live.py --json        # 同时输出 JSON
    python aliiptv_live.py --daemon --interval 30   # 每30分钟定时刷新
    python aliiptv_live.py --out D:\\x.m3u # 指定输出
"""
import io, sys, os, re, json, time, argparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
CHANNEL_FILE = os.path.join(HERE, 'channel_list_full.txt')
OUT_DEFAULT = os.path.join(HERE, '超级直播海外直连_完整.m3u')
JSON_OUT = os.path.join(HERE, 'aliiptv_channels.json')

# CDN 域名候选（按优先级探测，失效后可在此追加新域名）
CDN_DOMAINS = [
    'https://p-drm.aliiptv.com/live/',
    'https://play-drm.aliiptv.com/live/',
]

HEADERS = {'User-Agent': 'okhttp/3.12.1'}

# 内置兜底频道（channel_list 读取失败时用）
FALLBACK_CHANNELS = [
    ('CCTV-1 綜合', 'cctv1'), ('CCTV-2 财经', 'cctv2'), ('CCTV-3 综艺', 'cctv3'),
    ('CCTV-4 中文国际', 'cctv4'), ('CCTV-5 体育', 'cctv5'), ('CCTV-6 电影', 'cctv6'),
    ('CCTV-7 国防军事', 'cctv7'), ('CCTV-8 电视剧', 'cctv8'), ('CCTV-9 纪录', 'cctv9'),
    ('CCTV-10 科教', 'cctv10'), ('CCTV-11 戏曲', 'cctv11'), ('CCTV-12 社会与法', 'cctv12'),
    ('CCTV-13 新闻', 'cctv13'), ('CCTV-14 少儿', 'cctv14'), ('CCTV-15 音乐', 'cctv15'),
    ('CCTV-16 奥林匹克', 'cctv16'), ('CCTV-17 农业', 'cctv17'),
    ('TVB 翡翠台', 'jade'), ('TVB 星河', 'tvbxinghe'), ('TVB Plus', 'tvbPlus'),
    ('TVB 明珠台', 'tvbPearl'), ('TVB 无线新闻', 'tvbNews'), ('Viu TV', 'ViuTV'),
    ('凤凰卫视中文台', 'HDPhxChinese'), ('凤凰卫视资讯台', 'HDPhxInfonews'),
    ('凤凰卫视香港台', 'HDPhxHK'), ('东森洋片', 'dsyp'), ('東森电影', 'dsdy'),
    ('東森戲劇', 'dsxj'), ('東森综合', 'dszh'), ('東森幼幼', 'dsyy'),
]


def load_channel_list():
    """解析频道 (名称, mshd路径) 列表，失败则用兜底。"""
    channels = []
    if os.path.exists(CHANNEL_FILE):
        try:
            txt = open(CHANNEL_FILE, encoding='utf-8').read()
            for blk in re.split(r'(?=\[\d+\])', txt):
                m = re.match(r'\[(\d+)\]\s+(.+?)\s+\(', blk)
                if not m:
                    continue
                mm = re.search(r'mshd://p2p\.aliiptv\.com/live/(\S+)', blk)
                if mm:
                    channels.append((m.group(2), mm.group(1)))
        except Exception as e:
            print('解析频道列表失败:', e)
    if not channels:
        channels = FALLBACK_CHANNELS
    # 去重保序
    seen, out = set(), []
    for n, p in channels:
        if p not in seen:
            seen.add(p)
            out.append((n, p))
    return out


def resolve(channel_path):
    """返回第一个可用的 CDN URL，无则 None。"""
    import requests
    for dom in CDN_DOMAINS:
        url = dom + channel_path + '.m3u8'
        try:
            r = requests.get(url, timeout=8, headers=HEADERS)
            if r.status_code == 200:
                lines = [l.strip() for l in r.text.strip().split('\n')
                         if l.strip() and not l.startswith('#')]
                if lines:  # 有分片才认为有效
                    return url
        except Exception:
            continue
    return None


def build(out_path, want_json):
    channels = load_channel_list()
    print(f'频道数: {len(channels)}')
    result, ok = {}, 0
    for name, path in channels:
        url = resolve(path)
        result[path] = {'name': name, 'path': path, 'url': url}
        if url:
            ok += 1
    print(f'可播 {ok}/{len(channels)}')

    if want_json:
        with open(JSON_OUT, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    lines = ['#EXTM3U']
    for path, d in result.items():
        if d['url']:
            lines.append('#EXTINF:-1,' + d['name'])
            lines.append(d['url'])
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print(f'已生成 {out_path} ({ok} 频道) @ {time.strftime("%H:%M:%S")}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=OUT_DEFAULT)
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--daemon', action='store_true')
    ap.add_argument('--interval', type=int, default=30)
    a = ap.parse_args()

    while True:
        build(a.out, a.json)
        if not a.daemon:
            break
        print(f'休眠 {a.interval} 分钟...')
        time.sleep(a.interval * 60)