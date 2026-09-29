# -*- coding: utf-8 -*-
# @Author  : Doubebly
# @Time    : 2025/5/29 22:07

import sys
import hashlib
import time
import requests
import re
import json
sys.path.append('..')
from base.spider import Spider


class Spider(Spider):
    def getName(self):
        return "Jinpai"

    def init(self, extend):
        self.home_url = 'https://www.hkybqufgh.com'
        self.key = 'cb808529bae6b6be45ecfab29a4889bc'
        self.ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        self.error_url = "https://sf1-cdn-tos.huoshanstatic.com/obj/media-fe/xgplayer_doc_video/mp4/xgplayer-demo-720p.mp4"

    def getDependence(self):
        return []

    def isVideoFormat(self, url):
        pass

    def manualVideoCheck(self):
        pass

    def _api(self, path, params):
        t = str(int(time.time() * 1000))
        s = '&'.join([str(k) + '=' + str(v) for k, v in params] + ['key=' + self.key, 't=' + t])
        sign = hashlib.sha1(hashlib.md5(s.encode()).hexdigest().encode()).hexdigest()
        h = {
            'User-Agent': self.ua,
            'Accept': 'application/json, text/plain, */*',
            'sign': sign,
            't': t,
        }
        r = requests.get(self.home_url + path, params=params, headers=h)
        return r.json()

    def _vod(self, i):
        remarks = i.get('vodRemarks') or ''
        if i.get('typeId1') == 1:
            remarks = i.get('vodVersion') or remarks
        return {
            'vod_id': str(i.get('vodId', '')),
            'vod_name': i.get('vodName', ''),
            'vod_pic': i.get('vodPic', ''),
            'vod_remarks': remarks,
        }

    def homeContent(self, filter):
        classes = [
            {'type_id': '1', 'type_name': '电影'},
            {'type_id': '2', 'type_name': '电视剧'},
            {'type_id': '3', 'type_name': '综艺'},
            {'type_id': '4', 'type_name': '动漫'},
        ]
        filters = {}
        try:
            r = self._api('/api/mw-movie/anonymous/get/filer/type', [])
            data = r.get('data')
            if data:
                classes = [{'type_id': str(x.get('typeId', '')), 'type_name': x.get('typeName', '')} for x in data]
        except:
            pass
        try:
            r2 = self._api('/api/mw-movie/anonymous/v1/get/filer/list', [])
            data2 = r2.get('data') or {}
            for tid, cfg in data2.items():
                f = []
                if cfg.get('plotList'):
                    f.append({'key': 'v_class', 'name': '类型', 'value': [{'n': x.get('itemText', ''), 'v': x.get('itemText', '')} for x in cfg['plotList']]})
                if cfg.get('yearList'):
                    f.append({'key': 'year', 'name': '年份', 'value': [{'n': x.get('itemText', ''), 'v': x.get('itemText', '')} for x in cfg['yearList']]})
                sv = [{'n': '最近更新', 'v': '2'}, {'n': '人气高低', 'v': '3'}, {'n': '评分高低', 'v': '4'}]
                if str(tid) != '1':
                    sv = [{'n': '上映时间', 'v': '1'}] + sv
                f.append({'key': 'sort', 'name': '排序', 'value': sv})
                filters[str(tid)] = f
        except:
            pass
        return {'class': classes, 'filters': filters}

    def homeVideoContent(self):
        video_list = []
        try:
            r = self._api('/api/mw-movie/anonymous/v1/home/all/list', [])
            data = r.get('data') or {}
            for k, v in data.items():
                for i in (v.get('list') or []):
                    video_list.append(self._vod(i))
        except:
            pass
        try:
            r2 = self._api('/api/mw-movie/anonymous/home/hotSearch', [])
            for i in (r2.get('data') or []):
                video_list.append(self._vod(i))
        except:
            pass
        return {'list': video_list, 'parse': 0, 'jx': 0}

    def categoryContent(self, cid, page, filter, ext):
        if not ext:
            ext = {}
        params = [
            ('pageNum', str(page)),
            ('pageSize', '30'),
            ('sort', str(ext.get('sort', '1'))),
            ('sortBy', '1'),
            ('type1', str(cid)),
        ]
        if ext.get('v_class'):
            params.append(('v_class', str(ext['v_class'])))
        if ext.get('year'):
            params.append(('year', str(ext['year'])))
        video_list = []
        try:
            r = self._api('/api/mw-movie/anonymous/video/list', params)
            data_list = (r.get('data') or {}).get('list') or []
            for i in data_list:
                video_list.append(self._vod(i))
        except:
            return {'list': [], 'parse': 0, 'jx': 0}
        return {'list': video_list, 'parse': 0, 'jx': 0}

    def _episode_urls(self, vid, nid):
        """返回所有线路的播放地址（按分辨率升序：标清→高清→蓝光）。"""
        try:
            r = self._api('/api/mw-movie/anonymous/v2/video/episode/url', [('id', vid), ('nid', nid)])
            lst = (r.get('data') or {}).get('list') or []
            lst = sorted(lst, key=lambda x: int(x.get('resolution') or 0))
            return [x.get('url', '') for x in lst if x.get('url')]
        except:
            pass
        return []

    def detailContent(self, did):
        ids = did[0]
        try:
            r = self._api('/api/mw-movie/anonymous/video/detail', [('id', str(ids))])
            data = r.get('data') or {}
            if not data:
                return {'list': []}
            vod_name = data.get('vodName', '')
            play_list = data.get('episodeList') or []
            # 多线路：标清(默认快)/高清/蓝光，延迟加载（播放时再实时取地址）
            line_names = ['标清', '高清', '蓝光']
            groups = []
            for line_idx in range(len(line_names)):
                eps = []
                for i in play_list:
                    name = i.get('name', '') or vod_name
                    nid = i.get('nid', '')
                    if nid:
                        eps.append('%s$%s|%s|%d' % (name, str(ids), str(nid), line_idx))
                groups.append('#'.join(eps))
            video_list = [{
                'type_name': data.get('typeName', ''),
                'vod_id': str(ids),
                'vod_name': vod_name,
                'vod_pic': data.get('vodPic', ''),
                'vod_year': (data.get('vodPubdate') or '')[:4],
                'vod_area': data.get('vodArea', ''),
                'vod_actor': data.get('vodActor', ''),
                'vod_director': data.get('vodDirector', ''),
                'vod_content': re.sub(r'<[^>]+>', '', data.get('vodContent') or '').strip(),
                'vod_remarks': data.get('vodRemarks', ''),
                'vod_play_from': '$$$'.join(line_names),
                'vod_play_url': '$$$'.join(groups),
            }]
            return {'list': video_list, 'parse': 0, 'jx': 0}
        except:
            return {'list': []}

    def searchContent(self, key, quick, page='1'):
        video_list = []
        try:
            params = [('keyword', key), ('pageNum', str(page)), ('pageSize', '12'), ('type', 'false')]
            t = str(int(time.time() * 1000))
            s = '&'.join([str(k) + '=' + str(v) for k, v in params] + ['key=' + self.key, 't=' + t])
            sign = hashlib.sha1(hashlib.md5(s.encode()).hexdigest().encode()).hexdigest()
            h = {
                'User-Agent': self.ua,
                'sign': sign,
                't': t,
                'Accept': 'application/json, text/plain, */*',
            }
            r = requests.get(self.home_url + '/api/mw-movie/anonymous/video/searchByWordPageable', params=params, headers=h)
            data_list = (r.json().get('data') or {}).get('list') or []
            # 去空白后子串匹配，兼容“新兵第四季” vs “新兵 第四季”等空格差异
            key_n = re.sub(r'\s+', '', str(key or ''))
            for i in data_list:
                name = i.get('vodName') or ''
                if not key_n or key_n in re.sub(r'\s+', '', name):
                    video_list.append({
                        'vod_id': str(i.get('vodId', '')),
                        'vod_name': name,
                        'vod_pic': i.get('vodPic', ''),
                        'vod_remarks': i.get('vodVersion') or i.get('vodRemarks') or '',
                    })
            # 回退：去空白匹配无结果时（如“庆余年 第二季”vs“庆余年2”），直接返回 API 全部结果
            if not video_list and data_list:
                for i in data_list:
                    video_list.append({
                        'vod_id': str(i.get('vodId', '')),
                        'vod_name': i.get('vodName', ''),
                        'vod_pic': i.get('vodPic', ''),
                        'vod_remarks': i.get('vodVersion') or i.get('vodRemarks') or '',
                    })
        except:
            return {'list': [], 'parse': 0, 'jx': 0}
        return {'list': video_list, 'parse': 0, 'jx': 0}

    def playerContent(self, flag, pid, vipFlags):
        pid = str(pid or '')
        # 新格式：vid|nid|线路索引（详情页多线路延迟加载，播放时实时取地址）
        if '|' in pid and not pid.startswith('http'):
            parts = pid.split('|')
            vid = parts[0].strip()
            nid = parts[1].strip()
            line_idx = int(parts[2].strip()) if len(parts) > 2 and parts[2].strip().isdigit() else 0
            urls = self._episode_urls(vid, nid)
            url = ''
            if urls:
                url = urls[line_idx] if line_idx < len(urls) else urls[-1]
            if url:
                h = {
                    'User-Agent': self.ua,
                    'Referer': self.home_url + '/',
                }
                if '.m3u8' in url:
                    h['Origin'] = self.home_url
                    h['Sec-Fetch-Dest'] = 'empty'
                    h['Sec-Fetch-Mode'] = 'cors'
                    h['Sec-Fetch-Site'] = 'cross-site'
                return {'url': url, 'header': h, 'parse': 0, 'jx': 0}
        # 旧格式：完整 m3u8 URL（兼容）
        play_url = pid.split('&vodName=')[0].replace(' ', '%20').replace('"', '%22')
        h = {
            'User-Agent': self.ua,
            'Referer': self.home_url + '/',
        }
        if '.m3u8' in play_url:
            h['Origin'] = self.home_url
            h['Sec-Fetch-Dest'] = 'empty'
            h['Sec-Fetch-Mode'] = 'cors'
            h['Sec-Fetch-Site'] = 'cross-site'
        return {'url': play_url, 'header': h, 'parse': 0, 'jx': 0}

    def localProxy(self, params):
        pass

    def destroy(self):
        return '正在Destroy'


if __name__ == '__main__':
    pass
