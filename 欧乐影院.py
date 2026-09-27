# -*- coding: utf-8 -*-
"""
欧乐影院 (olevod.com) TVBox Python 爬虫
适配 2026-09 新版 API (api.olelive.com)

原爱壹帆(iyf.tv)免费档只有 576P，且 VIP 墙锁死 720P+。欧乐影院免费档即为 720P(1280x720)，
本爬虫直接对接其官方 JSON API：

1. 签名:   GET 请求附加 _vv 参数，算法 fe(时间戳) —— MD5 与二进制位重组
2. 分类:   /v1/pub/vod/list/type
3. 列表:   /v1/pub/vod/list/true/3/0/0/{typeId}/0/0/update/{page}/{size}
4. 详情:   /v1/pub/vod/detail/{vodId}/true  -> urls[] 内含 m3u8 播放地址(720P)
5. 搜索:   /v1/pub/index/search/{keyword}/vod/0/{page}/{size}
6. 图片:   https://static.olelive.com/{pic}
"""

import json
import time
import hashlib

import sys

sys.path.append('..')
from base.spider import Spider


class Spider(Spider):

    def init(self, extend=""):
        self.host = "https://api.olelive.com"
        self.home = "https://www.olevod.com"
        self.image_base = "https://static.olelive.com"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
            'Referer': 'https://www.olevod.com/',
        }

    def getName(self):
        return "欧乐影院"

    def isVideoFormat(self, url):
        pass

    def manualVideoCheck(self):
        pass

    def destroy(self):
        pass

    # ---------------- 签名 / 请求 ----------------

    @staticmethod
    def _md5(s):
        return hashlib.md5(s.encode('utf-8')).hexdigest()

    def _vv(self, timestamp):
        """复现前端 fe() 签名算法。"""
        t = str(int(timestamp))
        r = ['', '', '', '']
        for ch in t:
            b = bin(ord(ch))[2:]
            r[0] += b[2:3]
            r[1] += b[3:4]
            r[2] += b[4:5]
            r[3] += b[5:]
        a = []
        for i in range(4):
            e = format(int(r[i], 2), 'x')
            if len(e) == 2:
                e = '0' + e
            elif len(e) == 1:
                e = '00' + e
            elif len(e) == 0:
                e = '000'
            a.append(e)
        n = self._md5(t)
        return n[:3] + a[0] + n[6:11] + a[1] + n[14:19] + a[2] + n[22:27] + a[3] + n[30:]

    def _get(self, path):
        sep = '&' if '?' in path else '?'
        url = self.host + path + sep + '_vv=' + self._vv(int(time.time()))
        r = self.fetch(url, headers=self.headers)
        return json.loads(r.text)

    def _pic(self, url):
        if not url:
            return ''
        if url.startswith('http'):
            return url
        return self.image_base + '/' + url

    # ---------------- 首页 / 分类 ----------------

    def homeContent(self, filter):
        classes = [
            {"type_name": "电影", "type_id": "1"},
            {"type_name": "连续剧", "type_id": "2"},
            {"type_name": "综艺", "type_id": "3"},
            {"type_name": "动漫", "type_id": "4"},
            {"type_name": "短剧", "type_id": "14"},
        ]
        return {'class': classes, 'filters': {}}

    def homeVideoContent(self):
        try:
            data = self._get('/v1/pub/vod/list/true/3/0/0/1/0/0/update/1/24')
            return {'list': self._parse_list(data['data'].get('list', []))}
        except Exception:
            return {'list': []}

    def _parse_list(self, items):
        vods = []
        for it in items:
            vod = {
                'vod_id': str(it.get('id', '')),
                'vod_name': it.get('name', ''),
                'vod_pic': self._pic(it.get('pic', '')),
                'vod_remarks': it.get('remarks', ''),
            }
            vods.append(vod)
        return vods

    def categoryContent(self, tid, pg, filter, extend):
        try:
            pg = int(pg) if pg else 1
            data = self._get(f'/v1/pub/vod/list/true/3/0/0/{tid}/0/0/update/{pg}/36')
            d = data['data']
            total = d.get('total', 0)
            size = d.get('pageSize', 36) or 36
            pagecount = (total + size - 1) // size if size else 1
            return {
                'list': self._parse_list(d.get('list', [])),
                'page': pg,
                'pagecount': pagecount,
                'limit': size,
                'total': total,
            }
        except Exception:
            return {'list': []}

    # ---------------- 搜索 ----------------

    def searchContent(self, key, quick, pg="1"):
        try:
            from urllib.parse import quote
            pg = int(pg) if pg else 1
            data = self._get(f'/v1/pub/index/search/{quote(key)}/vod/0/{pg}/20')
            d = data.get('data') or {}
            blocks = d.get('data', []) if isinstance(d, dict) else []
            vods = []
            for block in blocks:
                if isinstance(block, dict) and block.get('type') == 'vod':
                    vods.extend(self._parse_list(block.get('list', [])))
            return {'list': vods, 'page': pg}
        except Exception:
            return {'list': []}

    # ---------------- 详情 ----------------

    def detailContent(self, ids):
        try:
            vid = ids[0]
            data = self._get(f'/v1/pub/vod/detail/{vid}/true')
            d = data['data']

            vod = {}
            vod['vod_id'] = vid
            vod['vod_name'] = d.get('name', '')
            vod['vod_pic'] = self._pic(d.get('pic', ''))
            vod['vod_remarks'] = d.get('remarks', '') or d.get('version', '')
            vod['vod_year'] = str(d.get('year', ''))
            vod['vod_actor'] = d.get('actor', '')
            vod['vod_director'] = d.get('director', '')
            vod['vod_area'] = d.get('area', '')
            vod['vod_content'] = d.get('content', '') or d.get('blurb', '')

            parts = []
            for u in d.get('urls', []):
                idx = u.get('index', 1)
                title = u.get('title', '') or f'第{idx}集'
                parts.append(f"{title}${vid}_{idx}")
            if parts:
                vod['vod_play_from'] = '欧乐影院'
                vod['vod_play_url'] = '#'.join(parts)

            return {'list': [vod]}
        except Exception:
            return {'list': []}

    # ---------------- 播放 ----------------

    def playerContent(self, flag, id, vipFlags):
        try:
            parts = str(id).split('_')
            vid = parts[0]
            idx = int(parts[1]) if len(parts) > 1 else 1
            data = self._get(f'/v1/pub/vod/detail/{vid}/true')
            urls = data['data'].get('urls', [])
            for u in urls:
                if u.get('index') == idx:
                    url = u.get('url', '')
                    if url:
                        return {'parse': 0, 'url': url, 'header': self.headers}
            return {'parse': 0, 'url': '', 'header': self.headers}
        except Exception:
            return {'parse': 0, 'url': '', 'header': self.headers}

    def localProxy(self, param):
        pass
