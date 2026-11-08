# -*- coding: utf-8 -*-
"""
牛牛视频 (com.qingbian.jz 1.6.3) — TVBox / FongMi py 源
纯标准库：不依赖 requests / Crypto / cryptography

接口层（反编译所得）
  base   : https://nn.123xiangshang.com:35620/
  分类   : GET /types
  首页   : GET /main
  列表   : GET /list?class=&area=&year=&state=&order=&type_id=&wd=&page=
  详情   : GET /detail?vod_id=
  搜索   : GET /search/suggest?wd=   (建议)  /list?wd= (结果)

响应加密
  全部为 AES/ECB/PKCS7 + Base64；密钥 = 请求「路径?参数」前 16 位，不足补 '0'
  例: /types                    -> key="/types0000000000"
      /list?type_id=1&page=1   -> key="/list?type_id=1&"

修复记录（TVBox 无法加载）
  1. 本文件曾被从仓库删除，配置 api 仍指向它 -> raw 404，TVBox 直接加载失败
  2. 补齐 FongMi/TVBox py 源标准接口（getName/isVideoFormat/manualVideoCheck/destroy），
     并优先复用 base.spider.Spider 的 fetch（走 Java OkHttp），规避壳内 urllib 证书/代理失败
  3. /types 不可达或返回空时回退静态分类，homeContent 不再返回空 class（TVBox 空白页）
  4. 请求加总时间预算，避免多域名重试把壳内加载拖到超时
  5. AES 逆列混淆改查表，降低手机端解密耗时
"""
import base64
import json
import re
import sys
import time
import ssl
from urllib import request as _rq
from urllib.parse import quote, unquote

# ---------------- 内联 AES（纯标准库，壳里不需要装依赖） ----------------
_SBOX = [
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
]

_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11b
    return a & 0xff


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r & 0xff


# 预计算 GF(2^8) 乘法表：逆列混淆只用到 9/11/13/14 四个常数
_MUL9 = [_mul(x, 9) for x in range(256)]
_MUL11 = [_mul(x, 11) for x in range(256)]
_MUL13 = [_mul(x, 13) for x in range(256)]
_MUL14 = [_mul(x, 14) for x in range(256)]


def _expand_key(key):
    nk = len(key) // 4
    if nk not in (4, 6, 8):
        raise ValueError('bad key length')
    nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return w, nr


def _decrypt_block(block, w, nr):
    s = [list(block[i::4]) for i in range(4)]  # 列主序 state
    # AddRoundKey (last)
    for c in range(4):
        for r in range(4):
            s[r][c] ^= w[(nr * 4) + c][r]
    for rnd in range(nr - 1, -1, -1):
        # InvShiftRows
        for r in range(1, 4):
            s[r] = s[r][-r:] + s[r][:-r]
        # InvSubBytes
        for r in range(4):
            for c in range(4):
                s[r][c] = _INV_SBOX[s[r][c]]
        # AddRoundKey
        for c in range(4):
            for r in range(4):
                s[r][c] ^= w[rnd * 4 + c][r]
        if rnd:
            # InvMixColumns
            for c in range(4):
                a0, a1, a2, a3 = s[0][c], s[1][c], s[2][c], s[3][c]
                s[0][c] = _MUL14[a0] ^ _MUL11[a1] ^ _MUL13[a2] ^ _MUL9[a3]
                s[1][c] = _MUL9[a0] ^ _MUL14[a1] ^ _MUL11[a2] ^ _MUL13[a3]
                s[2][c] = _MUL13[a0] ^ _MUL9[a1] ^ _MUL14[a2] ^ _MUL11[a3]
                s[3][c] = _MUL11[a0] ^ _MUL13[a1] ^ _MUL9[a2] ^ _MUL14[a3]
    out = bytearray(16)
    for c in range(4):
        for r in range(4):
            out[4 * c + r] = s[r][c]
    return bytes(out)


