# -*- coding: utf-8 -*-

import sys
import json
import hashlib
import hmac
import base64
import random
import string
from urllib.parse import urlencode, urljoin

import requests
import urllib3

sys.path.append('..')
from base.spider import Spider

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class Spider(Spider):

    # ====== RSA 密钥 (从 JS 中提取) ======
    CLIENT_PRIVATE_KEY = r"""-----BEGIN PRIVATE KEY-----
MIICdwIBADANBgkqhkiG9w0BAQEFAASCAmEwggJdAgEAAoGBAJ4FBai1Y6my4+fc
8AD5tyYzxgN8Q7M/PuFv+8i1Xje8ElXYVwzvYd1y/cNxwgW4RX0tDy9ya562V33x
6SyNr29DU6XytOeOlOkxt3gd5169K4iFaJ0l0wA4koMTcCAYVxC9B4+zzS5djYmF
MuRGfYgKYNH99vfY7BZjdAY68ty5AgMBAAECgYB1rbvHJj5wVF7Rf4Hk2BMDCi9+
zP4F8SW88Y6KrDbcPt1QvOonIea56jb9ZCxf4hkt3W6foRBwg86oZo2FtoZcpCJ+
rFqUM2/wyV4CuzlL0+rNNSq7bga7d7UVld4hQYOCffSMifyF5rCFNH1py/4Dvswm
pi5qljf+dPLSlxXl2QJBAMzPJ/QPAwcf5K5nngQtbZCD3nqDFpRixXH4aUAIZcDz
S1RNsHrT61mEwZ/thQC2BUJTQNpGOfgh5Ecd1MnURwsCQQDFhAFfmvK7svkygoKX
t55ARNZy9nmme0StMOfdb4Q2UdJjfw8+zQNtKFOM7VhB7ijHcfFuGsE7UeXBe20n
g/XLAkEAv9SoT2hgJaQxxUk4MCF8pgddstJlq8Z3uTA7JMa4x+kZfXTm/6TOo6I8
2VbXZLsYYe8op0lvsoHMFvBSBljV0QJBAKhxyoYRa98dZB5qZRskciaXTlge0WJk
kA4vvh3/o757izRlQMgrKTfng1GVfIZFqKtnBiIDWTXQw2N9cnqXtH8CQAx+CD5t
l1iT0cMdjvlMg2two3SnpOjpo7gALgumIDHAmsUWhocLtcrnJI032VQSUkNnLq9z
EIfmHDz0TPVNHBQ=
-----END PRIVATE KEY-----"""

    SERVER_PUBLIC_KEY = r"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCeBQWotWOpsuPn3PAA+bcmM8YD
