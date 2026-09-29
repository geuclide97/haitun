# -*- coding: utf-8 -*-
"""
泥视频 (www.nivod.cc) - TVBox 通用 Python Spider
仿爱奇艺国际版 (Gaze SSR) 前端，非 MacCMS 结构。

旧站 www.nivod.vip (MacCMS) 已挂 (522)，2026-09 逆向并切换到 www.nivod.cc：
  - 分类页   /class.html?channel=movie|tv|show|anime
  - 搜索页   /search.html?keyword=xxx
  - 详情页   /voddetail/{id}
  - 播放页   /vodplay/{id}/v (电影) 或 /vodplay/{id}/epN (电视剧)
  - 播放接口 /xhr_playinfo/{id} (电影) 或 /xhr_playinfo/{id}-{ep} (电视剧)
              返回 JSON: pdatas[].playurl (m3u8)、pdatas[].from、pdatas[].name

依赖：
    requests
    beautifulsoup4
"""

import json
import re
import threading
from urllib.parse import quote, urljoin

try:
    import requests
except Exception:
    requests = None

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

try:
    from base.spider import Spider as BaseSpider
except Exception:
    class BaseSpider(object):
        pass


class Spider(BaseSpider):
    HOST = "https://www.nivod.cc/"

    CLASS_MAP = [
        {"type_id": "movie", "type_name": "电影"},
        {"type_id": "tv", "type_name": "电视剧"},
        {"type_id": "show", "type_name": "综艺"},
        {"type_id": "anime", "type_name": "动漫"},
    ]

    def getName(self):
        return "泥视频"

    def init(self, extend=""):
        self.host = self.HOST.rstrip("/") + "/"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0 Safari/537.36",
            "Referer": self.host,
        }
        self.session = requests.Session() if requests else None
        if self.session:
            self.session.headers.update(self.headers)

    def _ensure_runtime(self):
        if requests is None:
            raise RuntimeError("缺少 requests")
        if BeautifulSoup is None:
            raise RuntimeError("缺少 beautifulsoup4")

    def _get(self, url, referer=None):
        self._ensure_runtime()
        headers = dict(self.headers)
        if referer:
            headers["Referer"] = referer
        response = self.session.get(
            urljoin(self.host, url),
            timeout=15,
            headers=headers,
        )
        response.raise_for_status()
        response.encoding = "utf-8"
        return response.text

    @staticmethod
    def _text(node):
        return node.get_text(" ", strip=True) if node else ""

    def _parse_cards(self, html):
        """解析列表卡片（分类页 / 搜索页通用）。

        分类页卡片：
            <li class="qy-mod-li">
                <a href="/voddetail/{id}" class="qy-mod-link">
                    <img src="/imgs/small/{id}.jpg" alt="{name}">
                    <span class="qy-mod-label">{remarks}</span>
                </a>
                <div class="title-wrap"><a title="{name}">...</a></div>
            </li>
        搜索页卡片封面为 background-image，剧名在 title-wrap 的 a[title]。
        """
        soup = BeautifulSoup(html, "html.parser")
        result = []

        for li in soup.select("li.qy-mod-li"):
            link = li.select_one('a[href*="/voddetail/"]')
            if not link:
                continue
            m = re.search(r"/voddetail/(\d+)", link.get("href", ""))
            if not m:
                continue
            vid = m.group(1)

            # 剧名：优先 title-wrap 里的 a[title]，其次 img alt
            name = ""
            title_a = li.select_one(".title-wrap a[title]")
            if title_a:
                name = (title_a.get("title", "") or "").strip()
            if not name:
                img = li.select_one("img")
                if img:
                    name = (img.get("alt") or img.get("title") or "").strip()
            if not name:
                name = link.get_text(strip=True)

            # 备注
            note = li.select_one(".qy-mod-label")
            remarks = self._text(note) if note else ""

            # 封面（站点固定路径 /imgs/small/{id}.jpg）
            pic = urljoin(self.host, "/imgs/small/%s.jpg" % vid)

            result.append({
                "vod_id": vid,
                "vod_name": name,
                "vod_pic": pic,
                "vod_remarks": remarks,
            })

        return result

    def homeContent(self, filter):
        return {"class": self.CLASS_MAP}

    def homeVideoContent(self):
        # 首页无独立推荐数据，直接复用电影分类
        try:
            return self.categoryContent("movie", 1, False, {})
        except Exception as exc:
            return {"list": [], "error": str(exc)}

    def categoryContent(self, tid, pg, filter, extend):
        page = max(int(pg or 1), 1)
        channel = str(tid or "movie").strip()
        try:
            html = self._get("/class.html?channel=%s" % channel)
            videos = self._parse_cards(html)
            return {
                "page": page,
                "pagecount": 1 if videos else page,  # 站点分类不分页
                "limit": len(videos),
                "total": len(videos),
                "list": videos,
            }
        except Exception as exc:
            return {
                "page": page,
                "pagecount": page,
                "limit": 0,
                "total": 0,
                "list": [],
                "error": str(exc),
            }

    def searchContent(self, key, quick, pg="1"):
        try:
            html = self._get("/search.html?keyword=%s" % quote(str(key)))
            videos = self._parse_cards(html)
            return {
                "page": 1,
                "pagecount": 1,
                "limit": len(videos),
                "total": len(videos),
                "list": videos,
            }
        except Exception as exc:
            return {"list": [], "error": str(exc)}

    def detailContent(self, ids):
        try:
            vid = ids[0] if isinstance(ids, list) else ids
            vid = re.sub(r"\D", "", str(vid))
            html = self._get("/voddetail/%s" % vid)
            soup = BeautifulSoup(html, "html.parser")

            # 剧名：<title>{name}_在线观看</title>
            name = ""
            m = re.search(r"<title>([^<]+)</title>", html)
            if m:
                name = m.group(1).split("_")[0].strip()
            if not name:
                name = vid

            def id_val(id_):
                node = soup.select_one("#" + id_)
                return self._text(node) if node else ""

            # 剧集链接（电视剧 epN / 综艺日期，统一处理）
            episodes = []
            seen = set()
            for a in soup.select('a[href*="/vodplay/"]'):
                href = a.get("href", "")
                if "/vodplay/" not in href:
                    continue
                label = self._text(a).strip()
                if not label:
                    continue
                # 跳过“立即播放”按钮（文本恰好是“播放”等）
                if label in ("播放", "立即播放", "开始播放", "立即观看"):
                    continue
                clean = href.split("#")[0]
                # 跳过电影线路（/vodplay/{id}/v）
                if re.search(r"/v$", clean):
                    continue
                if clean in seen:
                    continue
                seen.add(clean)
                episodes.append((clean, label))

            # 若有日期格式剧集（综艺），按最新在前排；否则（电视剧）按集数升序
            has_date = any(
                re.search(r"/\d{8}$", h) for h, _ in episodes
            )

            def ep_sort(item):
                mm = re.search(r"/ep(\d+)$", item[0])
                dd = re.search(r"/(\d{8})$", item[0])
                if has_date:
                    # 综艺：日期/集数统一降序（最新一期在前）
                    if dd:
                        return -int(dd.group(1))
                    if mm:
                        return -int(mm.group(1))
                    return 0
                # 电视剧：按集数升序（第01集在前）
                if mm:
                    return int(mm.group(1))
                return 0

            # HTML 中剧集倒序，重排：电视剧正序、综艺最新在前
            episodes.sort(key=ep_sort)

            if episodes:
                play_url = "#".join(
                    "%s$%s" % (label, urljoin(self.host, href))
                    for href, label in episodes
                )
                play_from = "泥视频"
            else:
                # 电影：单集，线路由 hash 区分，播放接口按 {id} 取全部线路
                play_url = "正片$%s" % urljoin(self.host, "/vodplay/%s/v" % vid)
                play_from = "泥视频"

            vod = {
                "vod_id": vid,
                "vod_name": name,
                "type_name": id_val("types-label"),
                "vod_pic": urljoin(self.host, "/imgs/%s.jpg" % vid),
                "vod_remarks": id_val("reso"),
                "vod_area": id_val("region"),
                "vod_director": id_val("director"),
                "vod_actor": id_val("actors"),
                "vod_content": id_val("show-desc"),
                "vod_play_from": play_from,
                "vod_play_url": play_url,
            }

            return {"list": [vod]}
        except Exception as exc:
            return {"list": [], "error": str(exc)}

    def playerContent(self, flag, id, vipFlags):
        try:
            page_url = str(id or "")
            m = re.search(r"/vodplay/(\d+)(?:/([\w-]+))?", page_url)
            if not m:
                return {"parse": 1, "playUrl": "", "url": page_url}

            vid = m.group(1)
            ep = (m.group(2) or "").strip()

            if ep and ep not in ("v",):
                api = "/xhr_playinfo/%s-%s" % (vid, ep)
            else:
                api = "/xhr_playinfo/%s" % vid

            obj = json.loads(self._get(api, referer=page_url))

            urls = []
            for p in obj.get("pdatas", []) if isinstance(obj, dict) else []:
                u = str(p.get("playurl", "") or "").strip()
                if u:
                    urls.append(u)

            if not urls:
                return {"parse": 1, "playUrl": "", "url": page_url}

            best = self._pick_playable(urls)
            return {
                "parse": 0,
                "playUrl": "",
                "url": best,
                "header": {"User-Agent": self.headers["User-Agent"]},
            }
        except Exception as exc:
            return {
                "parse": 1,
                "playUrl": "",
                "url": str(id or ""),
                "error": str(exc),
            }

    def _pick_playable(self, urls):
        """并发探测所有线路，优先返回可用的多码率 master playlist，
        跳过 403/超时/非标准端口的坏线路。
        """
        if not urls:
            return ""
        if len(urls) == 1:
            return urls[0]

        results = {}
        lock = threading.Lock()
        master_done = threading.Event()

        def probe(u):
            kind = self._m3u8_kind(u)
            if kind:
                with lock:
                    results.setdefault(kind, []).append(u)
            if kind == "master":
                master_done.set()

        threads = [threading.Thread(target=probe, args=(u,)) for u in urls]
        for t in threads:
            t.daemon = True
            t.start()

        # 优先等 master（最多 4s），有就尽快返回
        master_done.wait(4)
        if results.get("master"):
            return results["master"][0]
        # 无 master：等 ts 线程收尾，再验证首分片
        for t in threads:
            t.join(2)

        for u in results.get("ts", []):
            if self._ts_playable(u):
                return u
        if results.get("ts"):
            return results["ts"][0]
        return urls[0]

    def _m3u8_kind(self, url):
        """判断 m3u8 类型：master（多码率）/ ts（直接分片）/ None（无效）。"""
        try:
            r = self.session.get(url, timeout=5, headers=self.headers)
            if r.status_code != 200:
                return None
            text = r.text
            if "#EXTM3U" not in text:
                return None
            if "#EXT-X-STREAM-INF" in text:
                return "master"
            return "ts"
        except Exception:
            return None

    def _ts_playable(self, url):
        """验证直接 ts 列表的首分片确实能下到数据（过滤非标准端口/403）。"""
        try:
            r = self.session.get(url, timeout=5, headers=self.headers)
            text = r.text
            if "#EXTM3U" not in text:
                return False
            seg = re.search(r"\n(https?://[^\s]+)", text)
            if not seg:
                return True
            seg_url = seg.group(1).strip()
            r2 = self.session.get(
                seg_url, timeout=4, stream=True, headers=self.headers)
            got = 0
            for chunk in r2.iter_content(16384):
                got += len(chunk)
                if got >= 65536:
                    break
            r2.close()
            return got >= 8192
        except Exception:
            return False

    def isVideoFormat(self, url):
        return bool(re.search(
            r"(?i)\.(m3u8|mp4|flv|avi|mkv|wmv|mpg|mpeg|mov|ts|3gp|rmvb?)",
            str(url or ""),
        ))

    def manualVideoCheck(self):
        return False

    def localProxy(self, param):
        return [404, "text/plain", "Not Found"]
