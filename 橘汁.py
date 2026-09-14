# -*- coding: utf-8 -*-
"""
🍊 橘汁（AppDrama 类）「蓝光秒播」源 —— TVBox Python 版 Spider

基于 9527.jar 里 csp_AppDrama（com.github.catvod.spider.AppDrama）反编译的真实逻辑复刻：
  - 真实端点 / 加解密顺序 / protobuf 请求体与响应（手工 varint 编解码，无需 protoc）

接口约定：继承 base.spider.Spider（TVBox 猫影视/影视仓 python 版），
同时保留独立运行能力（python 本文件 可直接自检）。

依赖：requests + pycryptodome
"""

import json
import time
import base64
import random
import string
import sys
import binascii
import struct

import requests

try:
    from Crypto.Cipher import AES, PKCS1_v1_5
    from Crypto.PublicKey import RSA
    from Crypto.Util.Padding import pad, unpad
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False


# ============================================================
# 基类：优先继承 TVBox python 版的 base.spider.Spider，
#      独立运行时（无该模块）则用内置兜底基类。
# ============================================================
try:
    sys.path.append('..')
    from base.spider import Spider as _BaseSpider
except Exception:
    class _BaseSpider(object):
        def log(self, msg):
            print(msg)

        def localProxy(self, param):
            return None


# ============================================================
# 默认配置（橘汁）。ext 传入后会覆盖这些字段。
#   AES 密钥是字符串直接 getBytes()（UTF-8），需 16/24/32 字符。
# ============================================================
DEFAULT_CONFIG = {
    "appName": "橘汁",
    "publicKey": "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCr8SzZhjYy+rsya1K09t8d2K50pWFoBkgUqMpKOiW+3IEVKd4eTdvg9RSOjQ82kypL6R9BnsmrS1V8s4PVDwjQbUtYhTPPC9Hz16qY7rpD6m0d2vr09/UpWQ5uOy9PR0QTrsioveZ+DIe9jc3C+zBCu/kZSY/R8stwJoiitki3gwIDAQAB",
    "dataKey": "OW1WBLFZCLJ0WTNJCDMYEGXWYVP3PT0=",
    "dataIv": "OC1A06E197EF10CF3F6058CA7A803B5E",
    "pkg": "com.mxj.wylcjbxyx",
    "version": "3.0.2.3",
    "decrypt": "1",
    "cbcKey": "ed5fdsgucxumegqa",
    "jump_urls": ["https://123-1349250429.cos.ap-shanghai.myqcloud.com/app.txt"],
    "fallback_domains": ["http://juziapp.hzhcbkj.cn"],
    "timeout": 10,
    "jump_refresh": 3600,
}

# 反编译硬编码的设备指纹（d() 里的固定值）
_DEVICE_FIXED = {
    "country": "CN", "cpuId": "MT6893Z%2FCZA", "young": 0,
    "resolution": "1080x2272", "mac": "02%3A00%3A00%3A00%3A00%3A00",
    "abid": "397", "plat": "android", "dpi": "440", "net": "1",
    "lang": "zh", "density": "2.75", "cpu": "arm64-v8a",
    "chid": "10000", "carrier": "%E8%81%94%E9%80%9A", "v": 1,
    "tenantId": "", "device": 0,
}
_DEVICE_BUILD = {
    "facturer": "Xiaomi", "model": "Redmi K50", "brand": "Redmi",
    "_vOsCode": 31, "vOs": "12",
}


# ============================================================
# 一、protobuf 手工编解码（无需 protoc）
# ============================================================
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_varint(buf, i):
    r = 0
    s = 0
    while True:
        b = buf[i]
        i += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80):
            return r, i
        s += 7


def _tag(fn, wt):
    return _varint((fn << 3) | wt)


def _fvarint(fn, val):
    return _tag(fn, 0) + _varint(val)


def _fbytes(fn, val):
    return _tag(fn, 2) + _varint(len(val)) + val


def _fstr(fn, val):
    return _fbytes(fn, val.encode("utf-8"))


def parse_message(data):
    out = {}
    i = 0
    n = len(data)
    while i < n:
        tag, i = _read_varint(data, i)
        fn = tag >> 3
        wt = tag & 7
        if wt == 0:
            val, i = _read_varint(data, i)
        elif wt == 1:
            val = data[i:i + 8]; i += 8
        elif wt == 2:
            ln, i = _read_varint(data, i)
            val = data[i:i + ln]; i += ln
        elif wt == 5:
            val = data[i:i + 4]; i += 4
        else:
            break
        if fn in out:
            out[fn] = out[fn] + [val] if isinstance(out[fn], list) else [out[fn], val]
        else:
            out[fn] = val
    return out