def decrypt_ecb(data, key):
    """AES-ECB 解密并去 PKCS7 填充，返回 bytes"""
    w, nr = _expand_key(key)
    if len(data) % 16:
        data = data[:len(data) - (len(data) % 16)]
    out = bytearray()
    for i in range(0, len(data), 16):
        out += _decrypt_block(data[i:i + 16], w, nr)
    if out:
        pad = out[-1]
        if 0 < pad <= 16 and out[-pad:] == bytes([pad]) * pad:
            out = out[:-pad]
    return bytes(out)


BASE = "https://nn.123xiangshang.com:35620"
UA = "okhttp/4.9.0"
TIMEOUT = 8
# 单次 api 调用总时间预算（秒）：壳内加载首页有超时上限，不能让多域名重试拖死
API_BUDGET = 12.0
# 备用域名池（App 内置配置源 nn.php 解出来的，主域挂了自动切）
BASES = [
    BASE,
    "https://ccc.chaojichaojichanga.com:35620",
    "https://kkksss.yuechangyuehao.com:35820",
    "https://ccc.zhegeyoudianchang.com:35620",
]
# 配置源（AES/ECB 密钥固定），用于拉最新域名池
CFG_URLS = [
    "https://nnal.oss-cn-beijing.aliyuncs.com/nn.php",
    "https://nn-1352193558.cos.ap-guangzhou.myqcloud.com/nn.php",
]
CFG_KEY = b"@@bull!!!video$$"
# Android Uri.encode 保留集（unreserved + 自定义 allow）
_SAFE = "_-!.~'()*-![.:/,%?&="

# ---------------- 内容过滤：屏蔽成人板块 ----------------
# 成人板块 type_id：6 福利 / 7 传媒 / 8 吃瓜 / 9 午夜 / 10 热舞 / 11 直播 / 13 滴滴 / 14 嗒嗒
BLOCK_TIDS = ['6', '7', '8', '9', '10', '11', '13', '14']
_BLOCK_CLASSES = ['麻豆', '果冻', '蜜桃', '精东', '糖心', '天美', '星空', '玩偶',
                  '探花', '无码', 'AV', '网黄', '三级', '偷拍', '人妻', '变态',
                  '吃瓜', '福利', '午夜', '嗒嗒', '滴滴', '自拍', '主播', '网红',
                  '素人', '辣妹', '热舞', '岛国', '传媒']


def _is_blocked(cls):
    """按成人专属关键词判断分类字符串是否应屏蔽（保守语法，兼容精简解释器）"""
    text = str(cls or "")
    for k in _BLOCK_CLASSES:
        if k in text:
            return True
    return False


