#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# TVBox Python 爬虫 · 半日九九 (csp_App99 同族, host=103.217.190.91:19987)
# 由 jar 1.jar 里 com.github.catvod.spider.App99 逆向重写
#
# 协议要点(实测验证):
#   1. AES-256-CBC, 密钥 = 请求头 uuid 去掉横杠后的 32 字节 (uuid 是客户端自定的会话密钥)
#   2. 加密: 随机16字节IV + PKCS7, IV 拼在密文最前面, 整体 Base64
#   3. 响应解密后先尝试 zlib inflate(服务器对大响应做 deflate), 失败则当明文
#   4. sign = SHA256("nonce:timestamp:路径:uuid"), 服务端目前未强校验, 仍按原协议发送
#   5. 接口: /app/systemInit /vod/search /vod/detail /app/vodParser
#   6. co 专线(及 CO4K/Ace/PAN1/NSYS/hgdj 等付费线)返回 co_xxx 令牌,
#      需 POST /app/vodParser {id:解析器号, url:令牌} 换真实 m3u8
#
# config.json 挂载示例(与 jar 版等效):
#   { "key": "半日九九py", "name": "半日九九｜PY", "type": 3,
#     "api": "https://你的仓库/半日九九.py",      ← 改成你的 raw 链接
#     "searchable": 1, "quickSearch": 1, "filterable": 1,
#     "ext": "http://103.217.190.91:19987/app/bn" }
#   ext 可省略(用内置 host), 也可传 {"host": "..."} JSON
# ============================================================
import sys
import json
import os
import re
import time
import zlib
import base64
import hashlib
import urllib.request
import urllib.error

sys.path.append('..')
try:
    from base.spider import Spider as _BaseSpider
except Exception:
    _BaseSpider = object

try:
    from Crypto.Cipher import AES as _AES
    _BACKEND = "pycryptodome"
except Exception:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher as _Cp
        from cryptography.hazmat.primitives.ciphers import algorithms as _al
        from cryptography.hazmat.primitives.ciphers import modes as _md
        from cryptography.hazmat.backends import default_backend as _bk
        _BACKEND = "cryptography"
    except Exception:
        _BACKEND = "pure"