def _s(v):
    return v.decode("utf-8", "ignore") if isinstance(v, bytes) else v


def _i(v):
    return int(v) if not isinstance(v, bytes) else int.from_bytes(v, "little")


def _msg(v):
    return parse_message(v) if isinstance(v, bytes) else {}


# ===============
# 二、加密原语（f() / b() / a() / i()）
# ===============
_RAND_CHARS = "1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def f_rand(n):
    """f(n)：n 个随机字符 + 末尾 '='"""
    return "".join(random.choice(_RAND_CHARS) for _ in range(n)) + "="


def _norm_key(s):
    b = s.encode("utf-8")
    if len(b) in (16, 24, 32):
        return b
    for fn in (base64.b64decode, bytes.fromhex):
        try:
            d = fn(s)
            if len(d) in (16, 24, 32):
                return d
        except Exception:
            pass
    raise ValueError("无效 AES 密钥: %r" % s)


def aes_encrypt(plain, key, mode, iv=None):
    """b()：AES 加密，ECB→Base64，CBC→小写 hex"""
    k = _norm_key(key)
    data = plain.encode("utf-8")
    if mode == "CBC":
        ivb = _norm_key(iv)[:16]
        ct = AES.new(k, AES.MODE_CBC, ivb).encrypt(pad(data, AES.block_size))
        return binascii.hexlify(ct).decode()
    ct = AES.new(k, AES.MODE_ECB).encrypt(pad(data, AES.block_size))
    return base64.b64encode(ct).decode()


def aes_ecb_decrypt(cipher_b64, key):
    """a()：AES/ECB 解密，Base64 输入 → UTF-8"""
    k = _norm_key(key)
    raw = base64.b64decode(cipher_b64)
    return unpad(AES.new(k, AES.MODE_ECB).decrypt(raw), AES.block_size).decode("utf-8")


def rsa_encrypt(plain, pub_b64):
    """i()：RSA/ECB/PKCS1Padding 公钥加密（签名），输出 Base64"""
    pub = RSA.import_key(base64.b64decode(pub_b64))
    return base64.b64encode(PKCS1_v1_5.new(pub).encrypt(plain.encode("utf-8"))).decode()


# ============================================================
# 三、设备指纹 d()
# ============================================================
def _uuid_hex():
    return "".join(random.choices(string.hexdigits, k=32)).upper()


def device_info(cfg):
    uid = _uuid_hex()
    version = cfg.get("version") or ""
    info = dict(_DEVICE_FIXED)
    info.update(_DEVICE_BUILD)
    info.update({
        "vName": version,
        "pkg": cfg.get("pkg", ""),
        "uuid": uid,
        "udid": uid,
        "appName": cfg.get("appName", ""),
        "vApp": version.replace(".", ""),
        "androidID": uid,
    })
    return info


# ============================================================
# 四、publicParams 请求头（e() JSON / c() protobuf）
# ============================================================
def _json_headers(cfg):
    inner = json.dumps(device_info(cfg), separators=(",", ":"), ensure_ascii=False)
    params_data = aes_encrypt(inner, cfg["cbcKey"], "CBC", cfg["cbcKey"])
    return {
        "User-Agent": "okhttp/3.12.1",
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "publicParams": json.dumps({"paramsData": params_data}, ensure_ascii=False),
    }


def _proto_headers(cfg, dyn_pub):
    ts = int(time.time() * 1000)
    random_str = f_rand(16)
    dev = device_info(cfg)
    vapp = dev.get("vApp") or "3019"
    pub = dyn_pub or cfg["publicKey"]
    sig = rsa_encrypt(str(ts) + random_str + vapp, pub)
    aes_result = aes_encrypt(str(ts) + random_str, cfg["dataIv"], "ECB")
    inner = dict(dev)
    inner.update({
        "sig": sig, "random_str": random_str, "timestamp": ts,
        "sig2": aes_result[:8], "sig3": aes_result[8:],
    })
    params_data = aes_encrypt(
        json.dumps(inner, separators=(",", ":"), ensure_ascii=False),
        cfg["cbcKey"], "CBC", cfg["cbcKey"],
    )
    return {
        "User-Agent": "okhttp/3.12.1",
        "Accept": "application/x-protobuf",
        "Content-Type": "application/x-protobuf",
        "publicParams": json.dumps({"paramsData": params_data}, ensure_ascii=False),
    }


