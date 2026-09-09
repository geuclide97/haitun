# -*- coding: utf-8 -*-
"""
ZIP0 (zip0.com) TVBox 爬虫
==========================
站点架构: TanStack Start SPA, 数据全部走 /_serverFn/{sha256} 端点, payload 为 seroval 编码。
ZIP0 本身是聚合站, 后端挂 10 条上游线路, 因此:
  - 分类/搜索: 并发查询全部线路后按 标题:年份 去重合并
  - 详情: 同一影片会有多条线路, 每条线路的 episodes 直出 m3u8 地址
  - 播放: 起播前对每条线路做并发实测(走完 m3u8 跳转链), 按真实延时升序排列,
          最快的线路排在最前, 探测失败(403/404/超时)的线路排到最后

关键约束(实测):
  1) /_serverFn 必须同时带 x-tsr-serverFn: true 和 sec-fetch-site: same-origin, 否则 403
  2) 站点自带的测速端点不可用作排序依据: 有服务端缓存(重复调用恒返回 10ms),
     且判定不准(zuid 报 unavailable 但实际可播), 所以自行实测
  3) 多数线路的 index.m3u8 是 master, 需再跳一层才拿到分片列表, 探测必须走完整条链
  4) ALTS/SEARCH 是服务端扇出端点, 并发调用会互相争抢上游池(串行 0.7s 的调用
     并发时 5s 都完不成), 因此详情阶段的扇出调用串行错开; 分类端点是单源调用可 10 路并发

性能设计:
  - 持久线程池 + 每线程独立 Session: TLS 连接跨调用复用(每次重建握手是此前慢的主因)
  - 分类软超时 cat_deadline(默认 3s): 实测 dyttzy 分类接口固定 6.5s+, 到点放弃慢线路
  - 分类页 120s / 详情页 600s 结果缓存: 翻页与重进直接命中
  - 线路探测带域名级死判定缓存(15min): 死 CDN 对每部片都是死的, 不必每次等超时
  - init 时后台预热 TLS 连接

ext 可调项(JSON): {"probe":bool, "probe_timeout":秒, "probe_budget":秒,
  "cat_deadline":秒, "max_lines":n, "workers":n}
"""

import sys
import json
import re
import time
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, wait

import requests
import urllib3