# ---------- 纯 Python AES-256-CBC 兜底(无第三方库时) ----------
if _BACKEND == "pure":
    _SBOX = None

    def _init_sbox():
        global _SBOX, _INV_SBOX
        if _SBOX is not None:
            return
        p = q = 1
        sb = [0] * 256
        while True:
            p = (p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0))
            q ^= q << 1
            q ^= q << 2
            q ^= q << 4
            q &= 0xFF
            if q & 0x80:
                q ^= 0x09
            x = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) ^ \
                ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
            sb[p] = x & 0xFF ^ 0x63
            if p == 1:
                break
        sb[0] = 0x63
        _SBOX = sb
        _INV_SBOX = [0] * 256
        for i in range(256):
            _INV_SBOX[sb[i]] = i

    def _xtime(a):
        a <<= 1
        return (a ^ 0x1B) & 0xFF if a & 0x100 else a

    def _key_expand(key):
        _init_sbox()
        nk = len(key) // 4
        rounds = nk + 6
        w = [list(key[i * 4:i * 4 + 4]) for i in range(nk)]
        rcon = 1
        for i in range(nk, 4 * (rounds + 1)):
            t = list(w[i - 1])
            if i % nk == 0:
                t = t[1:] + t[:1]
                t = [_SBOX[b] for b in t]
                t[0] ^= rcon
                rcon = _xtime(rcon)
            elif nk > 6 and i % nk == 4:
                t = [_SBOX[b] for b in t]
            w.append([w[i - nk][j] ^ t[j] for j in range(4)])
        return w, rounds

    def _enc_block(w, rounds, block):
        _init_sbox()

        def add_rk(r):
            for j in range(16):
                block[j] ^= w[r * 4 + j // 4][j % 4]
        add_rk(0)
        for rnd in range(1, rounds + 1):
            # SubBytes
            for j in range(16):
                block[j] = _SBOX[block[j]]
            # ShiftRows (state column-major: idx = c*4 + r)
            for r in range(1, 4):
                row = [block[c * 4 + r] for c in range(4)]
                row = row[r:] + row[:r]
                for c in range(4):
                    block[c * 4 + r] = row[c]
            # MixColumns (最后一轮跳过)
            if rnd != rounds:
                for c in range(4):
                    a0, a1, a2, a3 = (block[c*4], block[c*4+1], block[c*4+2], block[c*4+3])
                    block[c*4] = _xtime(a0) ^ _xtime(a1) ^ a1 ^ a2 ^ a3
                    block[c*4+1] = a0 ^ _xtime(a1) ^ _xtime(a2) ^ a2 ^ a3
                    block[c*4+2] = a0 ^ a1 ^ _xtime(a2) ^ _xtime(a3) ^ a3
                    block[c*4+3] = _xtime(a0) ^ a0 ^ a1 ^ a2 ^ _xtime(a3)
            add_rk(rnd)
        return bytes(block)

    def _cbc_encrypt(key, iv, data):
        w, rounds = _key_expand(key)
        prev = iv
        out = bytearray()
        for i in range(0, len(data), 16):
            blk = bytearray(16)
            for j in range(16):
                blk[j] = data[i + j] ^ prev[j]
            ct = _enc_block(w, rounds, blk)
            out += ct
            prev = ct
        return bytes(out)

    def _dec_block(w, rounds, block):
        _init_sbox()

        def _mul9(a):
            return (_xtime(_xtime(_xtime(a))) ^ a) & 0xFF

        def _mul11(a):
            return (_xtime(_xtime(_xtime(a))) ^ _xtime(a) ^ a) & 0xFF

        def _mul13(a):
            return (_xtime(_xtime(_xtime(a))) ^ _xtime(_xtime(a)) ^ a) & 0xFF

        def _mul14(a):
            return (_xtime(_xtime(_xtime(a))) ^ _xtime(_xtime(a)) ^ _xtime(a)) & 0xFF

        def add_rk(r):
            for j in range(16):
                block[j] ^= w[r * 4 + j // 4][j % 4]

        add_rk(rounds)
        for rnd in range(rounds - 1, -1, -1):
            # 逆行移位 (inv shift rows, state column-major)
            for r in range(1, 4):
                row = [block[c * 4 + r] for c in range(4)]
                row = row[-r:] + row[:-r]
                for c in range(4):
                    block[c * 4 + r] = row[c]
            for j in range(16):
                block[j] = _INV_SBOX[block[j]]
            add_rk(rnd)
            if rnd != 0:
                for c in range(4):
                    a0, a1, a2, a3 = block[c*4], block[c*4+1], block[c*4+2], block[c*4+3]
                    block[c*4] = _mul14(a0) ^ _mul11(a1) ^ _mul13(a2) ^ _mul9(a3)
                    block[c*4+1] = _mul9(a0) ^ _mul14(a1) ^ _mul11(a2) ^ _mul13(a3)
                    block[c*4+2] = _mul13(a0) ^ _mul9(a1) ^ _mul14(a2) ^ _mul11(a3)
                    block[c*4+3] = _mul11(a0) ^ _mul13(a1) ^ _mul9(a2) ^ _mul14(a3)
        return bytes(block)

    def _cbc_decrypt(key, iv, data):
        w, rounds = _key_expand(key)
        out = bytearray()
        prev = iv
        for i in range(0, len(data), 16):
            blk = bytearray(data[i:i + 16])
            pt = _dec_block(w, rounds, blk)
            out += bytes(pt[j] ^ prev[j] for j in range(16))
            prev = data[i:i + 16]
        return bytes(out)


def _aes_cbc_encrypt(key, iv, data):
    if _BACKEND == "pycryptodome":
        return _AES.new(key, _AES.MODE_CBC, iv).encrypt(data)
    if _BACKEND == "cryptography":
        c = _Cp(_al.AES(key), _md.CBC(iv), backend=_bk())
        e = c.encryptor()
        return e.update(data) + e.finalize()
    return _cbc_encrypt(key, iv, data)


def _aes_cbc_decrypt(key, iv, data):
    if _BACKEND == "pycryptodome":
        return _AES.new(key, _AES.MODE_CBC, iv).decrypt(data)
    if _BACKEND == "cryptography":
        c = _Cp(_al.AES(key), _md.CBC(iv), backend=_bk())
        d = c.decryptor()
        return d.update(data) + d.finalize()
    return _cbc_decrypt(key, iv, data)


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
DEFAULT_HOST = "http://103.217.190.91:19987/app/bn"
VERSION = "1.2.0"
TIMEOUT = 20

# 解析器 id -> 名称 (systemInit.parser_api)
PARSER_NAMES = {1: "蚂蚁", 2: "咖啡", 3: "ACE", 4: "YYNB", 5: "NSYS",
                6: "直播", 7: "去插播", 8: "旺旺短剧", 9: "4K"}

# 请求体公共字段
APP_META = {"v": "2.0.0", "pl": 1}


class Spider(_BaseSpider):
    host = DEFAULT_HOST
    uuid = None          # 会话密钥来源: uuid 去横杠
    token = ""
    cats = []            # [{id,name,type_extend}]
    _init_ok = False

    # ================= 基础 =================
    def getName(self):
        return "半日九九"

    def init(self, extend=""):
        try:
            self._setup(extend)
        except Exception:
            self._init_ok = False

    def _setup(self, extend=""):
        host = DEFAULT_HOST
        ext = ""
        if extend:
            ext = str(extend).strip()
            if ext.startswith("{"):
                try:
                    j = json.loads(ext)
                    host = (j.get("host") or DEFAULT_HOST).strip()
                except Exception:
                    pass
            elif ext and not ext.startswith("http"):
                try:
                    j = json.loads(ext)
                    host = (j.get("host") or DEFAULT_HOST).strip()
                except Exception:
                    pass
            elif ext:
                host = ext
        self.host = host.rstrip("/")
        if not self.uuid:
            self.uuid = "%08x%04x%04x%04x%012x" % (
                int.from_bytes(os.urandom(4), 'big'),
                int.from_bytes(os.urandom(2), 'big'),
                int.from_bytes(os.urandom(2), 'big'),
                int.from_bytes(os.urandom(2), 'big'),
                int.from_bytes(os.urandom(6), 'big'))
        self._init_ok = True
        # 分类 + 播放器表
        try:
            d = self._api("/app/systemInit", {
                "v": VERSION, "n": "半日九九", "s": "", "pl": 1,
                "apiVersion": "v2", "token": ""})
            cats = (d or {}).get("categorys") or {}
            data = cats.get("data") or []
            self.cats = []
            for c in data:
                if not isinstance(c, dict):
                    continue
                try:
                    self.cats.append({
                        "id": str(c.get("id")),
                        "name": str(c.get("name") or ""),
                        "extend": c.get("type_extend") or {},
                    })
                except Exception:
                    pass
        except Exception:
            self.cats = []

    def isVideoFormat(self, url):
        return False

    def manualVideoCheck(self):
        return False

    # ================= 加解密 =================
    def _key(self):
        return self.uuid.replace("-", "").encode("utf-8")

    def _encrypt(self, obj):
        iv = os.urandom(16)
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        pad = 16 - (len(raw) % 16)
        raw += bytes([pad]) * pad
        return base64.b64encode(iv + _aes_cbc_encrypt(self._key(), iv, raw)).decode()

    def _decrypt(self, b64):
        data = base64.b64decode(b64.strip())
        if len(data) < 32:
            return ""
        pt = _aes_cbc_decrypt(self._key(), data[:16], data[16:])
        pad = pt[-1]
        if 1 <= pad <= 16 and pt[-pad:] == bytes([pad]) * pad:
            pt = pt[:-pad]
        try:
            pt = zlib.decompress(pt)
        except Exception:
            pass
        return pt.decode("utf-8", "replace")

    def _api(self, path, payload):
        ts = str(int(time.time() * 1000))
        nonce = base64.b64encode(os.urandom(16)).decode()
        sign = hashlib.sha256(
            ("%s:%s:%s:%s" % (nonce, ts, path, self.uuid)).encode("utf-8")
        ).hexdigest()
        payload = dict(payload)
        payload.setdefault("timestamp", ts)
        payload.setdefault("nonce", nonce)
        headers = {
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "client_type": "android",
            "uuid": self.uuid,
            "timestamp": ts,
            "sign": sign,
            "nonce": nonce,
            "version": VERSION,
            "api_version": "v1",
        }
        req = urllib.request.Request(self.host + path,
                                     data=self._encrypt(payload).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:
                return {}
        except Exception:
            return {}
        if not raw.strip():
            return {}
        try:
            plain = self._decrypt(raw)
        except Exception:
            return {}
        if not plain:
            return {}
        try:
            return json.loads(plain)
        except Exception:
            return {}

    # ================= 输出转换 =================
    def _vod_item(self, d):
        if not isinstance(d, dict):
            return {}
        return {
            "vod_id": str(d.get("id", "")),
            "vod_name": str(d.get("name", "")),
            "vod_pic": str(d.get("pic", "")),
            "vod_remarks": str(d.get("remarks", "")),
            "vod_year": str(d.get("year", "")),
            "vod_content": str(d.get("blurb", "")),
            "type_name": str(d.get("class", "")),
            "vod_area": str(d.get("area", "")),
            "vod_actor": str(d.get("actor", "")),
            "vod_director": str(d.get("director", "")),
        }

    @staticmethod
    def _split(s, sep):
        parts = re.split(sep, s or "")
        while parts and not parts[-1]:
            parts.pop()
        return parts

    def _filters_for(self, cat):
        """把 type_extend 转成 TVBox 筛选器"""
        te = cat.get("extend") or {}
        if isinstance(te, str):
            try:
                te = json.loads(te)
            except Exception:
                te = {}
        if not te:
            return []
        fl = []

        def arr(v):
            if isinstance(v, list):
                return [str(x) for x in v if str(x).strip()]
            return [x.strip() for x in str(v or "").split(",") if x.strip()]

        if te.get("class"):
            fl.append({"key": "class", "name": "类型",
                       "value": [{"n": "全部", "v": ""}] +
                                [{"n": x, "v": x} for x in arr(te["class"])]})
        if te.get("area"):
            fl.append({"key": "area", "name": "地区",
                       "value": [{"n": "全部", "v": ""}] +
                                [{"n": x, "v": x} for x in arr(te["area"])]})
        if te.get("lang"):
            fl.append({"key": "lang", "name": "语言",
                       "value": [{"n": "全部", "v": ""}] +
                                [{"n": x, "v": x} for x in arr(te["lang"])]})
        if te.get("year"):
            fl.append({"key": "year", "name": "年份",
                       "value": [{"n": "全部", "v": ""}] +
                                [{"n": x, "v": x} for x in arr(te["year"])]})
        return fl

    # ================= TVBox 接口 =================
    def homeContent(self, filter):
        if not self._init_ok:
            self._setup()
        classes = []
        filters = {}
        for c in self.cats:
            classes.append({"type_id": c["id"], "type_name": c["name"]})
            fl = self._filters_for(c)
            if fl:
                filters[c["id"]] = fl
        result = {"class": classes, "filters": filters, "list": []}
        # 首页推荐: 用电影分类第一页
        try:
            r = self._api("/vod/search", {
                "kw": "", "page": 1, "limit": 21, "pid": "1",
                "orderBy": "time", "isCategory": 1, "token": ""})
            items = r.get("data") or []
            result["list"] = [self._vod_item(x) for x in items if isinstance(x, dict)]
        except Exception:
            pass
        return result

    def homeVideoContent(self):
        try:
            r = self._api("/vod/search", {
                "kw": "", "page": 1, "limit": 21, "pid": "1",
                "orderBy": "time", "isCategory": 1, "token": ""})
            items = r.get("data") or []
            return {"list": [self._vod_item(x) for x in items if isinstance(x, dict)]}
        except Exception:
            return {"list": []}

    def categoryContent(self, tid, pg, filter, extend):
        if not self._init_ok:
            self._setup()
        try:
            page = max(1, int(pg))
        except Exception:
            page = 1
        payload = {
            "kw": "", "page": page, "limit": 21, "pid": str(tid),
            "orderBy": "time", "isCategory": 1, "token": self.token,
        }
        # 筛选参数(实测服务端对 class/area/lang/year 均生效)
        ext = {}
        if isinstance(extend, str) and extend:
            try:
                ext = json.loads(extend)
            except Exception:
                ext = {}
        elif isinstance(extend, dict):
            ext = extend
        for k in ("class", "area", "lang", "year"):
            v = str(ext.get(k, "") or "").strip()
            if v:
                payload[k] = v
        r = self._api("/vod/search", payload)
        items = r.get("data") or []
        lst = [self._vod_item(x) for x in items if isinstance(x, dict)]
        try:
            page_count = int(r.get("page_count") or page)
        except Exception:
            page_count = page
        if page_count < page:
            page_count = page
        return {"list": lst, "page": page, "pagecount": page_count,
                "limit": 21, "total": page_count * 21}

    def detailContent(self, ids):
        if not self._init_ok:
            self._setup()
        vid = str(ids[0])
        r = self._api("/vod/detail", dict(APP_META, id=vid, eps="1", token=self.token))
        d = r.get("data") or {}
        if not isinstance(d, dict):
            return {"list": []}
        vod = self._vod_item(d)
        vod["vod_content"] = str(d.get("content", "")) or vod["vod_content"]
        # 专线显示名映射(code -> 名称), 静态近似(systemInit.player 动态映射)
        LINE_NAMES = {
            "co": "专线", "rose": "自营", "NBY": "VIP", "CO4K": "4K",
            "Ace": "蓝光①", "PAN1": "蓝光②", "NSYS": "蓝光③",
            "hgdj": "短剧", "zydj": "红果短剧", "co_egg": "花絮",
            "ffm3u8": "非凡", "lzm3u8": "量子", "CCTV": "央视直播",
            "difangtai": "地方台",
        }
        # vodParser 需要的解析器号(code -> parser id)
        LINE_PARSER = {"co": 2, "rose": 2, "CO4K": 9, "Ace": 3,
                       "PAN1": 5, "NSYS": 5, "hgdj": 4, "zydj": 8}
        play_from = d.get("play_from") or ""
        play_url = d.get("play_url") or ""
        codes = self._split(play_from, r"\$\$\$")
        groups = self._split(play_url, r"\$\$\$")
        # 线路重排: 专线优先, m3u8 备用殿后(与 jar processDetailResult 一致)
        order = {"CO4K": 0, "co": 1, "Ace": 2, "PAN1": 3, "NSYS": 4, "rose": 5, "hgdj": 6}
        pairs = list(zip(codes, groups))
        pairs.sort(key=lambda x: (order.get(x[0], 99), x[0]))
        flags, urls = [], []
        for code, group in pairs:
            if not group:
                continue
            name = LINE_NAMES.get(code, code)
            eps = []
            for ep in group.split("#"):
                seg = ep.split("$")
                if len(seg) >= 2:
                    raw_url, ep_name = seg[1], seg[0]
                    # 专线令牌(co_xxx 等)不带 http, 播放时走 vodParser
                    if raw_url.startswith("http"):
                        play_id = raw_url
                    else:
                        play_id = "%s@%s" % (raw_url, code)
                    eps.append("%s$%s" % (ep_name, play_id))
            if eps:
                flags.append(name)
                urls.append("#".join(eps))
        if flags:
            vod["vod_play_from"] = "$$$".join(flags)
            vod["vod_play_url"] = "$$$".join(urls)
        vod["vod_id"] = vid
        return {"list": [vod]}

    def searchContent(self, key, quick, pg="1"):
        return self.searchContentPage(key, quick, pg)

    def searchContentPage(self, key, quick, pg="1"):
        if not self._init_ok:
            self._setup()
        try:
            page = max(1, int(pg))
        except Exception:
            page = 1
        key = str(key or "").strip()
        if not key:
            return {"list": [], "page": page, "pagecount": page, "limit": 0, "total": 0}
        r = self._api("/vod/search", {
            "kw": key, "page": page, "limit": 21,
            "orderBy": "vod_hits_month", "sort": "desc", "token": self.token})
        items = r.get("data") or []
        lst = [self._vod_item(x) for x in items if isinstance(x, dict)]
        pagecount = page if len(lst) < 21 else page + 1
        return {"list": lst, "page": page, "pagecount": pagecount,
                "limit": 21, "total": pagecount * 21}

    def playerContent(self, flag, id, vipFlags=None):
        try:
            if not id:
                raise ValueError("empty id")
            # 详情里存的是 "url@code"; 纯 http 老格式兼容
            if "@" in id:
                raw_url, code = id.rsplit("@", 1)
            else:
                raw_url, code = id, ""
            url = raw_url
            if not raw_url.startswith("http") and code:
                # 专线令牌 -> vodParser 换真实直链
                parser_id = {"co": 2, "rose": 2, "CO4K": 9, "Ace": 3,
                             "PAN1": 5, "NSYS": 5, "hgdj": 4, "zydj": 8}.get(code, 2)
                r = self._api("/app/vodParser",
                              {"id": parser_id, "url": raw_url, "token": self.token})
                data = r.get("data")
                if data and str(data).startswith("http"):
                    url = str(data)
                else:
                    return {"parse": 0, "url": "",
                            "msg": "专线解析失败, 请切换其他线路播放",
                            "header": {"User-Agent": UA}}
            direct = re.search(r"\.(m3u8|mp4|mkv|flv|ts)", url or "", re.I)
            return {"parse": 0, "url": url,
                    "header": {"User-Agent": UA},
                    "danmaku": ""}
        except Exception:
            return {"parse": 0, "url": "", "msg": "播放失败, 请切换线路",
                    "header": {"User-Agent": UA}}

    def localProxy(self, param):
        return {"list": [], "parse": 0, "url": ""}


# ---------------- 本机自测(直接运行本文件) ----------------
if __name__ == "__main__":
    def _j(x):
        return x if isinstance(x, dict) else json.loads(x)

    s = Spider()
    s.init("")
    home = _j(s.homeContent(True))
    print("== 首页分类 ==")
    for c in home.get("class", []):
        print("  ", c.get("type_id"), c.get("type_name"))
    print("== 分类(电视剧第1页, 前3条) ==")
    cat = _j(s.categoryContent("2", "1", True, {}))
    for v in (cat.get("list") or [])[:3]:
        print("  ", v.get("vod_id"), v.get("vod_name"), v.get("vod_remarks"))
    if cat.get("list"):
        det = _j(s.detailContent([cat["list"][0]["vod_id"]]))
        v = det["list"][0]
        print("== 详情 ==", v.get("vod_name"))
        print("   线路:", v.get("vod_play_from"))
        first = (v.get("vod_play_url") or "").split("$$$")[0].split("#")[0]
        print("   首集:", first[:120])
        flag = (v.get("vod_play_from") or "").split("$$$")[0]
        eid = first.split("$", 1)[1] if "$" in first else first
        play = _j(s.playerContent(flag, eid))
        print("== 播放 ==", "parse=", play.get("parse"), "url=", str(play.get("url"))[:120])
    print("== 搜索'庆余年' ==")
    se = _j(s.searchContent("庆余年", False, "1"))
    for v in (se.get("list") or [])[:3]:
        print("  ", v.get("vod_id"), v.get("vod_name"))
    print("DONE")