# ============================================================
# 五、protobuf 消息构造 / 响应解析
# ============================================================
def build_secure_request(cfg, params):
    ts = int(time.time() * 1000)
    random_str = f_rand(8)
    fake_str = f_rand(20)
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v not in (None, ""))
    aes_result = aes_encrypt(qs + str(ts), cfg["dataKey"], "ECB")
    full = random_str + aes_result
    body = b""
    body += _fstr(1, full[:20])
    body += _fstr(2, full[20:])
    body += _fstr(3, fake_str)
    body += _fvarint(4, ts)
    body += _fstr(5, random_str)
    return body


def build_rsa_request(cfg):
    ts = int(time.time() * 1000)
    random_str = f_rand(16)
    sign = rsa_encrypt(str(ts) + random_str, cfg["publicKey"])
    body = b""
    body += _fvarint(1, ts)
    body += _fstr(2, sign)
    body += _fstr(3, f_rand(16))
    body += _fstr(4, random_str)
    body += _fstr(5, f_rand(16))
    return body


def _api_data(resp_bytes):
    return parse_message(resp_bytes).get(3, b"")


def parse_rsa_public(resp_bytes):
    r = parse_message(_api_data(resp_bytes))
    return "".join(_s(r.get(i, b"")) for i in (2, 3, 4, 5))


def parse_drama_list(resp_bytes):
    r = parse_message(_api_data(resp_bytes))
    items = r.get(1, [])
    if not isinstance(items, list):
        items = [items]
    out = []
    for it in items:
        m = _msg(it)
        cover = _msg(m.get(2, b""))
        out.append({
            "vod_id": str(_i(m.get(3, 0))),
            "vod_name": _s(m.get(5, b"")),
            "vod_pic": _s(cover.get(2, b"")),
            "vod_remarks": _s(m.get(13, b"")),
        })
    return out


def parse_drama_detail(resp_bytes):
    m = parse_message(_api_data(resp_bytes))
    cover = _msg(m.get(2, b""))
    detail = {
        "vod_id": str(_i(m.get(4, 0))),
        "vod_name": _s(m.get(9, b"")),
        "vod_pic": _s(cover.get(2, b"")),
        "vod_actor": _s(m.get(25, b"")),
        "vod_director": _s(m.get(12, b"")),
        "type_name": _s(m.get(13, b"")),
        "vod_area": _s(m.get(1, b"")),
        "vod_year": str(_i(m.get(18, 0))),
        "vod_remarks": _s(m.get(26, b"")),
        "vod_content": _s(m.get(6, b"")),
    }
    videos = m.get(29, [])
    if not isinstance(videos, list):
        videos = [videos]
    groups = {}
    for v in videos:
        vd = _msg(v)
        src = _s(vd.get(10, b"")) or "橘汁"
        path = _s(vd.get(4, b""))
        title = _s(vd.get(2, b""))
        play_json = base64.b64encode(
            json.dumps({"vodPlayFrom": _s(vd.get(9, b"")), "playUrl": path},
                       ensure_ascii=False).encode("utf-8")
        ).decode()
        groups.setdefault(src, []).append(f"{title}${play_json}")
    detail["vod_play_from"] = "$$$".join(groups.keys())
    detail["vod_play_url"] = "$$$".join("#".join(eps) for eps in groups.values())
    return detail


def parse_play_url(resp_bytes):
    m = parse_message(_api_data(resp_bytes))
    headers = {}
    hdrs = m.get(6, [])
    if not isinstance(hdrs, list):
        hdrs = [hdrs]
    for h in hdrs:
        hm = _msg(h)
        headers[_s(hm.get(1, b""))] = _s(hm.get(2, b""))
    return {"url": _s(m.get(1, b"")), "header": headers}


_VIDEO_RE = __import__("re").compile(
    r"(?i).*\.(mp4|m3u8|flv|mkv|avi|ts|mov|mpd|m4a|wmv)(\?.*)?$")