# 三步解析接口表（反编译 /config 所得，player_id -> 解析模板）
PARSERS = {
    "djzy": ("http://82.156.24.206:12345/jx/dj.php?url=%s", ""),
    "qyzy": ("http://91.110.167.179:6100/jx/qy.php?url=%s", ""),
    "bddj": ("http://82.156.24.206:12345/jx/bddj.php?id=%s", ""),
    "qmdj": ("http://82.156.24.206:12345/jx/qmdj.php?id=%s", ""),
    "hmjc": ("http://154.8.141.13:5560/jx/dj.php?url=%s", ""),
    "jzzy": ("http://154.8.141.13:5560/jx/ht.php?url=%s", ""),
    "pp": ("http://ccs.js.yy.028ncf.cn/jx/ss.php?id=%s", "nnsp"),
    "xj": ("http://82.156.24.206:12345/jx/xj22.php?id=%s", ""),
    "meiju": ("http://82.156.24.206:12345/jx/jxm.php?url=%s", ""),
    "xm": ("http://152.136.196.209:12345/jx/xm.php?url=%s", ""),
    "jm3u8": ("http://82.156.24.206:12345/jx/jx.php?url=%s", "Mozi"),
    "hm3u8": ("http://82.156.24.206:12345/jx/jx.php?url=%s", "Mozi"),
    "zbjx": ("http://192.144.141.22/jx/migu.php?id=%s", "Mozilla/5.0"),
    "91gc": ("http://91.110.167.179:6100/jx/wy.php?id=%s", ""),
    "dle": ("http://91.110.167.179:856/jx/dlejx.php?id=%s", ""),
    "thzy": ("http://91.110.167.179:856/jx/thz.php?id=%s", "Mozilla/5.0"),
    "ffzy": ("http://91.110.167.179:6100/jx/ff.php?url=%s", "Mozilla/5.0"),
    "kkzy": ("http://91.110.167.179:6100/jx/ff.php?url=%s", "Mozilla/5.0"),
    "tkzy": ("http://91.110.167.179:6100/jx/ff.php?url=%s", "Mozilla/5.0"),
    "lzzy": ("http://91.110.167.179:6100/jx/ff.php?url=%s", "Mozilla/5.0"),
    "bfzy": ("http://49.232.251.44:894/jx/bf.php?url=%s", "Mozilla/5.0"),
    "snzy": ("http://49.232.251.44:894/jx/sn.php?url=%s", "Mozilla/5.0"),
    "jszy": ("http://91.110.167.179:6100/jx/jszy.php?url=%s", "Mozilla/5.0"),
    "jyzy": ("http://91.110.167.179:6100/jx/js.php?url=%s", ""),
    "douban": ("http://91.110.167.179:6100/jx/db.php?url=%s", ""),
}
_UA_M3U8 = "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/92.0 Mobile Safari/537.36"


def _enc(q):
    return quote(q, safe=_SAFE)


def _key(path, query=""):
    s = path + ("?" + _enc(query) if query else "")
    return (s if len(s) >= 16 else s + "0" * (16 - len(s)))[:16].encode()


_FETCH = None            # Spider.init 注入的壳内 fetch（base.spider.Spider）
_SSL_UNVERIFIED = False  # 壳内常缺 CA 根证书，命中一次 SSL 错误后长期走不校验通道
_CTX = None


def _urllib_get(url, headers):
    """标准库通道；仅证书类失败时降级为不校验，避免每次请求都白等一个超时"""
    global _SSL_UNVERIFIED, _CTX
    req = _rq.Request(url, headers=headers)
    if not _SSL_UNVERIFIED:
        try:
            with _rq.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except Exception as e:
            msg = str(e).upper()
            if "SSL" not in msg and "CERT" not in msg:
                raise
            _SSL_UNVERIFIED = True
    if _CTX is None:
        _CTX = ssl.create_default_context()
        _CTX.check_hostname = False
        _CTX.verify_mode = ssl.CERT_NONE
    with _rq.urlopen(req, timeout=TIMEOUT, context=_CTX) as r:
        return r.read()


def _http(url, headers=None, retries=1, deadline=None):
    """三通道取字节：壳内 fetch -> requests -> urllib；deadline 为绝对时间戳"""
    global _FETCH
    hdr = headers or {"User-Agent": UA}
    for i in range(retries + 1):
        if deadline and time.time() >= deadline:
            raise TimeoutError("api budget exhausted")
        # 1) FongMi/TVBox 壳内通道（OkHttp，最稳）
        if _FETCH is not None:
            try:
                r = _FETCH(url, headers=hdr)
                if hasattr(r, "content") and r.content:
                    v = r.content
                    return v.encode("utf-8") if isinstance(v, str) else v
                if isinstance(r, bytes):
                    return r
                if hasattr(r, "text") and r.text:
                    v = r.text
                    return v.encode("utf-8") if isinstance(v, str) else v
                if isinstance(r, str):
                    return r.encode("utf-8")
            except Exception:
                _FETCH = None
        last = None
        # 2) 标准库（PC / 已验证可用的壳，优先）
        try:
            return _urllib_get(url, hdr)
        except Exception as e:
            last = e
        # 3) requests（部分壳里 urllib 被限制）
        try:
            import requests
            r = requests.get(url, headers=hdr, timeout=TIMEOUT, verify=False)
            if r.content:
                return r.content
        except Exception as e:
            last = e
        if i >= retries:
            raise last if last else TimeoutError("all http channels failed")
        time.sleep(0.3)