fEOzPz7hb/vItV43vBJV2FcM72Hdcv3DccIFuEV9LQ8vcmuetld98eksja9vQ1Ol
8rTnjpTpMbd4HedevSuIhWidJdMAOJKDE3AgGFcQvQePs80uXY2JhTLkRn2ICmDR
/fb32OwWY3QGOvLcuQIDAQAB
-----END PUBLIC KEY-----"""

    BASE_URL = 'https://api-h5.uvod.tv'
    HOST = 'https://www.uvod.tv'
    PAGE_SIZE = 20
    ADULT_KEYWORDS = ['午夜版', '午夜', '成人', '情色', '色情', '三级', '18禁', '伦理', '性爱', '性交', '乱伦', '偷拍', 'AV', 'av', '无码', '有码', '高清无', '自拍', '猎奇', 'SM', 'sm', '黄色', 'H版', 'h版', '啪啪', '做爱', '肉文']

    def init(self, extend=''):
        self.ext = extend or ''
        self.session = requests.Session()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
            'Content-Type': 'application/json',
        }
        # 缓存分类
        self._categories = None
        self._cat_map = {}  # type_id -> parent_category_id
        self._bad_cat_ids = set()  # 成人分类 ID 集合
        return None

    def getName(self):
        return 'u视把'

    def isVideoFormat(self, url):
        value = str(url or '').lower()
        return any(m in value for m in ('.m3u8', '.mp4', '.m4v', '.flv', '.webm', '.ts'))

    # ========== 加密工具 ==========

    @staticmethod
    def _random_str(length=16):
        chars = string.ascii_letters + string.digits
        return ''.join(random.choice(chars) for _ in range(length))

    @staticmethod
    def _rsa_encrypt(data, key_str):
        from Crypto.PublicKey import RSA
        from Crypto.Cipher import PKCS1_v1_5
        key = RSA.importKey(key_str)
        cipher = PKCS1_v1_5.new(key)
        return base64.b64encode(cipher.encrypt(data.encode())).decode()

    @staticmethod
    def _aes_encrypt(data, key, iv):
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        cipher = AES.new(key.encode(), AES.MODE_CBC, iv.encode())
        return base64.b64encode(cipher.encrypt(pad(data.encode(), 16))).decode()

    @staticmethod
    def _aes_decrypt(data, key, iv):
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
        raw = base64.b64decode(data)
        cipher = AES.new(key.encode(), AES.MODE_CBC, iv.encode())
        return unpad(cipher.decrypt(raw), 16).decode()

    @staticmethod
    def _rsa_decrypt(data, key_str):
        from Crypto.PublicKey import RSA
        from Crypto.Cipher import PKCS1_v1_5
        key = RSA.importKey(key_str)
        cipher = PKCS1_v1_5.new(key)
        return cipher.decrypt(base64.b64decode(data), None).decode()

    def _encrypt_request(self, data):
        aes_key = self._random_str(16)
        iv = 'abcdefghijklmnop'
        encrypted_data = self._aes_encrypt(json.dumps(data, separators=(',', ':')), aes_key, iv)
        encrypted_key = self._rsa_encrypt(aes_key, self.SERVER_PUBLIC_KEY)
        return f'{encrypted_data}.{encrypted_key}'

    def _decrypt_response(self, data):
        parts = data.split('.')
        if len(parts) != 2:
            return None
        aes_key = self._rsa_decrypt(parts[1], self.CLIENT_PRIVATE_KEY)
        iv = 'abcdefghijklmnop'
        try:
            decrypted = self._aes_decrypt(parts[0], aes_key, iv)
            return json.loads(decrypted)
        except Exception:
            return None

    @staticmethod
    def _filter_dict(d):
        result = {}
        for k, v in d.items():
            if v is None or v == 0 or v == '0' or v == '' or v is False:
                continue
            result[k] = v
        return result

    @staticmethod
    def _ksort_dict(d):
        result = {}
        for k in sorted(d.keys()):
            result[k] = d[k]
        return result

    def _create_signature(self, query_str, timestamp):
        raw = f'-{query_str}-{timestamp}'
        return hashlib.md5(raw.encode()).hexdigest()

    def _call_api(self, method, params=None):
        params = params or {}
        timestamp = str(int(__import__('time').time() * 1000))
        # 过滤空值并排序用于签名
        filtered = self._filter_dict(params)
        sorted_params = self._ksort_dict(filtered)
        query_str = urlencode(sorted_params).lower()
        signature = self._create_signature(query_str, timestamp)
        encrypted_body = self._encrypt_request(params)
        headers = dict(self.headers)
        headers['X-TOKEN'] = ''
        headers['X-TIMESTAMP'] = timestamp
        headers['X-SIGNATURE'] = signature
        url = f'{self.BASE_URL}{method}'
        try:
            resp = self.session.post(url, headers=headers, data=encrypted_body, timeout=15, verify=False)
            if resp.status_code == 200:
                decrypted = self._decrypt_response(resp.text)
                if decrypted and isinstance(decrypted, dict) and 'data' in decrypted:
                    return decrypted['data']
                return decrypted
        except Exception:
            pass
        return None

    # ========== 分类 ==========

    def _load_categories(self):
        if self._categories is not None:
            return self._categories
        result = self._call_api('/video/category', {})
        cats = []
        self._cat_map = {}
        self._bad_cat_ids = set()
        # 只保留顶层分类(pid=0)，到"儿童"为止：
        # 电影/电视剧/综艺/动漫/体育/纪录片/粤台专区/儿童 共 8 个
        # 午夜版(108)等成人分类被 ADULT_KEYWORDS 过滤，所有子分类不再单独列出
        # (点进顶层分类即可看到该分类下全部子分类内容)
        if result and 'category_list' in result:
            for cat in result['category_list']:
                cid = str(cat.get('id') or '')
                name = str(cat.get('name') or '')
                pid = cat.get('pid', 0)
                if not cid or not name:
                    continue
                # 子分类一律不收
                if pid != 0:
                    continue
                # 检测是否为成人分类(午夜版等)
                if any(kw in name for kw in self.ADULT_KEYWORDS):
                    self._bad_cat_ids.add(int(cid))
                    continue
                cats.append({'type_id': cid, 'type_name': name})
                self._cat_map[cid] = int(cid)
        self._categories = cats
        return cats

    def homeContent(self, filter):
        cats = self._load_categories()
        result = {'class': cats, 'filters': {}}
        return result

    def homeVideoContent(self):
        try:
            self._load_categories()
            videos = []
            seen_ids = set()
            # 遍历每个非成人分类，从每个分类取最新视频
            if self._categories:
                for cat in self._categories:
                    tid = cat['type_id']
                    params = {'page': 1, 'pagesize': 5}
                    parent_id = self._get_parent_category_id(tid)
                    if parent_id is not None:
                        params['parent_category_id'] = parent_id
                    try:
                        tid_int = int(tid)
                        if parent_id is not None and parent_id != tid_int:
                            params['category_id'] = tid_int
                    except Exception:
                        pass
                    result = self._call_api('/video/list', params)
                    if result and 'video_list' in result:
                        for v in result['video_list']:
                            vid = str(v.get('id') or '')
                            if vid in seen_ids:
                                continue
                            seen_ids.add(vid)
                            item = self._parse_video_item(v)
                            if item:
                                videos.append(item)
                            if len(videos) >= 20:
                                return {'list': videos}
            return {'list': videos}
        except Exception:
            return {'list': []}

    # ========== 分类内容 ==========

    def _get_parent_category_id(self, tid):
        """获取分类对应的 parent_category_id"""
        try:
            tid_int = int(tid)
        except Exception:
            return None
        # 从缓存中查找
        if self._cat_map:
            return self._cat_map.get(tid)
        # 如果缓存为空，加载分类
        self._load_categories()
        return self._cat_map.get(tid) if hasattr(self, '_cat_map') else None

    def categoryContent(self, tid, pg, filter, extend):
        page = max(1, self._int(pg, 1))
        try:
            params = {'page': page, 'pagesize': self.PAGE_SIZE}
            # 先尝试用 parent_category_id
            parent_id = self._get_parent_category_id(tid)
            if parent_id is not None:
                params['parent_category_id'] = parent_id
            # 如果 tid 是子分类，也传入 category_id
            try:
                tid_int = int(tid)
                if parent_id is not None and parent_id != tid_int:
                    params['category_id'] = tid_int
            except Exception:
                pass

            result = self._call_api('/video/list', params)
            videos = []
            total = 0
            if result:
                if 'video_list' in result and isinstance(result.get('video_list'), list):
                    for v in result['video_list']:
                        item = self._parse_video_item(v)
                        if item:
                            videos.append(item)
                total = int(result.get('video_total', 0) or 0)
            pagecount = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
            limit = len(videos) or self.PAGE_SIZE
            return {
                'list': videos,
                'page': page,
                'pagecount': pagecount,
                'limit': limit,
                'total': total,
            }
        except Exception:
            return {
                'list': [],
                'page': page,
                'pagecount': page,
                'limit': self.PAGE_SIZE,
                'total': 0,
            }

    # ========== 详情 ==========

    def detailContent(self, ids):
        raw_id = str(ids[0] if ids else '').strip()
        if not raw_id:
            return {'list': []}
        try:
            # video_id 可能是数字或 URL
            if raw_id.isdigit():
                video_id = int(raw_id)
            else:
                # 从 URL 中提取 video_id (支持 ?video_id=, &video_id=, 或 video_id= 开头)
                import re
                m = re.search(r'(?:[?&]|^)video_id=(\d+)', raw_id)
                if m:
                    video_id = int(m.group(1))
                else:
                    return {'list': []}

            result = self._call_api('/video/info', {'id': video_id})
            if not result:
                return {'list': []}

            video = result.get('video') or {}
            fragment_list = result.get('video_fragment_list') or []

            title = video.get('title') or ''
            pic = video.get('pic') or ''
            score = video.get('score') or 0
            year = str(video.get('year') or '')
            area = video.get('region') or ''
            lang = video.get('language') or ''
            desc = video.get('description') or ''
            state = video.get('state') or ''
            hits = video.get('hits') or 0
            category_id = video.get('category_id') or ''
            director = video.get('director') or ''
            starring = video.get('starring') or ''

            # 备注
            remarks = []
            if year:
                remarks.append(str(year))
            if state:
                remarks.append(state)
            if score:
                remarks.append(f'{score}分')

            # 构建剧集列表
            from_list = []
            url_list = []

            if fragment_list:
                vod_eps = []
                for frag in fragment_list:
                    fid = frag.get('id') or ''
                    symbol = frag.get('symbol') or ''
                    if not fid or not symbol:
                        continue
                    # 生成播放 URL: 用 video_id 和 fragment_id 作为标识
                    play_url = f'video_id={video_id}&fragment_id={fid}'
                    ep_name = symbol
                    vod_eps.append(f'{ep_name}${play_url}')

                if vod_eps:
                    from_list.append('uTV')
                    url_list.append('#'.join(vod_eps))
            else:
                # 没有分集，直接使用 source
                play_url = f'video_id={video_id}'
                from_list.append('uTV')
                url_list.append(f'播放${play_url}')

            if not from_list:
                return {'list': []}

            vod = {
                'vod_id': f'video_id={video_id}',
                'vod_name': title,
                'vod_pic': pic,
                'type_name': '',
                'vod_year': year,
                'vod_area': area,
                'vod_actor': starring,
                'vod_director': director,
                'vod_remarks': ' '.join(remarks) or '',
                'vod_content': desc or title,
                'vod_play_from': '$$$'.join(from_list),
                'vod_play_url': '$$$'.join(url_list),
            }
            return {'list': [vod]}
        except Exception:
            return {'list': []}

    # ========== 搜索 ==========

    def searchContent(self, key, quick, pg='1'):
        page = max(1, self._int(pg, 1))
        keyword = str(key or '').strip()
        if not keyword:
            return {'list': [], 'page': page, 'pagecount': page, 'limit': self.PAGE_SIZE, 'total': 0}
        try:
            # 使用 /video/list 加 keyword 参数搜索
            params = {'keyword': keyword, 'page': page, 'pagesize': self.PAGE_SIZE}
            result = self._call_api('/video/list', params)
            videos = []
            total = 0
            if result:
                if 'video_list' in result and isinstance(result.get('video_list'), list):
                    for v in result['video_list']:
                        item = self._parse_video_item(v)
                        if item:
                            videos.append(item)
                total = int(result.get('video_total', 0) or 0)
            pagecount = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
            limit = len(videos) or self.PAGE_SIZE
            return {
                'list': videos,
                'page': page,
                'pagecount': pagecount,
                'limit': limit,
                'total': total,
            }
        except Exception:
            return {'list': [], 'page': page, 'pagecount': page, 'limit': self.PAGE_SIZE, 'total': 0}

    # ========== 播放 ==========

    def playerContent(self, flag, id, vipFlags):
        value = str(id or '').strip()
        headers = {
            'User-Agent': self.headers['User-Agent'],
            'Referer': self.HOST + '/',
        }
        if not value:
            return {'parse': 1, 'playUrl': '', 'url': self.HOST + '/', 'header': headers}

        # 解析参数
        params = {}
        for part in value.split('&'):
            if '=' in part:
                k, v = part.split('=', 1)
                params[k] = v

        video_id = params.get('video_id', '')
        fragment_id = params.get('fragment_id', '')

        if not video_id:
            return {'parse': 1, 'playUrl': '', 'url': self.HOST + '/', 'header': headers}

        try:
            source_params = {'video_id': int(video_id)}
            if fragment_id:
                source_params['video_fragment_id'] = int(fragment_id)
                source_params['quality'] = 4

            result = self._call_api('/video/source', source_params)
            if result and 'video_soruce' in result:
                source = result['video_soruce']
                url = source.get('url') or ''
                if url:
                    return {
                        'parse': 0,
                        'playUrl': '',
                        'url': url,
                        'header': headers,
                        'type': 'm3u8',
                    }
        except Exception:
            pass

        return {'parse': 1, 'playUrl': '', 'url': self.HOST + '/', 'header': headers}

    def localProxy(self, param):
        # uvod 直接返回的是可播放的 m3u8 链接，不需要本地代理
        return [404, 'text/plain; charset=utf-8', b'not supported']

    # ========== 工具方法 ==========

    def _parse_video_item(self, v):
        if not v or not isinstance(v, dict):
            return None
        vid = v.get('id') or ''
        title = v.get('title') or ''
        pic = v.get('pic') or ''
        if not vid or not title:
            return None
        score = v.get('score') or 0
        year = str(v.get('year') or '')
        state = v.get('state') or ''
        remark_parts = []
        if year:
            remark_parts.append(year)
        if state:
            remark_parts.append(state)
        if score:
            remark_parts.append(f'{score}分')
        return {
            'vod_id': f'video_id={vid}',
            'vod_name': title,
            'vod_pic': pic,
            'vod_remarks': ' '.join(remark_parts) or '',
            'vod_year': year or '',
        }

    @staticmethod
    def _int(value, default=0):
        try:
            return int(value)
        except Exception:
            return default