sys.path.append('..')
from base.spider import Spider

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class Spider(Spider):

    BASE_URL = 'https://zip0.com'
    UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')

    # /_serverFn 端点 ID (从 assets/video.functions-*.js 提取)
    FN_SEARCH_ALL = '0ea055b1bc0887b5073a2593859b23183a23130e03aa0cd4acea5e53c72ee834'
    FN_SEARCH_ONE = '924908a6328d92c97055b1d048defe7b4f8102dee6907ac36be30470204c7535'
    FN_SOURCES    = '8fa43bc249007c84c4782b4237f5181449e18cd1723d5656f5f3c45c42e2daab'
    FN_CATEGORY   = '7e1a065dd0c4f105c2195db3af705adf30b75b211920223f5e3f9e62024916a0'
    FN_RECENT     = 'b42ee085174093515a801f91174a8e544266b579c62db3cecc6d13f99a17dd1d'
    FN_DETAIL     = '75b7f04db7f68591c58cd4cbf1a827b99ed16ccbe2e203c1af2d34358cab97df'
    FN_ALTS       = 'a406c5547c83f4497d0bfc2c25bba7383721c724b5b695d7fe56f3f6e22e7710'

    # 上游线路(顺序即站点自身的线路编号, 用于去重时的优先级)
    # 2026-09 实测: 对热门片走完整 m3u8 跳转链测速, 快且稳定的线路排前面,
    # ikun/zuid/ruyi/mdzy 可播且延时低, dyttzy 最慢且常 fetch failed 排最后
    SOURCES = ['ikun', 'zuid', 'ruyi', 'mdzy', 'zy360',
               'bfzy', 'lzi', 'ffzy', 'jisu', 'dyttzy']

    # 服务端支持的分类(实测: anime 不被 category 端点接受, 仅存在于首页 feed)
    CATEGORIES = [
        ('movie', '电影'),
        ('tv', '电视剧'),
        ('short', '短剧'),
        ('variety', '综艺'),
        ('documentary', '纪录片'),
        ('sports', '体育'),
    ]

    FILTER_AREA = [
        ('all', '全部地区'), ('mainland', '大陆'), ('hong-kong', '香港'),
        ('taiwan', '台湾'), ('japan', '日本'), ('korea', '韩国'),
        ('western', '欧美'), ('thailand', '泰国'), ('india', '印度'),
        ('other', '其他'),
    ]
    FILTER_YEAR = [
        ('all', '全部年份'), ('current', '今年'), ('last', '去年'),
        ('recent', '近五年'), ('2010s', '2010 年代'), ('2000s', '2000 年代'),
        ('1990s', '90 年代'), ('older', '更早'),
    ]

    # ---------------- 生命周期 ----------------

    def init(self, extend=''):
        self.ext = {}
        try:
            if extend:
                if isinstance(extend, dict):
                    self.ext = extend
                else:
                    s = str(extend).strip()
                    if s.startswith('{'):
                        self.ext = json.loads(s)
        except Exception:
            self.ext = {}

        # 是否在详情阶段实测线路延时并排序(默认开, 关掉可省 2~3 秒但线路顺序不再按快慢)
        self.probe_enabled = bool(self.ext.get('probe', True))
        # 单次 HTTP 探测超时(秒)
        try:
            self.probe_timeout = float(self.ext.get('probe_timeout', 1.8))
        except Exception:
            self.probe_timeout = 1.8
        # 单条线路的整链探测总预算(秒): 详情页探测阶段的硬上限
        # (健康线路实测全部在 1s 内完成探测, 预算只为兕住死线路的等待)
        try:
            self.probe_budget = float(self.ext.get('probe_budget', 2.2))
        except Exception:
            self.probe_budget = 2.2
        # 分类页软超时(秒): 到点即返回已到达的线路, 慢线路放弃。
        # 实测 dyttzy 分类接口固定 6.5s+ 且非网络原因, 3s 后其余 9 条线路均已到达
        try:
            self.cat_deadline = float(self.ext.get('cat_deadline', 3))
        except Exception:
            self.cat_deadline = 3.0
        # 最多纳入多少条线路(线路越多详情越慢)
        try:
            self.max_lines = max(1, min(20, int(self.ext.get('max_lines', 12))))
        except Exception:
            self.max_lines = 12
        # 并发度
        try:
            self.workers = max(2, min(16, int(self.ext.get('workers', 12))))
        except Exception:
            self.workers = 12

        self._local = threading.local()
        self._probe_cache = {}
        self._probe_lock = threading.Lock()
        self._domain_stat = {}  # host -> (ts, ok) 域名级存活判定, 死域名跨影片复用
        self._page_cache = {}
        self._detail_cache = {}
        self._home_cache = None  # (ts, out) 首页最近更新缓存
        self._cache_lock = threading.Lock()
        # 持久线程池: 线程复用 => 每线程的 Session(含 TLS 连接)跨调用复用,
        # 之前每次调用新建线程池, 10 条线路每次都要重新握手, 是分类/详情慢的主因之一
        self._executor = ThreadPoolExecutor(max_workers=self.workers,
                                            thread_name_prefix='zip0')
        # 后台预热: 提前完成与 zip0.com 的 TLS 握手, 首次真实请求不再付这笔钱
        self._executor.submit(self._warmup)
        return None

    def _warmup(self):
        try:
            self._call(self.FN_SOURCES, timeout=6)
        except Exception:
            pass

    def getName(self):
        return 'ZIP0'

    def isVideoFormat(self, url):
        v = str(url or '').lower()
        return any(m in v for m in ('.m3u8', '.mp4', '.m4v', '.flv', '.ts', '.mkv'))

    def manualVideoCheck(self):
        return False

    def destroy(self):
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass
        return None

    # ---------------- HTTP 基础设施 ----------------

    def _session(self):
        """requests.Session 非线程安全, 每线程独立一份。"""
        s = getattr(self._local, 'session', None)
        if s is None:
            s = requests.Session()
            s.verify = False
            s.headers.update({
                'User-Agent': self.UA,
                'Accept-Language': 'zh-CN,zh;q=0.9',
            })
            self._local.session = s
        return s

    # ---- seroval 编解码 ----
    # 请求侧只需要 object/array/string/number/bool/null 这几种节点。

    def _ser(self, val, ctr=None):
        if ctr is None:
            ctr = [0]
        if isinstance(val, bool):
            return {'t': 2, 's': 2 if val else 3}
        if val is None:
            return {'t': 2, 's': 1}
        if isinstance(val, str):
            return {'t': 1, 's': val}
        if isinstance(val, (int, float)):
            return {'t': 0, 's': val}
        if isinstance(val, (list, tuple)):
            node = {'t': 9, 'i': ctr[0], 'a': [], 'o': 0}
            ctr[0] += 1
            node['a'] = [self._ser(v, ctr) for v in val]
            return node
        if isinstance(val, dict):
            node = {'t': 10, 'i': ctr[0], 'p': {'k': [], 'v': []}, 'o': 0}
            ctr[0] += 1
            for k, v in val.items():
                node['p']['k'].append(str(k))
                node['p']['v'].append(self._ser(v, ctr))
            return node
        return {'t': 1, 's': str(val)}

    _UNESC = {
        '\\\\': '\\', '\\"': '"', '\\n': '\n', '\\r': '\r', '\\b': '\b',
        '\\t': '\t', '\\f': '\f', '\\u2028': '\u2028', '\\u2029': '\u2029',
        '\\x3C': '<',
    }

    def _unesc(self, s):
        s = str(s)
        if '\\' not in s:
            return s
        return re.sub(r'\\(?:\\|"|n|r|b|t|f|u2028|u2029|x3C)',
                      lambda m: self._UNESC.get(m.group(0), m.group(0)), s)

    def _deser(self, node, refs=None):
        if refs is None:
            refs = {}
        if not isinstance(node, dict):
            return None
        t = node.get('t')
        if t == 0:
            return node.get('s')
        if t == 1:
            return self._unesc(node.get('s', ''))
        if t == 2:
            return {0: None, 1: None, 2: True, 3: False,
                    4: 0, 5: float('inf'), 6: float('-inf')}.get(node.get('s'))
        if t == 4:
            return refs.get(node.get('i'))
        if t == 9:
            arr = []
            if node.get('i') is not None:
                refs[node['i']] = arr
            for x in (node.get('a') or []):
                arr.append(self._deser(x, refs) if x else None)
            return arr
        if t in (10, 11):
            obj = {}
            if node.get('i') is not None:
                refs[node['i']] = obj
            p = node.get('p') or {}
            ks, vs = p.get('k') or [], p.get('v') or []
            for i, k in enumerate(ks):
                obj[self._unesc(k)] = self._deser(vs[i], refs) if i < len(vs) else None
            return obj
        if t == 25:  # 插件序列化, 站点用它包装 Error
            out = {'__error': True}
            for k, v in (node.get('s') or {}).items():
                out[k] = self._deser(v, refs)
            return out
        return None

    def _call(self, fn_id, data=None, method='GET', timeout=15):
        """调用 /_serverFn 端点, 返回 (result, error_message)。"""
        url = '%s/_serverFn/%s' % (self.BASE_URL, fn_id)
        headers = {
            # 这两个头缺任意一个都会被服务端 403 掉
            'x-tsr-serverFn': 'true',
            'sec-fetch-site': 'same-origin',
            'Accept': 'application/json',
            'Referer': self.BASE_URL + '/',
        }
        try:
            payload = json.dumps(
                {'t': self._ser({'data': data} if data is not None else {}),
                 'f': 1023, 'm': []},
                ensure_ascii=False, separators=(',', ':'))
            s = self._session()
            if method == 'POST':
                headers['Content-Type'] = 'application/json'
                r = s.post(url, data=payload.encode('utf-8'),
                           headers=headers, timeout=timeout)
            else:
                q = '' if data is None else '?payload=' + urllib.parse.quote(payload)
                r = s.get(url + q, headers=headers, timeout=timeout)
            if r.status_code != 200:
                return None, 'HTTP %s' % r.status_code
            envelope = self._deser(json.loads(r.text))
        except Exception as e:
            return None, str(e)

        if not isinstance(envelope, dict):
            return None, 'bad envelope'
        err = envelope.get('error')
        if isinstance(err, dict) and err.get('message'):
            return None, str(err.get('message'))
        return envelope.get('result'), None

    # ---------------- 数据整理 ----------------

    @staticmethod
    def _norm_key(item):
        """站点自身的去重口径: 去空格小写标题 + 年份。"""
        title = str(item.get('title') or '').strip()
        title = re.sub(r'\s+', '', title).lower()
        return '%s:%s' % (title, item.get('year') or '')

    @staticmethod
    def _ts(item):
        s = str(item.get('updatedAt') or '').replace(' ', 'T')
        try:
            return time.mktime(time.strptime(s[:19], '%Y-%m-%dT%H:%M:%S'))
        except Exception:
            return 0.0

    def _dedupe(self, items):
        """按 标题:年份 合并跨线路的同一影片, 保留线路编号靠前的那条并补全字段。"""
        order = {s: i for i, s in enumerate(self.SOURCES)}
        merged = {}
        for it in items:
            if not isinstance(it, dict) or not it.get('id') or not it.get('title'):
                continue
            k = self._norm_key(it)
            old = merged.get(k)
            if old is None:
                merged[k] = dict(it)
                continue
            keep, drop = old, it
            if order.get(it.get('source'), 99) < order.get(old.get('source'), 99):
                keep, drop = it, old
            out = dict(keep)
            for f in ('poster', 'remarks', 'category', 'area', 'language', 'score'):
                if not out.get(f):
                    out[f] = drop.get(f)
            if str(out.get('year') or '') in ('', '未知'):
                out['year'] = drop.get('year')
            try:
                out['episodeCount'] = max(int(keep.get('episodeCount') or 0),
                                          int(drop.get('episodeCount') or 0))
            except Exception:
                pass
            if self._ts(drop) > self._ts(keep):
                out['updatedAt'] = drop.get('updatedAt')
            merged[k] = out
        return list(merged.values())

    def _vod(self, item):
        remark = str(item.get('remarks') or '').strip()
        ep = item.get('episodeCount')
        if not remark and ep:
            remark = '共 %s 集' % ep
        score = str(item.get('score') or '').strip()
        if score and score not in ('0.0', '0'):
            remark = ('%s %s分' % (remark, score)).strip()
        # 把标题嵌进 vod_id: 详情阶段就能直接按标题搜同片其它线路,
        # 不必先串行拉一次主详情才知道标题(省约 1 秒)
        title = str(item.get('title') or '').strip()
        return {
            'vod_id': '%s@%s@%s' % (item.get('source'), item.get('id'),
                                    urllib.parse.quote(title, safe='')),
            'vod_name': title,
            'vod_pic': str(item.get('poster') or '').strip(),
            'vod_remarks': remark,
            'vod_year': str(item.get('year') or ''),
        }

    def _parallel(self, func, args_list, workers=None):
        """持久线程池上的有序 map。线程跨调用复用, TLS 连接跟着复用。"""
        args_list = list(args_list)
        if not args_list:
            return []
        return list(self._executor.map(func, args_list))

    def _batch(self, func, args_list, deadline_s):
        """
        带软超时的并发批: 到点返回已完成的, 未完成的放弃(不取消, 任其自然结束)。
        分类页用: 个别慢线路(实测 zy360 可拖到 9s+)不再拖住整体。
        """
        args_list = list(args_list)
        if not args_list:
            return []
        futs = [self._executor.submit(func, a) for a in args_list]
        wait(futs, timeout=max(0.2, deadline_s))
        out = []
        for f in futs:
            if f.done() and not f.exception():
                out.append(f.result())
        return out

    # ---------------- 首页 ----------------

    def homeContent(self, filter):
        classes = [{'type_id': t, 'type_name': n} for t, n in self.CATEGORIES]
        result = {'class': classes}
        if filter:
            f = [
                {'key': 'area', 'name': '地区',
                 'value': [{'n': n, 'v': v} for v, n in self.FILTER_AREA]},
                {'key': 'year', 'name': '年份',
                 'value': [{'n': n, 'v': v} for v, n in self.FILTER_YEAR]},
            ]
            result['filters'] = {t: f for t, _ in self.CATEGORIES}
        return result

    _HOME_TTL = 300  # 首页缓存 5 分钟: 重进首页/来回切换直接命中, 不重复请求

    def homeVideoContent(self):
        now = time.time()
        with self._cache_lock:
            hit = self._home_cache
        if hit and now - hit[0] < self._HOME_TTL:
            return hit[1]
        try:
            res, err = self._call(self.FN_RECENT, timeout=15)
            if err or not isinstance(res, dict):
                return {'list': []}
            items = []
            for sec in (res.get('sections') or {}).values():
                if isinstance(sec, list):
                    items.extend(sec)
            items = self._dedupe(items)
            items.sort(key=self._ts, reverse=True)
            out = {'list': [self._vod(i) for i in items]}
            with self._cache_lock:
                self._home_cache = (now, out)
            return out
        except Exception:
            return {'list': []}

    # ---------------- 分类 ----------------

    def categoryContent(self, tid, pg, filter, extend):
        cat = str(tid or '').strip()
        valid = {t for t, _ in self.CATEGORIES}
        if cat not in valid:
            return {'list': [], 'page': 1, 'pagecount': 1, 'limit': 0, 'total': 0}
        try:
            page = max(1, int(str(pg or 1)))
        except Exception:
            page = 1

        ext = extend if isinstance(extend, dict) else {}
        flt = {}
        for k in ('area', 'year'):
            v = str(ext.get(k) or '').strip()
            if v and v != 'all':
                flt[k] = v

        # 短 TTL 页缓存: TVBox 里来回翻页/切筛选时直接命中
        ckey = ('cat', cat, page, tuple(sorted(flt.items())))
        now = time.time()
        with self._cache_lock:
            hit = self._page_cache.get(ckey)
        if hit and now - hit[0] < 120:
            return hit[1]

        def one(src):
            data = {'category': cat, 'source': src, 'page': page}
            data.update(flt)
            res, err = self._call(self.FN_CATEGORY, data, timeout=max(4, self.cat_deadline + 2))
            if err or not isinstance(res, dict):
                return ([], False)
            return (res.get('items') or [], bool(res.get('hasMore')))

        # 软超时: 到点返回已到达的线路, 慢线路(如 zy360 偶发 9s+)直接放弃
        pairs = self._batch(one, self.SOURCES, self.cat_deadline)

        items, has_more = [], False
        for lst, more in pairs:
            items.extend(lst)
            has_more = has_more or more

        items = self._dedupe(items)
        items.sort(key=self._ts, reverse=True)
        vods = [self._vod(i) for i in items]
        # 上游只给 hasMore 不给总数: 永远允许翻下一页, 上游返回空时自然收口
        pagecount = page + 1 if (vods or has_more) else max(1, page)
        out = {'list': vods, 'page': page, 'pagecount': pagecount,
               'limit': len(vods), 'total': len(vods) * pagecount}
        with self._cache_lock:
            self._page_cache[ckey] = (now, out)
        return out

    # ---------------- 搜索 ----------------

    def searchContent(self, key, quick, pg='1'):
        kw = str(key or '').strip()
        if not kw:
            return {'list': []}
        try:
            page = max(1, int(str(pg or 1)))
        except Exception:
            page = 1
        # 聚合搜索端点一次返回全部线路的命中, 不支持翻页
        if page > 1:
            return {'list': [], 'page': page, 'pagecount': page}

        res, err = self._call(self.FN_SEARCH_ALL, {'query': kw[:60]}, timeout=25)
        items = []
        if not err and isinstance(res, dict):
            items = res.get('items') or []
        if not items:
            # 聚合端点异常时退化为逐线路搜索
            def one(src):
                r, e = self._call(self.FN_SEARCH_ONE, {'query': kw[:60], 'source': src}, timeout=15)
                return (r or {}).get('items') or [] if not e else []
            try:
                for lst in self._parallel(one, self.SOURCES):
                    items.extend(lst)
            except Exception:
                pass

        items = self._dedupe(items)
        # 标题完全命中的排前面, 其次按更新时间
        want = re.sub(r'\s+', '', kw).lower()

        def rank(it):
            t = re.sub(r'\s+', '', str(it.get('title') or '')).lower()
            if t == want:
                return 0
            if t.startswith(want):
                return 1
            if want in t:
                return 2
            return 3

        items.sort(key=lambda i: (rank(i), -self._ts(i)))
        return {'list': [self._vod(i) for i in items],
                'page': 1, 'pagecount': 1, 'limit': len(items), 'total': len(items)}

    # ---------------- 线路探测 ----------------

    def _probe(self, url, depth=0, spent=0.0, deadline=None):
        """
        走完 m3u8 跳转链, 返回 (可用, 累计毫秒)。
        多数线路的 index.m3u8 是 master, 只测第一层会误判成"可用但没内容"。
        deadline 限制整链总耗时, 避免个别慢线路拖住详情页。
        """
        if depth > 3 or not url:
            return (False, spent)
        if deadline is None:
            deadline = time.time() + self.probe_budget
        left = deadline - time.time()
        if left <= 0:
            return (False, spent)
        t0 = time.time()
        try:
            r = self._session().get(
                url,
                headers={'User-Agent': self.UA, 'Accept': '*/*'},
                timeout=(min(self.probe_timeout, max(0.5, left)),
                         min(self.probe_timeout, max(0.5, left))),
                allow_redirects=True, stream=True)
            if r.status_code != 200:
                r.close()
                return (False, spent + (time.time() - t0) * 1000.0)
            # 逐块读并盯住 deadline: requests 的 read timeout 是"单次 socket 读"超时,
            # 慢速涓流响应能远超它, 实测某线路整链耗到 11 秒。这里做硬上限。
            buf, cap = b'', 512 * 1024
            for chunk in r.iter_content(8192):
                if chunk:
                    buf += chunk
                if len(buf) >= cap or time.time() > deadline:
                    break
            r.close()
            cost = (time.time() - t0) * 1000.0
            body = buf.decode('utf-8', 'ignore')
            if not body.lstrip().startswith('#EXTM3U'):
                return (False, spent + cost)
            lines = [l.strip() for l in body.splitlines()
                     if l.strip() and not l.strip().startswith('#')]
            if not lines:
                return (False, spent + cost)
            if re.search(r'\.m3u8(\?|$)', lines[0], re.I):
                return self._probe(urllib.parse.urljoin(url, lines[0]),
                                   depth + 1, spent + cost, deadline)
            return (True, spent + cost)
        except Exception:
            return (False, spent + (time.time() - t0) * 1000.0)

    _PROBE_TTL = 600      # 探测结果缓存 10 分钟
    _DOMAIN_TTL = 900     # 域名级“死判定”复用 15 分钟(活域名每次仍实测)

    @staticmethod
    def _host_of(url):
        try:
            return urllib.parse.urlsplit(url).netloc.lower()
        except Exception:
            return ''

    def _probe_cached(self, url):
        now = time.time()
        with self._probe_lock:
            hit = self._probe_cache.get(url)
        if hit is not None and now - hit[2] < self._PROBE_TTL:
            return (hit[0], hit[1])
        # 域名级快速失败: 死 CDN(如 bfllvip 对部分出口 IP 全域 404)对每部片都是死的,
        # 与其每次等它超时, 不如直接复用判定, 详情页探测阶段就不必等满预算
        host = self._host_of(url)
        if host:
            with self._probe_lock:
                ds = self._domain_stat.get(host)
            if ds and ds[1] is False and now - ds[0] < self._DOMAIN_TTL:
                return (False, 0.0)
        out = self._probe(url)
        with self._probe_lock:
            self._probe_cache[url] = (out[0], out[1], now)
            if host:
                old = self._domain_stat.get(host)
                if out[0]:
                    # 活判定不缓存: 延时逐片实测才有意义
                    self._domain_stat[host] = (now, True)
                elif not old or now - old[0] >= self._DOMAIN_TTL:
                    self._domain_stat[host] = (now, False)
        return out

    # ---------------- 详情 ----------------

    def detailContent(self, ids):
        raw = ids[0] if isinstance(ids, (list, tuple)) and ids else ids
        raw = str(raw or '').strip()
        parts = raw.split('@', 2)
        if len(parts) >= 2:
            source, vid = parts[0].strip(), parts[1].strip()
            title_q = parts[2] if len(parts) > 2 else ''
        else:
            source, vid, title_q = raw, '', ''
        if not source or not vid:
            return {'list': []}
        # 标题由列表/搜索页嵌进 vod_id, 这里无需先拉主详情就能并发查其它线路
        list_title = urllib.parse.unquote(title_q or '')

        # 详情缓存: 重进同一部片 / 换集返回时直接命中
        dkey = raw
        now = time.time()
        with self._cache_lock:
            hit = self._detail_cache.get(dkey)
        if hit and now - hit[0] < 600:
            return hit[1]

        # ---- 阶段 1(串行): 主详情 -> ALTS -> 标题搜索(仅当 ALTS 不足) ----
        # 实测 ALTS/SEARCH 是服务端扇出端点, 并发调用会互相争抢上游池
        # (串行 0.7s 的 ALTS 在三路并发里 5s 都完不成), 所以扇出类调用必须单飞
        main, err = self._call(self.FN_DETAIL, {'source': source, 'id': vid}, timeout=5)
        if (err or not isinstance(main, dict)) and len(list_title.strip()) >= 2:
            # 主线路所属源临时故障(实测 dyttzy 会整体 fetch failed):
            # 按标题搜同一部片, 切到其它源, 详情页不至于整个打不开
            try:
                r, e2 = self._call(self.FN_SEARCH_ALL,
                                   {'query': list_title[:60]}, timeout=5)
                if not e2 and isinstance(r, dict):
                    want = re.sub(r'\s+', '', list_title).lower()
                    best = None
                    for x in (r.get('items') or []):
                        if not isinstance(x, dict):
                            continue
                        if re.sub(r'\s+', '', str(x.get('title') or '')).lower() != want:
                            continue
                        try:
                            ec = int(x.get('episodeCount') or 0)
                        except Exception:
                            ec = 0
                        if best is None or ec > best[0]:
                            best = (ec, x)
                    if best:
                        ns, nid = best[1].get('source'), best[1].get('id')
                        if ns and nid:
                            m2, e3 = self._call(self.FN_DETAIL,
                                                {'source': ns, 'id': nid}, timeout=5)
                            if not e3 and isinstance(m2, dict):
                                source, vid, main = ns, nid, m2
                                err = None
            except Exception:
                pass
        if err or not isinstance(main, dict):
            return {'list': []}

        alts = []
        try:
            a, e = self._call(self.FN_ALTS, {'source': source, 'id': vid}, timeout=4)
            if not e and isinstance(a, dict):
                alts = [x for x in (a.get('alternatives') or []) if isinstance(x, dict)]
        except Exception:
            alts = []

        # FN_ALTS 会漏线路(实测部分综艺/纪录片返回空但搜索能找到 9 条),
        # 且搜索端点慢且抖(0.6~10s), 只在 ALTS 不足时才补一砍
        if len(alts) < 4 and list_title:
            try:
                r, e = self._call(self.FN_SEARCH_ALL,
                                  {'query': list_title[:60]}, timeout=5)
                if not e and isinstance(r, dict):
                    want = re.sub(r'\s+', '', list_title).lower()
                    alts.extend(x for x in (r.get('items') or [])
                                if isinstance(x, dict)
                                and re.sub(r'\s+', '',
                                           str(x.get('title') or '')).lower() == want)
            except Exception:
                pass

        title = str(main.get('title') or '').strip()
        main_year = str(main.get('year') or '').strip()
        # 年份不一致的同名条目仍然收下, 但标记为次级:
        # 长跑综艺常见同名不同年(站点自身按 标题:年份 去重会把它们当两部片),
        # 这类线路能播且内容相符; 而同名翻拍则确实是另一部片, 所以只作备选。
        for x in alts:
            y = str(x.get('year') or '').strip()
            x['__weak'] = bool(main_year and y and '未知' not in (y, main_year)
                               and y != main_year)
        # 年份一致的排前面, 年份不同的同名条目放后面
        alts.sort(key=lambda x: 1 if x.get('__weak') else 0)

        # 主线路 + 备选线路, 去掉重复的 source@id
        targets, seen = [], set()
        for cand in [{'source': source, 'id': vid, '__weak': False}] + \
                    [{'source': x.get('source'), 'id': x.get('id'),
                      '__weak': bool(x.get('__weak'))} for x in alts]:
            if not cand.get('source') or not cand.get('id'):
                continue
            k = '%s@%s' % (cand['source'], cand['id'])
            if k in seen:
                continue
            seen.add(k)
            targets.append(cand)
        targets = targets[:self.max_lines]

        def fetch(cand):
            if cand['source'] == source and str(cand['id']) == vid:
                return (cand, main)
            d, e = self._call(self.FN_DETAIL,
                              {'source': cand['source'], 'id': cand['id']}, timeout=8)
            return (cand, None if e else d)

        # 软超时: 个别线路详情拖住时放弃, 主线路已在手
        fetched = self._batch(fetch, targets, self.cat_deadline + 2)

        lines = []
        for cand, det in fetched:
            if not isinstance(det, dict):
                continue
            eps = [e for e in (det.get('episodes') or [])
                   if isinstance(e, dict) and e.get('url')]
            if not eps:
                continue
            lines.append({
                'source': cand['source'],
                'name': str(det.get('sourceName') or cand['source']),
                'episodes': eps,
                'detail': det,
                'weak': bool(cand.get('__weak')),
            })
        if not lines:
            return {'list': []}

        # 按实测延时升序: 最快的线路排最前, 探测失败的线路排最后。
        # 年份不一致的同名线路(weak)统一压到同组之后, 避免翻拍片顶掉正片。
        if self.probe_enabled and len(lines) > 1:
            def measure(ln):
                ok, ms = self._probe_cached(ln['episodes'][0]['url'])
                return (ln, ok, ms)
            try:
                measured = self._parallel(measure, lines, workers=min(12, len(lines)))
            except Exception:
                measured = [(ln, True, 0.0) for ln in lines]
            measured.sort(key=lambda x: (0 if x[1] else 1,
                                         1 if x[0].get('weak') else 0,
                                         x[2]))
            ordered = []
            for ln, ok, ms in measured:
                ln = dict(ln)
                if ok:
                    ln['name'] = '%s %dms' % (ln['name'], int(ms))
                else:
                    ln['name'] = '%s 备用' % ln['name']
                ordered.append(ln)
            lines = ordered
        else:
            lines.sort(key=lambda ln: 1 if ln.get('weak') else 0)

        froms, urls = [], []
        used = set()
        for ln in lines:
            label = ln['name'].replace('$', ' ').replace('#', ' ').strip()
            base = label or ln['source']
            n = 2
            while label in used:
                label = '%s(%d)' % (base, n)
                n += 1
            used.add(label)
            froms.append(label)

            parts = []
            for idx, ep in enumerate(ln['episodes'], 1):
                name = str(ep.get('name') or '').strip() or ('第%d集' % idx)
                name = name.replace('$', ' ').replace('#', ' ').replace('|', ' ')
                url = str(ep.get('url') or '').strip()
                if not url:
                    continue
                parts.append('%s$%s' % (name, url))
            urls.append('#'.join(parts))

        # 元信息一律取用户点进来的那条线路, 不受排序影响
        d = main if isinstance(main, dict) else lines[0]['detail']
        vod = {
            'vod_id': raw,
            'vod_name': str(d.get('title') or '').strip(),
            'vod_pic': str(d.get('poster') or '').strip(),
            'type_name': str(d.get('category') or '').strip(),
            'vod_year': str(d.get('year') or '').strip(),
            'vod_area': str(d.get('area') or '').strip(),
            'vod_lang': str(d.get('language') or '').strip(),
            'vod_remarks': str(d.get('remarks') or '').strip(),
            'vod_actor': str(d.get('actors') or '').strip(),
            'vod_director': str(d.get('director') or '').strip(),
            'vod_content': re.sub(r'<[^>]+>', '', str(d.get('description') or '')).strip(),
            'vod_play_from': '$$$'.join(froms),
            'vod_play_url': '$$$'.join(urls),
        }
        out = {'list': [vod]}
        with self._cache_lock:
            self._detail_cache[dkey] = (time.time(), out)
            # 防膨胀: 只保留最近 50 部
            if len(self._detail_cache) > 50:
                for k in sorted(self._detail_cache,
                                key=lambda k: self._detail_cache[k][0])[:-50]:
                    self._detail_cache.pop(k, None)
        return out

    # ---------------- 播放 ----------------

    def playerContent(self, flag, id, vipFlags):
        url = str(id or '').strip()
        headers = {'User-Agent': self.UA}
        if not url:
            return {'parse': 0, 'playUrl': '', 'url': '', 'header': headers}
        # 详情阶段已把真实 m3u8 直接写进选集, 这里无需再解析, 起播零等待
        if url.startswith('http'):
            return {'parse': 0, 'playUrl': '', 'url': url,
                    'header': headers, 'format': 'application/x-mpegURL'}
        return {'parse': 1, 'playUrl': '', 'url': url, 'header': headers}

    def localProxy(self, param):
        return [404, 'text/plain; charset=utf-8', b'not supported']

    def liveContent(self, url):
        return ''