def api(path, query=""):
    """请求并解密，返回 dict；失败自动切备用域名，受总时间预算约束"""
    last = None
    deadline = time.time() + API_BUDGET
    for round_ in range(2):
        for base in list(BASES):
            if time.time() >= deadline:
                raise last if last else TimeoutError("api budget exhausted")
            try:
                url = base + path + ("?" + _enc(query) if query else "")
                raw = _http(url, retries=0, deadline=deadline).strip()
                if isinstance(raw, str):
                    raw = raw.encode("utf-8")
                if not raw or raw[:1] == b"<":
                    raise ValueError("non-api body")
                body = decrypt_ecb(base64.b64decode(raw), _key(path, query))
                data = json.loads(body.decode("utf-8", "replace"))
                if base != BASES[0]:
                    BASES.remove(base)
                    BASES.insert(0, base)   # 把能用的挪到最前
                return data
            except Exception as e:
                last = e
                continue
        if round_ == 0 and time.time() < deadline and refresh_bases(deadline):
            continue   # 换了新域名池再试一轮
    raise last if last else ValueError("no base available")


def refresh_bases(deadline=None):
    """从配置源拉最新域名池，成功则替换 BASES"""
    global BASES
    for u in CFG_URLS:
        if deadline and time.time() >= deadline:
            break
        try:
            raw = _http(u, {"User-Agent": UA}, retries=0, deadline=deadline).strip()
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            txt = decrypt_ecb(base64.b64decode(raw), CFG_KEY).decode("utf-8", "replace")
            doms = [x.strip().rstrip("/") for x in txt.splitlines() if x.strip().startswith("http")]
            if doms:
                BASES = doms
                return True
        except Exception:
            continue
    return False


def _join(*parts):
    return "$$$".join([p for p in parts if p])


# FongMi/TVBox 的 py 源壳提供 base.spider.Spider（fetch 走 Java OkHttp）；
# 普通 CPython / 其它壳没有这个包，退化为 object，由 _http 走 requests/urllib。
try:
    sys.path.append('..')
    from base.spider import Spider as _BaseSpider
except Exception:
    _BaseSpider = object

# 接口不可达时的静态分类（只含正常板块，绝不含成人分类）
FALLBACK_CLASSES = [
    {"type_id": "1", "type_name": "电影"},
    {"type_id": "2", "type_name": "剧集"},
    {"type_id": "3", "type_name": "综艺"},
    {"type_id": "4", "type_name": "动漫"},
    {"type_id": "5", "type_name": "短剧"},
    {"type_id": "12", "type_name": "AI短剧"},
]


