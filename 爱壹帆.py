# -*- coding: utf-8 -*-
"""
爱壹帆 (iyf.tv) TVBox Python 爬虫
适配 2026-09 新版 API (m10.iyf.tv / rankv21.iyf.tv)

原旧版基于 www.iyf.lv 静态蓝光模板(.module-poster-item)，该域名已 523 失效，
且 iyf.tv 已改版为 Vue SPA。本版直接对接官方 JSON API：

1. 密钥:  从 https://www.iyf.tv/ 首页 injectJson -> config[0].pConfig.publicKey/privateKey
2. 签名:   vv = MD5(publicKey + '&' + query.lower() + '&' + privateKey[ts % len])
3. 分类:   m10.iyf.tv/api/list/Search
4. 搜索:   rankv21.iyf.tv/v3/list/briefsearch (无需签名)
5. 剧集:   m10.iyf.tv/v3/video/languagesplaylist
6. 播放:   m10.iyf.tv/v3/video/play -> flvPathList[isHls].result + ?vv=&pub=
"""

import json
import re
import time
import hashlib
import base64

import sys

sys.path.append('..')
from base.spider import Spider


class Spider(Spider):

    def init(self, extend=""):
        self.host = "https://m10.iyf.tv"
        self.home = "https://www.iyf.tv"
        self.search_host = "https://rankv21.iyf.tv"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
            'Referer': 'https://www.iyf.tv/',
        }
        self.public_key = ""
        self.private_key = []
        self._key_time = 0
        self._update_keys()

    def getName(self):
        return "爱壹帆"

    def isVideoFormat(self, url):
        pass

    def manualVideoCheck(self):
        pass

    def destroy(self):
        pass

    # ---------------- 密钥 / 签名 ----------------

    def _update_keys(self):
        """从首页 injectJson 提取 publicKey / privateKey，缓存 10 分钟。"""
        if self.public_key and (time.time() - self._key_time) < 600:
            return
        try:
            html = self.fetch(self.home, headers=self.headers).text
            m = re.search(r'var\s+injectJson\s*=\s*(\{.*?\});', html, re.S)
            if not m:
                return
            ij = json.loads(m.group(1))
            pconf = ij['config'][0]['pConfig']
            self.public_key = pconf['publicKey']
            self.private_key = pconf['privateKey']
            self._key_time = time.time()
        except Exception:
            pass

    def _sign(self, query):
        pk = self.private_key[int(time.time() * 1000) % len(self.private_key)]
        raw = self.public_key + '&' + query.lower() + '&' + pk
        return hashlib.md5(raw.encode('utf-8')).hexdigest()

    def _get_json(self, url):
        """带密钥刷新重试的 JSON GET。"""
        self._update_keys()
        try:
            r = self.fetch(url, headers=self.headers)
            data = json.loads(r.text)
            if data.get('ret') == 200:
                return data
        except Exception:
            pass
        # 失败：强制刷新密钥重试一次
        self.public_key = ""
        self._update_keys()
        r = self.fetch(url, headers=self.headers)
        return json.loads(r.text)

    # ---------------- 首页 / 分类 ----------------

    def homeContent(self, filter):
        classes = [
            {"type_name": "电影", "type_id": "3"},
            {"type_name": "剧集", "type_id": "4"},
            {"type_name": "综艺", "type_id": "5"},
            {"type_name": "动漫", "type_id": "6"},
            {"type_name": "纪录片", "type_id": "7"},
            {"type_name": "体育", "type_id": "95"},
        ]
        return {'class': classes, 'filters': {}}

    def _category_url(self, cid, pg):
        params = f'cinema=1&page={pg}&size=36&orderby=0&desc=1&cid=0,1,{cid}&isserial=-1&isIndex=-1&isfree=-1'
        return f'{self.host}/api/list/Search?{params}&vv={self._sign(params)}&pub={self.public_key}'

    def _parse_list(self, result):
        vods = []
        for it in result:
            meta = {
                'key': it.get('key', ''),
                'name': it.get('title', ''),
                'pic': it.get('image', ''),
                'remarks': it.get('cid', ''),
                'year': it.get('year', ''),
                'actor': it.get('starring', ''),
                'director': it.get('directed', ''),
                'area': it.get('regional', ''),
                'content': it.get('contxt', ''),
            }
            vod = {
                'vod_id': self._encode_meta(meta),
                'vod_name': meta['name'],
                'vod_pic': meta['pic'],
                'vod_remarks': meta['remarks'],
            }
            vods.append(vod)
        return vods

    def homeVideoContent(self):
        try:
            data = self._get_json(self._category_url('3', '1'))
            result = data['data']['info'][0].get('result', [])
            return {'list': self._parse_list(result)}
        except Exception:
            return {'list': []}

    def categoryContent(self, tid, pg, filter, extend):
        try:
            data = self._get_json(self._category_url(tid, pg))
            result = data['data']['info'][0].get('result', [])
            return {
                'list': self._parse_list(result),
                'page': pg,
                'pagecount': 9999,
                'limit': 36,
                'total': 99999,
            }
        except Exception:
            return {'list': []}

    # ---------------- 搜索 ----------------

    def searchContent(self, key, quick, pg="1"):
        try:
            from urllib.parse import quote
            params = f'tags={quote(key)}&orderby=4&page={pg}&size=10&desc=0&isserial=-1&istitle=true'
            url = f'{self.search_host}/v3/list/briefsearch?{params}'
            data = self._get_json(url)
            result = data['data']['info'][0].get('result', [])
            vods = []
            for it in result:
                meta = {
                    'key': it.get('contxt', ''),
                    'name': it.get('title', ''),
                    'pic': it.get('imgPath', ''),
                    'remarks': it.get('lastName', '') or it.get('cid', ''),
                    'actor': it.get('starring', ''),
                    'director': it.get('directed', ''),
                    'area': it.get('regional', ''),
                }
                vods.append({
                    'vod_id': self._encode_meta(meta),
                    'vod_name': meta['name'],
                    'vod_pic': meta['pic'],
                    'vod_remarks': meta['remarks'],
                })
            return {'list': vods, 'page': pg}
        except Exception:
            return {'list': []}

    # ---------------- 详情 ----------------

    def _encode_meta(self, meta):
        s = json.dumps(meta, ensure_ascii=False)
        return base64.urlsafe_b64encode(s.encode('utf-8')).decode('ascii').rstrip('=')

    def _decode_meta(self, vod_id):
        try:
            pad = '=' * (-len(vod_id) % 4)
            s = base64.urlsafe_b64decode((vod_id + pad).encode('ascii')).decode('utf-8')
            return json.loads(s)
        except Exception:
            return {'key': vod_id, 'name': '', 'pic': '', 'remarks': ''}

    def detailContent(self, ids):
        try:
            meta = self._decode_meta(ids[0])
            vid = meta.get('key', '')
            params = f'cinema=1&vid={vid}&lsk=1&taxis=0&cid=0,1,4,133'
            url = f'{self.host}/v3/video/languagesplaylist?{params}&vv={self._sign(params)}&pub={self.public_key}'
            data = self._get_json(url)
            play_list = data['data']['info'][0].get('playList', [])

            vod = {}
            vod['vod_id'] = ids[0]
            vod['vod_name'] = meta.get('name', '')
            vod['vod_pic'] = meta.get('pic', '')
            vod['vod_remarks'] = meta.get('remarks', '')
            vod['vod_year'] = meta.get('year', '')
            vod['vod_actor'] = meta.get('actor', '')
            vod['vod_director'] = meta.get('director', '')
            vod['vod_area'] = meta.get('area', '')
            vod['vod_content'] = meta.get('content', '')

            parts = []
            for p in play_list:
                name = p.get('name', '')
                k = p.get('key', '')
                if k:
                    parts.append(f"{name}${k}")
            if parts:
                vod['vod_play_from'] = '爱壹帆'
                vod['vod_play_url'] = '#'.join(parts)

            return {'list': [vod]}
        except Exception:
            return {'list': []}

    # ---------------- 播放 ----------------

    def playerContent(self, flag, id, vipFlags):
        try:
            params = f'cinema=1&id={id}&a=0&lang=none&usersign=1&region=GL.&device=1&isMasterSupport=1'
            url = f'{self.host}/v3/video/play?{params}&vv={self._sign(params)}&pub={self.public_key}'
            data = self._get_json(url)
            flv = data['data']['info'][0].get('flvPathList', [])

            hls = None
            mp4 = None
            for f in flv:
                if f.get('isHls'):
                    hls = f.get('result')
                    break
                if not mp4:
                    mp4 = f.get('result')
            play_url = hls or mp4
            if play_url:
                if hls:
                    play_url += f'?vv={self._sign("")}&pub={self.public_key}'
                return {'parse': 0, 'url': play_url, 'header': self.headers}
            return {'parse': 0, 'url': '', 'header': self.headers}
        except Exception:
            return {'parse': 0, 'url': '', 'header': self.headers}

    def localProxy(self, param):
        pass