# ============================================================
# 六、网络层（懒加载域名 / 动态公钥）
# ============================================================
class _Client:
    def __init__(self, cfg):
        self.cfg = cfg
        self._domain = None
        self._domain_ts = 0
        self._dyn_pub = ""
        self._pub_ts = 0

    def resolve_domain(self):
        now = time.time()
        if self._domain and (now - self._domain_ts) < self.cfg.get("jump_refresh", 3600):
            return self._domain
        for url in self.cfg.get("jump_urls", []):
            try:
                r = requests.get(url, timeout=self.cfg.get("timeout", 10))
                r.raise_for_status()
                data = r.json()
                dom = (data.get("domain") or "").strip()
                if dom and data.get("enabled", True):
                    self._domain = dom.rstrip("/")
                    self._domain_ts = now
                    return self._domain
            except Exception:
                continue
        for dom in self.cfg.get("fallback_domains", []):
            if self._probe(dom):
                self._domain = dom.rstrip("/")
                self._domain_ts = now
                return self._domain
        raise RuntimeError("无法解析可用域名")

    def _probe(self, dom):
        try:
            return requests.get(dom.rstrip("/") + "/", timeout=5).status_code == 200
        except Exception:
            return False

    @property
    def domain(self):
        return self.resolve_domain()

    def ensure_public_key(self):
        now = time.time()
        if self._dyn_pub and (now - self._pub_ts) < 3600:
            return self._dyn_pub
        headers = _proto_headers(self.cfg, "")
        r = requests.post(
            self.domain + "/api/v5/find/app/zone",
            data=build_rsa_request(self.cfg),
            headers=headers, timeout=self.cfg.get("timeout", 10),
        )
        r.raise_for_status()
        self._dyn_pub = parse_rsa_public(r.content)
        self._pub_ts = now
        return self._dyn_pub

    def get_json(self, path, params=None):
        r = requests.get(
            self.domain + path, params=params or {},
            headers=_json_headers(self.cfg), timeout=self.cfg.get("timeout", 10),
        )
        r.raise_for_status()
        return r.json()

    def post_proto(self, path, params):
        self.ensure_public_key()
        r = requests.post(
            self.domain + path, data=build_secure_request(self.cfg, params),
            headers=_proto_headers(self.cfg, self._dyn_pub),
            timeout=self.cfg.get("timeout", 10),
        )
        r.raise_for_status()
        return r.content