class Spider(_BaseSpider):

    def __init__(self):
        self.extend = {}
        try:
            _BaseSpider.__init__(self)
        except Exception:
            pass

    def init(self, extend=""):
        global _FETCH
        try:
            if extend and extend.strip().startswith("{"):
                self.extend = json.loads(extend)
        except Exception:
            self.extend = {}
        try:
            if hasattr(self, "fetch") and callable(self.fetch):
                _FETCH = self.fetch
        except Exception:
            _FETCH = None
        return None

    # ---- TVBox / FongMi py 源标准接口（壳加载时会调用，缺方法可能加载失败） ----
    def getName(self):
        return "牛牛视频"

    def isVideoFormat(self, url):
        try:
            u = str(url).lower()
            return ".m3u8" in u or ".mp4" in u or ".flv" in u
        except Exception:
            return False

    def manualVideoCheck(self):
        return False

    def destroy(self):
        return None

    # ---------------- 首页 ----------------
    def homeContent(self, filter):
        res = {"class": [], "filters": {}}
        try:
            data = api("/types").get("data") or []
        except Exception:
            data = []
        if not data:
            # 接口不可达时也要给分类，否则 TVBox 判定为加载失败（空白页）
            return {"class": FALLBACK_CLASSES, "filters": {}}
        classes = []
        for t in data:
            tid = str(t.get("type_id"))
            if tid in BLOCK_TIDS:      # 屏蔽成人板块
                continue
            name = t.get("type_name") or tid
            classes.append({"type_id": tid, "type_name": name})
            ext = t.get("type_extend") or {}
            fl = []
            for k, label in (("class", "类型"), ("area", "地区"), ("year", "年份"),
                             ("lang", "语言")):
                vals = ext.get(k) or ""
                if not vals:
                    continue
                items = [{"n": "全部", "v": ""}]
                for v in str(vals).split(","):
                    v = v.strip()
                    if v:
                        items.append({"n": v, "v": v})
                if len(items) > 1:
                    fl.append({"key": k, "name": label, "value": items})
            fl.append({"key": "order", "name": "排序", "value": [
                {"n": "最新", "v": ""}, {"n": "最热", "v": "hits"},
                {"n": "评分", "v": "score"}]})
            if fl:
                res["filters"][tid] = fl
        res["class"] = classes or FALLBACK_CLASSES
        return res

    def homeVideoContent(self):
        videos = []
        try:
            data = api("/main").get("data") or []
        except Exception:
            return {"list": []}
        for sec in data:
            if str(sec.get("type_id")) in BLOCK_TIDS:   # 防御：首页板块按分类过滤
                continue
            for v in (sec.get("list") or []):
                videos.append(self._vod(v))
        return {"list": videos}

    # ---------------- 分类 ----------------
    def categoryContent(self, tid, pg, filter, extend):
        if str(tid) in BLOCK_TIDS:      # 屏蔽成人板块：直接返回空列表
            return {"list": [], "page": int(pg), "pagecount": 1, "limit": 0, "total": 0}
        ext = extend or {}
        q = "type_id=%s&page=%s" % (tid, pg)
        for k in ("class", "area", "year", "lang", "state", "order"):
            v = ext.get(k)
            if v:
                q += "&%s=%s" % (k, v)
        try:
            r = api("/list", q)
        except Exception:
            return {"list": [], "page": pg, "pagecount": 1, "limit": 12, "total": 0}
        vlist = [self._vod(v) for v in (r.get("data") or [])]
        return {"list": vlist, "page": int(pg), "pagecount": 9999,
                "limit": len(vlist) or 12, "total": 9999}

    # ---------------- 搜索 ----------------
    def searchContent(self, key, quick, pg="1"):
        try:
            r = api("/list", "wd=%s&page=%s" % (key, pg))
        except Exception:
            return {"list": []}
        vlist = []
        for v in (r.get("data") or []):
            if _is_blocked(v.get("vod_class")):
                continue
            vlist.append(self._vod(v))
        return {"list": vlist}

    # ---------------- 详情 ----------------
    def detailContent(self, ids):
        vid = str(ids[0]).strip()
        try:
            d = api("/detail", "vod_id=%s" % vid).get("data") or {}
        except Exception:
            return {"list": []}
        if not d:
            return {"list": []}
        # 兜底：即便通过 vod_id 直达，也不输出成人分类的详情
        if (str(d.get("type_id") or "") in BLOCK_TIDS
                or _is_blocked(d.get("vod_class"))):
            return {"list": []}
        play_from, play_url = [], []
        direct_lines, code_lines = [], []
        for src in (d.get("sources") or []):
            pid = src.get("player_id") or "src"
            eps = src.get("episodes") or []
            if not eps:
                continue
            names, urls = [], []
            direct = 0
            for i, ep in enumerate(eps):
                u = (ep.get("url") or "").strip()
                if not u:
                    continue
                nm = ep.get("name") or ("第%02d集" % (i + 1))
                names.append(nm)
                if "://" in u:
                    urls.append(u)
                    direct += 1
                else:
                    # 内码源（源2 加密线路），标记为不可直出
                    urls.append("nncode://" + pid + "/" + u)
            if not names:
                continue
            label = pid + ("" if direct else "(需解析)")
            line = (label, "#".join("%s$%s" % (n, u) for n, u in zip(names, urls)))
            if direct:
                direct_lines.append(line)
            elif pid in PARSERS:
                code_lines.append(line)   # 有解析接口的内码线路
            # 其余（xm3u8/xiaocao 等特殊源）域名已停，直接丢弃
        # 可直出的线路排前面，TVBox 默认用第一条
        for label, pu in direct_lines + code_lines:
            play_from.append(label)
            play_url.append(pu)
        vod = {
            "vod_id": str(d.get("vod_id", vid)),
            "vod_name": d.get("vod_name") or "",
            "vod_pic": d.get("vod_pic") or "",
            "vod_year": str(d.get("vod_year") or ""),
            "vod_area": d.get("vod_area") or "",
            "vod_remarks": d.get("vod_remarks") or "",
            "vod_actor": d.get("vod_actor") or "",
            "vod_director": d.get("vod_director") or "",
            "vod_content": re.sub(r"<[^>]+>", "", d.get("vod_content") or ""),
            "type_name": d.get("vod_class") or "",
            "vod_play_from": _join(*play_from),
            "vod_play_url": _join(*play_url),
        }
        return {"list": [vod]}

    # ---------------- 播放 ----------------
    def playerContent(self, flag, id, vipFlags):
        u = (id or "").strip()
        if u.startswith("nncode://"):
            body = u[len("nncode://"):]
            pid, _, code = body.partition("/")
            real = self._parse_code(pid, code)
            if real:
                host = re.match(r"(https?://[^/]+)/", real)
                return {"parse": 0, "playUrl": "", "url": real,
                        "header": {"User-Agent": _UA_M3U8,
                                   "Referer": (host.group(1) + "/") if host else ""}}
            return {"parse": 1, "playUrl": "", "url": u, "header": {}}
        m = re.match(r"(https?://[^/]+)/", u)
        host = m.group(1) + "/" if m else BASE + "/"
        hdr = {"User-Agent": _UA_M3U8, "Referer": host}
        return {"parse": 0, "playUrl": "", "url": u, "header": hdr}

    def _parse_code(self, pid, code):
        """内码 -> 真实播放地址（走 jx 解析接口）"""
        item = PARSERS.get(pid)
        if not item:
            return ""
        tpl, ua = item
        url = tpl % quote(code, safe="")
        try:
            raw = _http(url, {"User-Agent": ua or _UA_M3U8}).decode("utf-8", "replace")
        except Exception:
            return ""
        # JSON 形式
        try:
            j = json.loads(raw)
            for k in ("url", "play_url", "data", "vid"):
                v = j.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    return v
            if isinstance(j.get("data"), dict):
                v = j["data"].get("url")
                if isinstance(v, str) and v.startswith("http"):
                    return v
        except Exception:
            pass
        # 文本 / HTML 形式
        for pat in (r"https?://[^\s\"'<>\\]+?\.m3u8[^\s\"'<>\\]*",
                    r"https?://[^\s\"'<>\\]+?\.mp4[^\s\"'<>\\]*"):
            m = re.search(pat, raw)
            if m:
                return m.group(0).replace("\\/", "/")
        m = re.search(r'"url"\s*:\s*"([^"]+)"', raw)
        if m:
            return m.group(1).replace("\\/", "/")
        return ""

    # ---------------- 工具 ----------------
    @staticmethod
    def _vod(v):
        return {
            "vod_id": str(v.get("vod_id") or ""),
            "vod_name": v.get("vod_name") or "",
            "vod_pic": v.get("vod_pic") or "",
            "vod_remarks": v.get("vod_remarks") or "",
            "vod_year": str(v.get("vod_year") or ""),
            "vod_area": v.get("vod_area") or "",
        }
