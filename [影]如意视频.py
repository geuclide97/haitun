import re
import os
import json
import time
import threading
import urllib.parse
import importlib.util

import requests

# 只有存在 Brotli 解码器时才声明 br，否则响应体无法解压
_ACCEPT_ENCODING = "gzip, deflate"
if importlib.util.find_spec("brotli") is not None or importlib.util.find_spec("brotlicffi") is not None:
    _ACCEPT_ENCODING += ", br"


class Spider:
    """如意视频（rysp.tv）五壳通用独立 Spider"""

    _TIMEOUT = 20
    _PAGE_SIZE = 24
    # 线路名与 chapters.resource_url 的 key：1=普快线路，21=超快线路
    _SOURCES = [("普快线路", "1"), ("超快线路", "21")]
    _SORTS = [("最新", "new"), ("人气", "hot"), ("评分", "score")]
    _FILTER_FIELDS = [("tag", "类型"), ("area", "地区"), ("year", "年代"),
                      ("source", "线路"), ("status", "状态")]
    _CHANNELS = [("2", "电视剧"), ("1", "电影"), ("3", "综艺"),
                 ("4", "动漫"), ("32", "纪录片")]
    _AREA_NAMES = ("国产", "欧美", "日本", "香港", "韩国", "泰国",
                   "台湾", "英国", "东南亚", "其它", "其他")
    _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")

    def __init__(self):
        self.host = "https://rysp.tv"
        self.ua = self._UA
        self.headers = {
            "User-Agent": self.ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": _ACCEPT_ENCODING,
            "Accept-Priority": "u=0, i",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="154", "Google Chrome";v="154"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "Referer": self.host + "/",
        }
        self.categories = [{"type_id": cid, "type_name": name} for cid, name in self._CHANNELS]
        self.filters = {cid: [] for cid, _ in self._CHANNELS}
        self._cookie_lock = threading.Lock()
        self._session = None
        self._sess = None
        self._filter_cache_file = None
        self._filter_cache_data = None
        self._hot_data = None

    # ---------- 基础接口 ----------
    def getName(self):
        return "如意视频"

    def getDependence(self):
        return []

    def init(self, extend=""):
        if isinstance(extend, str) and extend.strip()[:1] == "{":
            try:
                extend = json.loads(extend)
            except Exception:
                extend = dict()
        if not isinstance(extend, dict):
            extend = dict()
        if extend.get("host"):
            self.host = str(extend["host"]).rstrip("/")
            self.headers["Referer"] = self.host + "/"
        self.s = self.session
        self.session = self.sess = self.s
        self._filter_cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                               ".rysp_filter_cache.json")

    # ---------- 会话 ----------
    @property
    def session(self):
        if self._session is None:
            with self._cookie_lock:
                if self._session is None:
                    self._session = requests.Session()
                    self._session.headers.clear()
                    self._session.headers.update(self.headers)
        return self._session

    @session.setter
    def session(self, value):
        self._session = value

    @property
    def sess(self):
        return self.session

    @sess.setter
    def sess(self, value):
        self._session = value

    def _get(self, path, referer=None, xhr=False):
        hd = dict(self.headers)
        hd["Referer"] = referer or (self.host + "/video/list?channel_id=1")
        if xhr:
            hd["X-Requested-With"] = "XMLHttpRequest"
            hd["Accept"] = "application/json, text/javascript, */*; q=0.01"
            hd["Sec-Fetch-Dest"] = "empty"
            hd["Sec-Fetch-Mode"] = "cors"
            hd["Sec-Fetch-Site"] = "same-origin"
            hd.pop("Sec-Fetch-User", None)
            hd.pop("Upgrade-Insecure-Requests", None)
        last = None
        for attempt in range(3):
            try:
                r = self.session.get(self.host + path, headers=hd, timeout=self._TIMEOUT)
                r.raise_for_status()
                r.encoding = r.apparent_encoding or "utf-8"
                return r.text
            except Exception as e:
                last = e
                time.sleep(0.6 * (attempt + 1))
        raise last

    def _json(self, path, referer=None):
        return json.loads(self._get(path, referer, xhr=True))

    def _cate_json(self, **params):
        q = {"page_num": 1, "page_size": self._PAGE_SIZE, "sort": "new", "sorttype": "desc",
             "channel_id": "", "tag": "", "area": "", "year": "", "source": "", "status": ""}
        q.update(params)
        return self._json("/video/refresh-cate?" + urllib.parse.urlencode(q))["data"]

    # ---------- 筛选器 ----------
    def _read_filter_box(self, search_box):
        box = {}
        for grp in search_box:
            field = grp.get("field")
            if field == "sort" or field in box:
                continue
            box[field] = [{"n": x["display"], "v": str(x["value"])}
                         for x in grp["list"] if x["display"] != "全部"]
        return box

    def _build_filters(self):
        box = self._read_filter_box(self._cate_json(page_size=1)["search_box"])
        out = {}
        for cid, _ in self._CHANNELS:
            groups = [{"key": "sort", "name": "排序", "value":
                       [{"n": n, "v": v} for n, v in self._SORTS]}]
            for field, label in self._FILTER_FIELDS:
                values = box.get(field) or self._read_filter_box(
                    self._cate_json(channel_id=cid, page_size=1)["search_box"]).get(field) or []
                if values:
                    groups.append({"key": field, "name": label, "value": values})
            out[cid] = groups
        return out

    def _load_filters(self):
        if self._filter_cache_data is not None:
            return self._filter_cache_data
        path = self._filter_cache_file
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("date") == time.strftime("%Y%m%d") and isinstance(data.get("filters"), dict):
                    self._filter_cache_data = data["filters"]
                    return self._filter_cache_data
            except Exception:
                pass
        out = self._build_filters()
        self._filter_cache_data = out
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"date": time.strftime("%Y%m%d"), "filters": out}, f, ensure_ascii=False)
            except Exception:
                pass
        return out

    # ---------- 首页 ----------
    def homeContent(self, filter=None):
        try:
            self.filters = self._load_filters()
        except Exception:
            self.filters = {cid: [] for cid, _ in self._CHANNELS}
        return {"class": self.categories, "filters": self.filters}

    def homeVideoContent(self):
        if self._hot_data is None:
            self._hot_data = self._cate_json(page_size=24, sort="hot")["list"]
        return {"list": self._cards(self._hot_data)}

    # ---------- 分类列表 ----------
    def categoryContent(self, tid, pg=1, filter=None, extend=None):
        if not isinstance(extend, dict):
            extend = dict()
        page = max(int(pg or 1), 1)
        tid = str(tid or "").strip()
        data = self._cate_json(channel_id=tid, page_num=page,
                               sort=extend.get("sort") or "new",
                               tag=extend.get("tag", ""), area=extend.get("area", ""),
                               year=extend.get("year", ""), source=extend.get("source", ""),
                               status=extend.get("status", ""))
        cards = self._cards(data["list"])
        total = data.get("total_count") or 0
        return {"page": data.get("current_page") or page,
                "pagecount": data.get("total_page") or 1,
                "limit": self._PAGE_SIZE, "total": total,
                "list": cards}

    # ---------- 搜索 ----------
    def searchContent(self, key, quick=False, pg="1"):
        page = max(int(pg or 1), 1)
        q = urllib.parse.urlencode({"keyword": key, "page_num": page,
                                    "page_size": self._PAGE_SIZE, "sort": "new",
                                    "sorttype": "desc", "type": ""})
        try:
            html = self._get("/video/refresh-video?" + q, xhr=True,
                             referer=self.host + "/video/search-result?keyword=" + urllib.parse.quote(key))
        except Exception:
            return {"list": [], "page": page, "pagecount": 1,
                    "limit": self._PAGE_SIZE, "total": 0}
        cards = self._cards_from_html(html)
        return {"list": cards, "page": page, "pagecount": page + (1 if cards else 0),
                "limit": self._PAGE_SIZE, "total": len(cards)}

    # ---------- 详情 ----------
    def detailContent(self, ids):
        if isinstance(ids, (list, tuple)):
            vid = str(ids[0]) if ids else ""
        else:
            vid = str(ids or "")
        vid = vid.split("$$")[0].strip()
        page = self._get("/video/detail?video_id=" + vid)
        info = self._info_of(page, vid)
        chapters = self._chapters_of(page)
        names = []
        groups = []
        for name, key in self._SOURCES:
            eps = []
            for idx, ch in enumerate(chapters, 1):
                url = (ch.get("resource_url") or {}).get(key)
                if url:
                    eps.append("%s$%s" % (self._ep_name(ch, idx), url))
            if eps:
                names.append(name)
                groups.append("#".join(eps))
        return {"list": [{
            "vod_id": vid,
            "vod_name": info["name"],
            "vod_pic": info["pic"],
            "vod_remarks": info["score"],
            "type_name": info["types"],
            "vod_class": info["types"],
            "vod_year": info["year"],
            "vod_area": info["area"],
            "vod_actor": info["actor"],
            "vod_director": info["director"],
            "vod_content": info["intro"],
            "vod_play_from": "$$$".join(names),
            "vod_play_url": "$$$".join(groups),
        }]}

    def _chapters_of(self, page):
        # 详情页内嵌完整章节数组：// console.log([{...}])
        i = page.find("// console.log([{")
        if i < 0:
            return []
        start = page.index("[", i)
        end = self._match_bracket(page, start)
        return json.loads(page[start:end + 1])

    def _match_bracket(self, s, start):
        depth = 0
        in_str = False
        esc = False
        for k in range(start, len(s)):
            c = s[k]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    return k
        raise ValueError("chapters 数组未闭合")

    def _info_of(self, page, vid):
        name = ""
        m = re.search(r'<div class="play-name"[^>]*>\s*([^<]{1,80}?)\s*</div>', page)
        if m:
            name = m.group(1)
        else:
            m = re.search(r"<title>\s*(.*?)\s*-\s*如意视频", page, re.S)
            name = m.group(1).strip() if m else vid
        tags = []
        m = re.search(r'<div class="GNbox-type"[^>]*>([\s\S]*?)</div>', page)
        if m:
            tags = [self._text(x) for x in re.findall(r"<span[^>]*>([\s\S]*?)</span>", m.group(1))]
        tags = [t for t in tags if t]
        year = next((t for t in tags if re.fullmatch(r"\d{4}", t)), "")
        area = ""
        cls = []
        for t in tags:
            if t == year:
                continue
            if t in self._AREA_NAMES:
                area = t
            else:
                cls.append(t)
        pic = ""
        m = re.search(r'<div class="GNbox-xq-img"[\s\S]{0,400}?originalSrc="([^"]+)"', page)
        if m:
            pic = self._fix_url(m.group(1))
        score = ""
        m = re.search(r'<div class="GNbox-PF"[^>]*>\s*<span>([\d.]+)</span>', page)
        if m:
            score = m.group(1)
        return {"name": name, "pic": pic, "score": score,
                "types": " ".join(cls), "year": year, "area": area,
                "actor": self._field(page, "主演"),
                "director": self._field(page, "导演"),
                "intro": self._field(page, "简介")}

    def _field(self, page, label):
        m = re.search(label + r"[：:]\s*<span>\s*([\s\S]*?)\s*</span>", page)
        return self._text(m.group(1)) if m else ""

    def _ep_name(self, ch, index):
        # 选集只保留集名，不附带时长
        name = (ch.get("title") or "").strip()
        return name or ("第%02d集" % index)

    # ---------- 播放 ----------
    def _media_header(self):
        # 取流请求模拟桌面浏览器直接访问媒体地址
        return {"User-Agent": self._UA,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": self.host,
                "Referer": self.host + "/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site"}

    def playerContent(self, flag, id, vipFlags=None):
        url = str(id or "")
        if url.startswith("//"):
            url = "https:" + url
        media = self._is_media(url)
        return {"parse": 0 if media else 1, "jx": 0, "playUrl": "",
                "url": url,
                "header": self._media_header(),
                "format": "application/x-mpegURL" if ".m3u8" in url.lower() else ""}

    def localProxy(self, param):
        if isinstance(param, str):
            try:
                param = json.loads(param)
            except Exception:
                param = dict()
        if not isinstance(param, dict):
            param = dict()
        url = str(param.get("url") or "")
        if not url:
            return [404, "text/plain", b"Not Found", dict()]
        if url.startswith("//"):
            url = "https:" + url
        try:
            r = self.session.get(url, headers=self._media_header(),
                                 timeout=self._TIMEOUT)
        except Exception:
            return [502, "text/plain", b"Fetch Failed", dict()]
        if r.status_code != 200:
            return [r.status_code, "text/plain", b"Fetch Failed", dict()]
        mime = "application/vnd.apple.mpegurl" if ".m3u8" in url.lower() else "application/octet-stream"
        return [200, mime, r.content, {"Access-Control-Allow-Origin": "*"}]

    def _is_media(self, url):
        low = (url or "").lower()
        return ".m3u8" in low or ".mp4" in low or ".flv" in low or ".ts" in low

    def isVideoFormat(self, url):
        return self._is_media(url)

    def manualVideoCheck(self):
        return False

    def action(self, action):
        return dict()

    def destroy(self):
        self._session = None
        self._filter_cache_data = None
        self._hot_data = None
        return None

    # ---------- 辅助 ----------
    def _fix_url(self, url):
        if not url:
            return ""
        url = url.strip()
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return self.host.rstrip("/") + url
        return url

    def _text(self, s):
        s = re.sub(r"<[^>]+>", " ", s or "")
        s = s.replace("&nbsp;", " ").replace("&amp;", "&")
        s = s.replace(chr(38) + "quot;", '"').replace("&#39;", "'")
        s = s.replace("&lt;", "<").replace("&gt;", ">")
        return re.sub(r"\s+", " ", s).strip()

    def _cards(self, items):
        out = []
        seen = set()
        for it in items:
            vid = str(it.get("video_id") or "")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            remark = str(it.get("score") or "").strip()
            hot = str(it.get("play_times") or "").strip()
            out.append({
                "vod_id": vid,
                "vod_name": self._text(it.get("video_name") or ""),
                "vod_pic": self._fix_url(it.get("cover") or ""),
                "vod_remarks": ("评分%s" % remark) if remark else hot,
                "vod_class": self._text(it.get("category") or ""),
                "vod_year": "",
                "vod_area": "",
                "vod_actor": self._text(it.get("artist") or "").replace("演员:", "").strip()[:120],
                "vod_director": self._text(it.get("director") or "").replace("导演:", "").strip()[:60],
                "vod_content": self._text(it.get("intro") or ""),
            })
        return out

    def _cards_from_html(self, html):
        out = []
        seen = set()
        blocks = html.split('<li class="Movie-list">')[1:]
        if not blocks:
            blocks = re.findall(r'<li[^>]*class="[^"]*Movie-list[^"]*"[\s\S]{0,4000}?</li>', html)
        for blk in blocks:
            vid = re.search(r'video_id=(\d+)', blk)
            if not vid:
                continue
            if vid.group(1) in seen:
                continue
            seen.add(vid.group(1))
            name = re.search(r'Movie-name01[^>]*>\s*([^<]+)', blk)
            pic = re.search(r'(?:originalSrc|data-src|src)="(https?://[^"]+\.(?:jpg|jpeg|png|webp|gif))"', blk)
            score = re.search(r'class="oth-time"[\s\S]{0,160}?>\s*([\d.]+)\s*<', blk)
            cat = re.search(r'<ul[^>]*class="[^"]*Movie-type[^"]*"[\s\S]*?</ul>', blk)
            actor = re.search(r'Movie-star[\s\S]{0,2000}?主演[：:]\s*([\s\S]{0,600}?)</div>', blk)
            intro = re.search(r'Movie-content[\s\S]{0,2400}?简介[：:]\s*([\s\S]{0,900}?)</span>', blk)
            hot = re.search(r'热度[：:]\s*([\d.]+)', blk)
            out.append({
                "vod_id": vid.group(1),
                "vod_name": self._text(name.group(1)) if name else "",
                "vod_pic": self._fix_url(pic.group(1)) if pic else "",
                "vod_remarks": ("评分%s" % score.group(1)) if score else (("热度%s" % hot.group(1)) if hot else ""),
                "vod_class": self._text(cat.group(0)) if cat else "",
                "vod_year": "",
                "vod_area": "",
                "vod_actor": self._text(actor.group(1))[:120] if actor else "",
                "vod_director": "",
                "vod_content": self._text(intro.group(1))[:400] if intro else "",
            })
        return out