# ============================================================
# 七、Spider（TVBox python 版接口）
# ============================================================
class Spider(_BaseSpider):
    def __init__(self):
        self.cfg = dict(DEFAULT_CONFIG)
        self.client = _Client(self.cfg)

    # ---- 基类要求的元信息 ----
    def getName(self):
        return "橘汁"

    def getDependence(self):
        return ["pycryptodome"] if not _HAS_CRYPTO else []

    # ---- 配置注入（ext 可为 JSON 字符串或 dict）----
    def _parse_ext(self, extend):
        if not extend:
            return {}
        if isinstance(extend, dict):
            return extend
        try:
            return json.loads(extend)
        except Exception:
            return {}

    def setExtendInfo(self, extend):
        self._apply_ext(extend)
        return None

    def init(self, extend=""):
        self._apply_ext(extend)
        return None

    def _apply_ext(self, extend):
        ext = self._parse_ext(extend)
        if not ext:
            return
        # 把 ext 字段映射到内部配置
        mapping = {
            "appName": "appName", "publicKey": "publicKey", "dataKey": "dataKey",
            "dataIv": "dataIv", "pkg": "pkg", "version": "version",
            "decrypt": "decrypt",
        }
        for k, v in mapping.items():
            if k in ext:
                self.cfg[v] = ext[k]
        if "site" in ext:
            self.cfg["jump_urls"] = [ext["site"]] if isinstance(ext["site"], str) else list(ext["site"])
        if ext.get("host"):
            self.cfg["fallback_domains"] = [ext["host"]]
        if "timeout" in ext:
            self.cfg["timeout"] = int(ext["timeout"])

    # ---- 首页分类 ----
    def homeContent(self, filter=False):
        try:
            data = self.client.get_json("/api/v3/drama/getCategory", {"orderBy": "type_id"})
            arr = data.get("data") or []
            classes, filters = [], {}
            for item in arr:
                cid = str(item.get("id", ""))
                name = item.get("name", "")
                if name == "公告":
                    continue
                classes.append({"type_id": cid, "type_name": name})
                conv = item.get("converUrl") or ""
                if conv:
                    try:
                        cj = json.loads(conv)
                    except Exception:
                        cj = {}
                    fgroups = []
                    for key in ("class", "lang", "area", "year", "extend_sort"):
                        if cj.get(key):
                            vals = [v for v in str(cj[key]).split(",") if v]
                            fgroups.append({"key": key, "name": key,
                                            "value": [{"n": v, "v": v} for v in vals]})
                    if fgroups:
                        filters[cid] = fgroups
            return {"class": classes, "filters": filters}
        except Exception as e:
            self.log("橘汁 homeContent 失败: %s" % e)
            return {"class": [], "filters": {}}

    # ---- 首页推荐 ----
    def homeVideoContent(self):
        try:
            data = self.client.get_json("/api/ex/v3/security/tag/list")
            raw = data.get("data") or ""
            if not raw:
                return {"list": []}
            if self.cfg.get("decrypt", "1") != "0":
                raw = aes_ecb_decrypt(raw, self.cfg["dataKey"])
                raw = aes_ecb_decrypt(raw, self.cfg["dataIv"])
            arr = json.loads(raw)
            out = []
            for sec in arr:
                for s in (sec.get("sections") or []):
                    for v in (s.get("vodList") or []):
                        cover = v.get("coverImage") or {}
                        out.append({
                            "vod_id": str(v.get("id", "")),
                            "vod_name": v.get("name", ""),
                            "vod_pic": cover.get("path", ""),
                            "vod_remarks": v.get("remark", ""),
                        })
            return {"list": out}
        except Exception as e:
            self.log("橘汁 homeVideoContent 失败: %s" % e)
            return {"list": []}

    # ---- 分类列表 ----
    def categoryContent(self, tid, pg, filter, extend):
        ext = self._parse_ext(extend)
        params = {
            "pagesize": "21", "typeId1": str(tid), "page": str(pg),
            "vodOrderBy": ext.get("extend_sort", "最新"),
            "vodArea": ext.get("area", ""),
            "vodLang": ext.get("lang", ""),
            "vodClass": ext.get("class", ""),
            "vodYear": ext.get("year", ""),
        }
        try:
            resp = self.client.post_proto("/api/proto/v5/drama/category", params)
            return {"list": parse_drama_list(resp), "page": int(pg), "pagecount": 1}
        except Exception as e:
            self.log("橘汁 categoryContent 失败: %s" % e)
            return {"list": [], "page": int(pg), "pagecount": 1}

    # ---- 详情 ----
    def detailContent(self, ids):
        try:
            resp = self.client.post_proto("/api/proto/v5/drama/getDetail", {"id": str(ids[0])})
            return {"list": [parse_drama_detail(resp)]}
        except Exception as e:
            self.log("橘汁 detailContent 失败: %s" % e)
            return {"list": []}

    # ---- 搜索 ----
    def searchContent(self, key, quick, pg="1"):
        try:
            resp = self.client.post_proto("/api/proto/v5/drama/search", {
                "searchKeys": str(key), "page": str(pg), "pagesize": "21",
            })
            return {"list": parse_drama_list(resp), "page": int(pg)}
        except Exception as e:
            self.log("橘汁 searchContent 失败: %s" % e)
            return {"list": [], "page": int(pg)}

    # ---- 播放 ----
    def playerContent(self, flag, id, vipFlags):
        try:
            value = str(id or "").strip()
            if _VIDEO_RE.match(value):
                return {"parse": 0, "playUrl": "", "url": value, "header": {}}
            params = json.loads(base64.b64decode(value).decode("utf-8"))
            resp = self.client.post_proto("/api/proto/v5/videoUsableUrl", params)
            return {"parse": 0, "playUrl": "", **parse_play_url(resp)}
        except Exception as e:
            self.log("橘汁 playerContent 失败: %s" % e)
            return {"parse": 0, "playUrl": "", "url": str(id), "header": {}}

    # ---- 释放 ----
    def destroy(self):
        return None

    # ---- 可选方法（与标准 Spider 对齐）----
    def homeLayout(self):
        return 0

    def manualVideoCheck(self):
        return False

    def isVideoFormat(self, url):
        return bool(_VIDEO_RE.match(str(url or "")))


# ============================================================
# 八、独立自检
# ============================================================
if __name__ == "__main__":
    print("=" * 50)
    print("橘汁 AppDrama 复刻自检")
    print("=" * 50)
    s = Spider()
    try:
        dom = s.client.resolve_domain()
        print("✅ 域名:", dom)
        s.client.ensure_public_key()
        print("✅ 动态公钥: OK")
        h = s.homeContent(False)
        print("✅ 首页分类:", [c["type_name"] for c in h["class"]])
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("❌ 自检失败:", e)
