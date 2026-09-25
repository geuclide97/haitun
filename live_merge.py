# -*- coding: utf-8 -*-
"""
全能直播聚合插件（多源合并 + 智能路由 + 自动解密）
- 支持 ext.lives 直接配置，或通过 lives_urls 远程获取多个 JSON 配置，自动解密（PNG/BMP/JPEG/WebP/AES-CBC/Base64/外部API）
- 合并去重，支持 .py 子模块（api）和 M3U/TXT 直连（url）
- 汉语化参数：包含关键词、排除关键词、匹配字段、白名单源、重复阈值、刷新间隔、启用日志
- 直连频道直接输出真实地址，无需代理；子模块通过 __src 精确路由，性能优化
- 输出格式与 zbhb.py 完全一致（每个源大分组 + 占位频道 + 内部保留分类）
- 升级：统一相对路径补全，增强下载重试，并发加载，UA改为okhttp，子模块 init 传 JSON 字符串，代理正确传递
"""

import re
import sys
import os
import time
import json
import base64
import struct
import hashlib
import threading
import importlib.util
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

try:
    from base.spider import Spider as BaseSpider
except ImportError:
    class BaseSpider:
        def getProxyUrl(self): return "http://127.0.0.1:9978/proxy?do=py&"
        def init(self, extend): pass
        def getName(self): return "Live"
        def liveContent(self, url): return ""
        def localProxy(self, params): return []
        def destroy(self): return ""

# ========================= 日志管理器 =========================
class Logger:
    def __init__(self):
        self.log_dir = '/storage/emulated/0/download/logs/'
        self.log_file = os.path.join(self.log_dir, 'live_plugin.log')
        self.enabled = False
        self._ensure_dir()
        self._log_count = 0

    def _ensure_dir(self):
        try:
            if not os.path.exists(self.log_dir):
                os.makedirs(self.log_dir, 0o755, True)
        except Exception:
            pass

    def set_enabled(self, enabled):
        self.enabled = enabled
        if enabled:
            try:
                with open(self.log_file, 'w', encoding='utf-8') as f:
                    f.write('')
            except Exception:
                pass

    def log(self, msg, data=None):
        if not self.enabled:
            return
        self._log_count += 1
        try:
            line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{self._log_count}] {msg}"
            if data is not None:
                line += ' ' + json.dumps(data, ensure_ascii=False, default=str)
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass

# ========================= 解密功能模块 =========================
_HAS_AES = False
_AES_MODE = None
try:
    from Crypto.Cipher import AES as _AES_IMPL
    _AES_MODE = 'pycryptodome'
    _HAS_AES = True
except ImportError:
    try:
        import pyaes as _AES_IMPL
        _AES_MODE = 'pyaes'
        _HAS_AES = True
    except ImportError:
        pass

DEFAULT_EXTERNAL_API_URL = "https://xn--v4q818bf34b.cc/helper/api.php"

def pad_end(key: str) -> str:
    return key + "0000000000000000"[:16 - len(key)]

def decode_png_encrypted(content: str) -> str:
    if not content or len(content) < 50:
        return None
    try:
        content = content.strip()
        r = len(content) % 4
        if r:
            content += '=' * (4 - r)
        hexdata = base64.b64decode(content).decode('ascii', errors='ignore')
        if not hexdata.startswith('2423') and not hexdata.startswith('24'):
            return None
        if len(hexdata) % 2 != 0:
            hexdata = hexdata[:-1]
        raw = bytes.fromhex(hexdata).decode('latin-1').lower()
        key_start = raw.find('$#')
        if key_start < 0: return None
        key_end = raw.find('#$', key_start + 2)
        if key_end < 0: return None
        key = raw[key_start+2:key_end]
        iv = raw[-13:]
        hdr_end = hexdata.find('2324')
        if hdr_end < 0: return None
        hdr_end += 4
        cipher_hex = hexdata[hdr_end:-26]
        if len(cipher_hex) < 32:
            return None
        cipher = bytes.fromhex(cipher_hex)
        key_full = pad_end(key).encode('latin-1')
        iv_full = pad_end(iv).encode('latin-1')
        if _AES_MODE == 'pycryptodome':
            _aes = _AES_IMPL.new(key_full, _AES_IMPL.MODE_CBC, iv_full)
            decrypted = _aes.decrypt(cipher)
        elif _AES_MODE == 'pyaes':
            _aes_cbc = _AES_IMPL.AESModeOfOperationCBC(key_full, iv=iv_full)
            _decrypter = _AES_IMPL.Decrypter(_aes_cbc)
            decrypted = _decrypter.feed(cipher)
            decrypted += _decrypter.feed()
        else:
            return None
        pad_len = decrypted[-1]
        if 0 < pad_len <= 16:
            decrypted = decrypted[:-pad_len]
        result = decrypted.decode('utf-8')
        json.loads(result)
        return result
    except Exception:
        return None

def decrypt_cbc(hex_data: str) -> str:
    if not hex_data or len(hex_data) < 50:
        return None
    try:
        cleaned = re.sub(r'\s+', '', hex_data)
        raw_bytes = bytes.fromhex(cleaned) if len(cleaned) % 2 == 0 else bytes.fromhex(cleaned[:-1])
        raw_str = raw_bytes.decode('latin-1').lower()
        key_start = raw_str.find('$#')
        if key_start < 0: return None
        key_end = raw_str.find('#$', key_start + 2)
        if key_end < 0: return None
        key = raw_str[key_start+2:key_end]
        iv = raw_str[-13:]
        hdr_end = cleaned.find('2324')
        if hdr_end < 0: return None
        hdr_end += 4
        cipher_hex = cleaned[hdr_end:-26]
        if len(cipher_hex) < 32:
            return None
        cipher = bytes.fromhex(cipher_hex)
        key_full = pad_end(key).encode('latin-1')
        iv_full = pad_end(iv).encode('latin-1')
        if _AES_MODE == 'pycryptodome':
            _aes = _AES_IMPL.new(key_full, _AES_IMPL.MODE_CBC, iv_full)
            decrypted = _aes.decrypt(cipher)
        elif _AES_MODE == 'pyaes':
            _aes = _AES_IMPL.AESModeOfOperationCBC(key_full, iv=iv_full)
            decrypted = b''
            for _i in range(0, len(cipher), 16):
                decrypted += _aes.decrypt(cipher[_i:_i+16])
        else:
            return None
        pad_len = decrypted[-1]
        if 0 < pad_len <= 16:
            decrypted = decrypted[:-pad_len]
        result = decrypted.decode('utf-8', errors='replace').strip()
        if result.startswith('{') or result.startswith('['):
            return result
        return None
    except Exception:
        return None

def decode_bmp(content: bytes) -> str:
    if len(content) < 100 or content[:2] != b'BM':
        return None
    try:
        pixel_offset = struct.unpack('<I', content[10:14])[0]
        pixel_data = content[pixel_offset:]
        for key in [0x9B, 0xAF, 0x5A, 0x66, 0x88, 0x77]:
            decoded = bytes([b ^ key for b in pixel_data])
            text = decoded.decode('utf-8', errors='replace').strip()
            start = text.find('{')
            if start < 0:
                start = text.find('[')
            if start >= 0:
                candidate = text[start:]
                depth = 0
                end = 0
                for i, ch in enumerate(candidate):
                    if ch == '{': depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                if end > 0:
                    try:
                        json.loads(candidate[:end])
                        return candidate[:end]
                    except:
                        pass
            if '#genre' in text:
                return decoded.decode('utf-8', errors='replace')
        return None
    except Exception:
        return None

def decode_image(content: bytes) -> str:
    if not content:
        return None
    if len(content) > 200 and content[:4] == b'RIFF' and b'WEBP' in content[:12]:
        text = content.decode('latin-1')
        b64s = re.findall(r'[A-Za-z0-9+/=]{200,}', text)
        for b64 in b64s:
            try:
                pad = 4 - len(b64) % 4
                if pad != 4:
                    b64 += '=' * pad
                dec = base64.b64decode(b64)
                try:
                    return json.dumps(json.loads(dec))
                except:
                    pass
            except:
                continue
    if content[:4] == b'\x89PNG':
        iend_pos = content.rfind(b'IEND')
        if iend_pos > 0:
            after_iend = content[iend_pos + 8:]
            if len(after_iend) > 50:
                b64s = re.findall(rb'[A-Za-z0-9+/]{100,}=*', after_iend)
                for b64 in b64s:
                    try:
                        padding = 4 - len(b64) % 4
                        if padding != 4:
                            b64 += b'=' * padding
                        dec = base64.b64decode(b64)
                        try:
                            return json.dumps(json.loads(dec))
                        except:
                            pass
                        try:
                            txt = dec.decode('ascii', errors='ignore').strip()
                            txt = ''.join(c for c in txt if c in '0123456789abcdefABCDEF')
                            if len(txt) > 50:
                                if len(txt) % 2:
                                    txt = txt[:-1]
                                hex_bytes = bytes.fromhex(txt)
                                try:
                                    return json.dumps(json.loads(hex_bytes))
                                except:
                                    pass
                        except:
                            pass
                    except:
                        pass
            try:
                raw_str = content.decode('latin-1')
                b64_start = after_iend.find(b'MjQ')
                if b64_start >= 0:
                    b64_part = after_iend[b64_start:]
                    b64_clean = bytes([b for b in b64_part if b in b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/='])
                    if b64_clean:
                        result = decode_png_encrypted(b64_clean.decode('ascii'))
                        if result:
                            return result
            except:
                pass
    jpeg_end = b'\xff\xd9'
    pos = content.rfind(jpeg_end)
    if pos >= 0 and pos + 2 < len(content):
        extra = content[pos + 2:]
        if extra.strip():
            try:
                return json.dumps(json.loads(extra))
            except:
                pass
            try:
                decoded = base64.b64decode(extra).decode('utf-8', errors='ignore')
                start = decoded.find('{')
                if start < 0:
                    start = decoded.find('[')
                if start >= 0:
                    return decoded[start:]
            except:
                pass
    text = content.decode('utf-8', errors='ignore')
    if text.strip():
        try:
            return json.dumps(json.loads(text))
        except:
            pass
    for b64 in re.findall(r'[A-Za-z0-9+/=]{100,}', text):
        try:
            pad = 4 - len(b64) % 4
            if pad != 4:
                b64 += '=' * pad
            dec = base64.b64decode(b64).decode('utf-8', errors='ignore')
            start = dec.find('{')
            if start < 0:
                start = dec.find('[')
            if start >= 0:
                json.loads(dec[start:])
                return dec[start:]
        except:
            continue
    return None

def is_encrypted_data(content: str) -> bool:
    if not content:
        return False
    clean = content.replace('\n', '').replace(' ', '').replace('\r', '').strip()
    if clean.startswith('2423'):
        return True
    if '2324' in clean:
        return True
    if '**' in content and len(content) > 50:
        return True
    return False

def try_decrypt_content(content: str, url: str = '', external_api_url: str = DEFAULT_EXTERNAL_API_URL, session=None) -> str:
    if not content:
        return None
    try:
        json.loads(content)
        return content
    except:
        pass
    if isinstance(content, str) and len(content) > 100:
        cleaned = re.sub(r'\s+', '', content)
        if re.match(r'^[A-Fa-f0-9]+$', cleaned) and len(cleaned) > 100:
            decrypted = decrypt_cbc(cleaned)
            if decrypted:
                return decrypted
    if isinstance(content, str) and '**' in content:
        try:
            m = re.search(r'[A-Za-z0-9]{8}\*\*(.+)', content, re.DOTALL)
            if m:
                b64_data = m.group(1).strip()
                decoded_bytes = base64.b64decode(b64_data)
                decoded_text = decoded_bytes.decode('utf-8', errors='replace')
                json.loads(decoded_text.lstrip('\ufeff').strip(), strict=False)
                return decoded_text
        except:
            pass
    if isinstance(content, str) and content[:2] == 'BM':
        decoded = decode_bmp(content.encode('latin-1'))
        if decoded:
            return decoded
    if isinstance(content, str):
        try:
            raw_bytes = content.encode('latin-1')
            img_dec = decode_image(raw_bytes)
            if img_dec:
                if isinstance(img_dec, str) and (img_dec.startswith('{') or img_dec.startswith('[')):
                    return img_dec
                elif isinstance(img_dec, dict):
                    return json.dumps(img_dec, ensure_ascii=False)
        except:
            pass
    json_str = re.search(r'\{[\s\S]*\}', content)
    if json_str:
        try:
            return json_str.group()
        except:
            pass
    if external_api_url and session:
        try:
            encoded_url = encode_url_with_chinese(url)
            if '?url=' in external_api_url:
                resp = session.get(external_api_url + encoded_url, timeout=(5, 10))
            else:
                resp = session.post(external_api_url,
                                    json={"action": "fetch_content", "params": {"url": encoded_url}, "ts": int(time.time())},
                                    timeout=(5, 10))
            if resp.status_code == 200:
                data = resp.json()
                if data.get('status') == 'success':
                    result = data.get('formattedContent') or data.get('data', '')
                    if result:
                        return result
        except Exception:
            pass
    return None

# ========================= 外部频道管理器 =========================
class ExternalChannelManager:
    def __init__(self):
        self.channels = []
        self.id_map = {}
        self.next_id = 1
        self._lock = threading.Lock()

    def add_channel(self, name, group, url, headers=None, proxy=False, source='external'):
        headers = headers or {}
        ch = {
            'name': name,
            'group': group,
            'url': url,
            'headers': headers,
            'proxy': proxy,
            'source': source,
            'id': f"ext_{self.next_id}"
        }
        with self._lock:
            self.next_id += 1
            self.channels.append(ch)
            self.id_map[ch['id']] = ch
        return ch

    def get_channel_by_id(self, ch_id):
        with self._lock:
            return self.id_map.get(ch_id)

    def clear(self):
        with self._lock:
            self.channels.clear()
            self.id_map.clear()
            self.next_id = 1

# ========================= 主 Spider 类 =========================
class Spider(BaseSpider):
    CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR, 0o755, True)

    EXTERNAL_CACHE_TTL = 300
    MODULE_CACHE_TTL = 600

    def __init__(self):
        super().__init__()
        self.logger = Logger()
        self.session = None
        self.ext_manager = ExternalChannelManager()
        self._ext_cache = {}
        self._ext_cache_lock = threading.Lock()

        self.keywords = []
        self.exclude_keywords = []
        self.match_fields = ['name']
        self.whitelist_sources = []
        self.dedup_count = 3
        self.refresh_interval = 0
        self._refresh_thread = None
        self._stop_refresh = False
        self._lock = threading.Lock()
        self._last_lives = []
        self._last_extend = None
        self._loaded_modules = {}
        self._module_m3u_cache = {}
        self._lives_sources = []
        self.external_api_url = DEFAULT_EXTERNAL_API_URL

    def _get_param(self, extend_dict, key, default):
        zh_keys = {
            'log_enabled': '启用日志',
            'proxy': '代理',
            'keywords': '包含关键词',
            'exclude_keywords': '排除关键词',
            'match_fields': '匹配字段',
            'whitelist_sources': '白名单源',
            'dedup_count': '重复阈值',
            'refresh_interval': '刷新间隔',
            'lives_urls': '远程配置地址',
            'lives_url': '远程配置地址',
            'external_api_url': '外部解密API',
        }
        zh_key = zh_keys.get(key)
        if zh_key and zh_key in extend_dict:
            return extend_dict[zh_key]
        return extend_dict.get(key, default)

    def init(self, extend):
        try:
            extend_dict = json.loads(extend) if extend else {}
        except:
            extend_dict = {}

        self._last_extend = extend_dict

        log_enabled = self._get_param(extend_dict, 'log_enabled', False)
        self.logger.set_enabled(log_enabled)
        self.logger.log("=" * 60)
        self.logger.log("Spider 启动（多源合并 + 智能路由 + 自动解密）")
        self.logger.log("=" * 60)
        self.logger.log("原始配置", extend_dict)

        self._parse_ext_params(extend_dict)
        self._init_session(extend_dict)

        merged_lives = self._fetch_and_merge_lives(extend_dict)
        self._last_lives = merged_lives
        self.logger.log("最终合并后的 lives 数量", {"总数": len(merged_lives)})

        self._load_all_sources(merged_lives)
        self._start_refresh_thread()

        self.logger.log("=" * 60)
        self.logger.log("初始化完成", {
            "总频道数": len(self.ext_manager.channels) + sum(len(v.splitlines()) for v in self._module_m3u_cache.values()),
            "直连源数量": len(self.ext_manager.channels),
            "子模块源数量": len(self._module_m3u_cache)
        })
        self.logger.log("=" * 60)

    def _parse_ext_params(self, extend_dict):
        self.keywords = self._get_param(extend_dict, 'keywords', [])
        self.exclude_keywords = self._get_param(extend_dict, 'exclude_keywords', [])
        self.match_fields = self._get_param(extend_dict, 'match_fields', ['name'])
        self.whitelist_sources = self._get_param(extend_dict, 'whitelist_sources', [])
        self.dedup_count = int(self._get_param(extend_dict, 'dedup_count', 3))
        self.refresh_interval = int(self._get_param(extend_dict, 'refresh_interval', 0))
        self.external_api_url = self._get_param(extend_dict, 'external_api_url', DEFAULT_EXTERNAL_API_URL)

        if self.dedup_count < 1:
            self.dedup_count = 1
        if self.refresh_interval < 0:
            self.refresh_interval = 0

        if isinstance(self.match_fields, str):
            self.match_fields = [f.strip() for f in self.match_fields.split(',') if f.strip()]

        self.keywords = self._compile_regex_list(self.keywords)
        self.exclude_keywords = self._compile_regex_list(self.exclude_keywords)
        self.whitelist_sources = self._compile_regex_list(self.whitelist_sources)

        self.logger.log("扩展参数", {
            "包含关键词": [p.pattern for p in self.keywords],
            "排除关键词": [p.pattern for p in self.exclude_keywords],
            "匹配字段": self.match_fields,
            "白名单源": [p.pattern for p in self.whitelist_sources],
            "重复阈值": self.dedup_count,
            "刷新间隔": self.refresh_interval,
            "外部解密API": self.external_api_url
        })

    def _compile_regex_list(self, value):
        if isinstance(value, str):
            items = [v.strip() for v in value.split(',') if v.strip()]
        elif isinstance(value, list):
            items = [str(v).strip() for v in value if v]
        else:
            items = []
        compiled = []
        for item in items:
            try:
                compiled.append(re.compile(item, re.IGNORECASE))
            except re.error:
                compiled.append(re.compile(re.escape(item), re.IGNORECASE))
        return compiled

    def _init_session(self, extend_dict):
        self.session = requests.Session()
        retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry, pool_connections=5, pool_maxsize=10)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        self.session.headers.update({
            'User-Agent': 'okhttp/4.12.0',
            'Accept-Language': 'zh-CN,zh;q=0.9'
        })

        proxy_list = self._get_param(extend_dict, 'proxy', [])
        if proxy_list:
            self.logger.log("代理配置", {"代理列表": proxy_list})
            for p in proxy_list:
                test_proxies = {'http': p, 'https': p}
                try:
                    r = requests.get('https://www.google.com', proxies=test_proxies, timeout=(3, 5))
                    if r.status_code < 400:
                        self.session.proxies = test_proxies
                        self.logger.log("代理设置成功", {"代理": p})
                        break
                except Exception as e:
                    self.logger.log("代理测试失败", {"代理": p, "错误": repr(e)})
                    continue
            else:
                self.logger.log("所有代理均不可用，将不使用代理")

    def _resolve_relative_paths(self, obj, base_url):
        if isinstance(obj, dict):
            return {k: self._resolve_relative_paths(v, base_url) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._resolve_relative_paths(v, base_url) for v in obj]
        elif isinstance(obj, str):
            if not obj.startswith(('http://', 'https://', 'data:', 'javascript:', '#')):
                if '/' in obj or '.' in obj or obj.startswith(('?', '&')):
                    return urljoin(base_url, obj)
            return obj
        else:
            return obj

    def _fetch_and_merge_lives(self, extend_dict):
        all_lives = []
        sources = []

        if 'lives' in extend_dict and isinstance(extend_dict['lives'], list):
            all_lives.extend(extend_dict['lives'])
            sources.append("ext.lives 直接配置")
            self.logger.log("从 ext.lives 读取到源", {"数量": len(extend_dict['lives'])})

        lives_urls = self._get_param(extend_dict, 'lives_urls', [])
        if isinstance(lives_urls, str):
            lives_urls = [lives_urls] if lives_urls else []
        elif not isinstance(lives_urls, list):
            lives_urls = []

        if not lives_urls:
            single_url = self._get_param(extend_dict, 'lives_url', '')
            if single_url:
                lives_urls = [single_url]

        if lives_urls:
            self.logger.log("开始并发获取远程 lives 配置（含自动解密）", {"地址数量": len(lives_urls)})
            with ThreadPoolExecutor(max_workers=min(10, len(lives_urls))) as executor:
                future_to_url = {executor.submit(self._fetch_remote_lives, url): url for url in lives_urls}
                for future in as_completed(future_to_url):
                    url = future_to_url[future]
                    try:
                        data = future.result()
                        if data:
                            data = self._resolve_relative_paths(data, url)
                            if isinstance(data, list):
                                all_lives.extend(data)
                                sources.append(f"远程 {url}")
                                self.logger.log(f"远程配置获取成功", {"url": url, "数量": len(data)})
                            elif isinstance(data, dict) and 'lives' in data and isinstance(data['lives'], list):
                                all_lives.extend(data['lives'])
                                sources.append(f"远程 {url}")
                                self.logger.log(f"远程配置获取成功（含 lives 字段）", {"url": url, "数量": len(data['lives'])})
                            else:
                                self.logger.log(f"远程配置格式无效", {"url": url})
                        else:
                            self.logger.log(f"远程配置获取失败或为空", {"url": url})
                    except Exception as e:
                        self.logger.log(f"远程配置获取异常", {"url": url, "错误": repr(e)})

        if all_lives:
            deduped = self._dedup_lives(all_lives)
            self.logger.log("合并去重后 lives 统计", {
                "合并前总数": len(all_lives),
                "去重后总数": len(deduped),
                "来源": sources
            })
            return deduped
        else:
            self.logger.log("警告：未获取到任何 lives 配置")
            return []

    def _fetch_remote_lives(self, url):
        try:
            resp = self.session.get(url, timeout=(5, 10), verify=False)
            if resp.status_code != 200:
                self.logger.log(f"远程请求失败", {"url": url, "状态码": resp.status_code})
                return None

            content = resp.content
            try:
                text = content.decode('utf-8')
            except UnicodeDecodeError:
                text = content.decode('latin-1')

            try:
                data = json.loads(text)
                return data
            except:
                pass

            self.logger.log(f"检测到加密内容，尝试解密: {url}", {"内容预览": text[:80]})
            decrypted = try_decrypt_content(text, url, self.external_api_url, self.session)
            if decrypted:
                try:
                    data = json.loads(decrypted)
                    self.logger.log(f"解密成功，已提取 JSON", {"url": url})
                    return data
                except Exception as e:
                    self.logger.log(f"解密后 JSON 解析失败", {"url": url, "错误": repr(e)})
                    json_str = re.search(r'\{[\s\S]*\}', decrypted)
                    if json_str:
                        try:
                            return json.loads(json_str.group())
                        except:
                            pass
                    lives_match = re.search(r'"lives"\s*:\s*(\[[\s\S]*?\])', decrypted)
                    if lives_match:
                        try:
                            lives_data = json.loads(lives_match.group(1))
                            if isinstance(lives_data, list):
                                return {"lives": lives_data}
                        except:
                            pass
                    return None
            else:
                self.logger.log(f"解密失败", {"url": url})
                return None
        except Exception as e:
            self.logger.log(f"远程请求异常", {"url": url, "错误": repr(e)})
            return None

    def _dedup_lives(self, lives_list):
        seen = set()
        deduped = []
        for item in lives_list:
            key = item.get('url') or item.get('api')
            if not key:
                key = json.dumps(item, sort_keys=True)
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        return deduped

    def _load_all_sources(self, lives):
        with self._lock:
            self.ext_manager.clear()
            self._loaded_modules.clear()
            self._module_m3u_cache.clear()

        with ThreadPoolExecutor(max_workers=min(10, len(lives))) as executor:
            futures = {executor.submit(self._load_single_source, item, idx): idx for idx, item in enumerate(lives)}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    self.logger.log("加载源异常", {"错误": repr(e)})

    def _load_single_source(self, item, idx):
        base_url = item.get('api') or item.get('url') or ''
        if base_url:
            item = self._resolve_relative_paths(item, base_url)

        name = item.get('name', f'源{idx+1}')
        self.logger.log("-" * 50)
        self.logger.log(f"【{name}】开始解析")

        api = item.get('api')
        url = item.get('url')

        if api:
            self._load_py_source(api, name, item, idx)
        elif url:
            self._load_direct_source(url, name, item, idx)
        else:
            self.logger.log(f"【{name}】跳过：既无 api 也无 url")

    # ---------- 加载 .py 子模块（核心修复：传递 JSON 字符串 & 正确代理） ----------
    def _load_py_source(self, api, name, item, idx):
        try:
            source_ext = item.get('ext', {})

            # 代理处理：优先使用子模块 ext 中的 proxy，若无则使用顶层 proxy
            ext_proxy = source_ext.get('proxy')
            top_proxy = item.get('proxy')
            raw_proxy = ext_proxy if ext_proxy is not None else top_proxy

            # 如果该值为 'proxy' 或 True 或空字符串，表示“需要代理但未指定地址”，则从父级获取代理列表
            if raw_proxy in (True, 'proxy', ''):
                # 使用 _get_param 正确获取父级代理（支持中文键）
                parent_proxy = self._get_param(self._last_extend, 'proxy', None)
                if parent_proxy and isinstance(parent_proxy, list):
                    source_ext['proxy'] = parent_proxy
                    self.logger.log(f"【{name}】将 proxy 设为父级代理列表", {"proxy": parent_proxy})
                else:
                    source_ext['proxy'] = []  # 父级无代理，传入空列表禁用自动探测
                    self.logger.log(f"【{name}】父级无有效代理，将 proxy 设为空列表")
            # 如果 raw_proxy 为 None（未定义），不添加，让子模块自行决定（可能会自动探测，但通常不会）

            # 合并公共配置（排除可能干扰的字段）
            common_ext = {k: v for k, v in self._last_extend.items() 
                          if k not in ('lives', 'lives_url', 'lives_urls', 'external_api_url',
                                       '启用日志', '包含关键词', '排除关键词', '匹配字段',
                                       '白名单源', '重复阈值', '刷新间隔')}
            merged_ext = common_ext.copy()
            merged_ext.update(source_ext)

            self.logger.log(f"【{name}】传递给子模块的 proxy 值", {"proxy": merged_ext.get('proxy')})

            module = self._import_py_module(api)
            if not module:
                self.logger.log(f"【{name}】模块加载失败")
                return
            if not hasattr(module, 'Spider'):
                self.logger.log(f"【{name}】模块中未找到 Spider 类")
                return

            spider = module.Spider()
            # 传入 JSON 字符串
            spider.init(json.dumps(merged_ext, ensure_ascii=False))
            content = spider.liveContent('')
            if not content:
                self.logger.log(f"【{name}】liveContent 返回空")
                return

            with self._lock:
                self._module_m3u_cache[name] = content
                self._loaded_modules[name] = spider
            self.logger.log(f"【{name}】加载成功（.py模块），内容长度 {len(content)}")
        except Exception as e:
            self.logger.log(f"【{name}】加载 .py 模块异常", {"错误": repr(e)})

    def _import_py_module(self, api):
        if api.startswith(('http://', 'https://')):
            cache_key = hashlib.md5(api.encode()).hexdigest() + '.py'
            cache_file = os.path.join(self.CACHE_DIR, cache_key)
            if os.path.exists(cache_file):
                mtime = os.path.getmtime(cache_file)
                if time.time() - mtime < self.MODULE_CACHE_TTL:
                    file_path = cache_file
                else:
                    file_path = self._download_py_module(api, cache_file)
            else:
                file_path = self._download_py_module(api, cache_file)
            if not file_path:
                return None
        else:
            if not os.path.isfile(api):
                self.logger.log(f"本地模块文件不存在: {api}")
                return None
            file_path = api

        module_name = f"py_module_{hash(file_path)}"
        if module_name in sys.modules:
            del sys.modules[module_name]
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[module_name] = module
        return module

    def _download_py_module(self, url, dest_path):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = self.session.get(url, timeout=(5, 15))
                if resp.status_code == 200:
                    content = resp.text
                    if 'class Spider' not in content:
                        self.logger.log(f"下载文件可能不完整（缺少Spider类）", {"url": url, "attempt": attempt+1})
                        if attempt == max_retries - 1:
                            return None
                        time.sleep(1)
                        continue
                    with open(dest_path, 'w', encoding='utf-8') as f:
                        f.write(content)
                    return dest_path
                else:
                    self.logger.log(f"下载失败，状态码", {"url": url, "状态码": resp.status_code, "attempt": attempt+1})
            except Exception as e:
                self.logger.log(f"下载异常", {"url": url, "错误": repr(e), "attempt": attempt+1})
            if attempt < max_retries - 1:
                time.sleep(1)
        return None

    # ---------- 加载直连源 ----------
    def _load_direct_source(self, url, name, item, idx):
        headers = {}
        if 'header' in item and isinstance(item['header'], dict):
            headers.update(item['header'])
        if 'ua' in item:
            headers['User-Agent'] = item['ua']
        if 'Referer' in item:
            headers['Referer'] = item['Referer']
        proxy = item.get('proxy', 'noproxy').lower() == 'proxy'

        self.logger.log(f"【{name}】请求信息", {
            "url": url,
            "使用代理": proxy,
            "headers": {k: v for k, v in headers.items() if k.lower() not in ['authorization', 'cookie']}
        })

        cache_key = hashlib.md5(f"{url}{json.dumps(headers, sort_keys=True)}".encode()).hexdigest()
        content = self._get_cached_ext_content(cache_key)

        if content is not None:
            self.logger.log(f"【{name}】使用缓存", {"内容长度": len(content)})
        else:
            self.logger.log(f"【{name}】发起请求...")
            try:
                resp = self._http_get_with_proxy(url, headers, proxy)
                if resp and resp.status_code == 200:
                    resp.encoding = 'utf-8'
                    content = resp.text
                    self.logger.log(f"【{name}】请求成功", {
                        "状态码": resp.status_code,
                        "内容长度": len(content)
                    })
                    self._set_cached_ext_content(cache_key, content)
                else:
                    status = resp.status_code if resp else '无响应'
                    self.logger.log(f"【{name}】请求失败", {"状态码": status})
                    return
            except Exception as e:
                self.logger.log(f"【{name}】请求异常", {"错误": repr(e)})
                return

        channels = self._parse_remote_content(content, name, headers, proxy)
        self.logger.log(f"【{name}】解析完成", {"频道数": len(channels)})

        with self._lock:
            for ch in channels:
                self.ext_manager.add_channel(
                    name=ch['name'],
                    group=ch['group'],
                    url=ch['url'],
                    headers=ch.get('headers', headers.copy()),
                    proxy=ch.get('proxy', proxy),
                    source=name
                )
        self.logger.log(f"【{name}】已添加到频道池", {"添加数量": len(channels)})

    def _parse_remote_content(self, content, source_name, default_headers, default_proxy):
        if '#EXTM3U' in content:
            return self._parse_m3u_content(content, default_headers, default_proxy)
        else:
            return self._parse_txt_content(content, default_headers, default_proxy)

    def _parse_m3u_content(self, content, default_headers=None, default_proxy=False):
        default_headers = default_headers or {}
        lines = content.splitlines()
        channels = []
        current_group = '默认分类'
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith('#EXTM3U') or line.startswith('#EXT-X-'):
                i += 1
                continue
            if line.startswith('#EXTINF:'):
                info = line
                group_match = re.search(r'group-title="([^"]+)"', info)
                if group_match:
                    current_group = group_match.group(1)
                name_match = re.search(r',([^,]+)$', info)
                ch_name = name_match.group(1).strip() if name_match else f"频道_{len(channels)}"
                i += 1
                while i < len(lines) and not lines[i].strip():
                    i += 1
                if i < len(lines):
                    url = lines[i].strip()
                    if url and not url.startswith('#'):
                        channels.append({
                            'name': ch_name,
                            'group': current_group,
                            'url': url,
                            'headers': default_headers.copy(),
                            'proxy': default_proxy
                        })
                i += 1
            else:
                if ',' in line and not line.startswith('#'):
                    parts = line.split(',', 1)
                    ch_name = parts[0].strip()
                    url = parts[1].strip()
                    if ch_name and url and not url.startswith('#'):
                        channels.append({
                            'name': ch_name,
                            'group': current_group,
                            'url': url,
                            'headers': default_headers.copy(),
                            'proxy': default_proxy
                        })
                i += 1
        return channels

    def _parse_txt_content(self, content, default_headers=None, default_proxy=False):
        default_headers = default_headers or {}
        lines = content.splitlines()
        channels = []
        current_group = '默认分类'
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '#genre#' in line:
                parts = line.split(',')
                grp = parts[0].strip()
                if grp.startswith('#'):
                    grp = grp[1:].strip()
                if grp:
                    current_group = grp
                continue
            if ',' in line:
                parts = line.split(',', 1)
                ch_name = parts[0].strip()
                url = parts[1].strip()
                if ch_name and url and not url.startswith('#'):
                    channels.append({
                        'name': ch_name,
                        'group': current_group,
                        'url': url,
                        'headers': default_headers.copy(),
                        'proxy': default_proxy
                    })
        return channels

    def _http_get_with_proxy(self, url, headers, use_proxy):
        if use_proxy and self.session.proxies:
            return self.session.get(url, headers=headers, timeout=(5, 15), verify=False)
        else:
            original = self.session.proxies
            self.session.proxies = {}
            try:
                return self.session.get(url, headers=headers, timeout=(5, 15), verify=False)
            finally:
                self.session.proxies = original

    def _get_cached_ext_content(self, key):
        with self._ext_cache_lock:
            entry = self._ext_cache.get(key)
            if entry and time.time() - entry['time'] < self.EXTERNAL_CACHE_TTL:
                return entry['content']
        return None

    def _set_cached_ext_content(self, key, content):
        with self._ext_cache_lock:
            self._ext_cache[key] = {'content': content, 'time': time.time()}

    def _add_src_to_url(self, url, src):
        parsed = urlparse(url)
        query_dict = parse_qs(parsed.query)
        query_dict['__src'] = [src]
        new_query = urlencode(query_dict, doseq=True)
        return urlunparse((
            parsed.scheme, parsed.netloc, parsed.path,
            parsed.params, new_query, parsed.fragment
        ))

    # ==================== 生成播放列表 ====================
    def liveContent(self, url):
        lines = ['#EXTM3U']
        placeholder_name = "↓↓↓↓↓↓"
        placeholder_url = "http://127.0.0.1:9978/proxy?do=py&fun=placeholder"

        with self._lock:
            source_groups = {}
            for ch in self.ext_manager.channels:
                source_groups.setdefault(ch['source'], []).append(ch)

            self.logger.log("=" * 60)
            self.logger.log("生成播放列表")
            self.logger.log("=" * 60)

            for src, ch_list in source_groups.items():
                is_whitelisted = self._is_whitelisted(src)
                groups = {}
                for ch in ch_list:
                    groups.setdefault(ch['group'], []).append(ch)

                filtered_groups = {}
                if is_whitelisted:
                    filtered_groups = groups
                else:
                    for gname, items in groups.items():
                        filtered_items = self._filter_channels(items)
                        if filtered_items:
                            filtered_groups[gname] = filtered_items

                renamed_groups = self._rename_duplicate_groups(filtered_groups)
                deduped_groups, _ = self._dedup_groups_with_count(renamed_groups, self.dedup_count)

                if not deduped_groups:
                    continue

                block_name = f"=={src}=="
                lines.append(f'\n{block_name},#genre#')
                lines.append(f'#EXTINF:-1 tvg-name="{placeholder_name}" group-title="{block_name}",{placeholder_name}')
                lines.append(placeholder_url)

                for group_name, items in deduped_groups.items():
                    lines.append(f'{group_name},#genre#')
                    for ch in items:
                        name = ch['name'].replace('"', '\\"').replace(',', '\\,')
                        if ch['headers'] or ch['proxy']:
                            proxy_url = f"http://127.0.0.1:9978/proxy?do=py&fun=external&id={ch['id']}"
                        else:
                            proxy_url = ch['url']
                        lines.append(f'#EXTINF:-1 tvg-id="{ch["id"]}" tvg-name="{name}" group-title="{group_name}",{name}')
                        lines.append(proxy_url)

            for src, m3u_content in self._module_m3u_cache.items():
                block_name = f"=={src}=="
                lines.append(f'\n{block_name},#genre#')
                lines.append(f'#EXTINF:-1 tvg-name="{placeholder_name}" group-title="{block_name}",{placeholder_name}')
                lines.append(placeholder_url)

                for line in m3u_content.splitlines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if stripped.startswith('#'):
                        if stripped.startswith('#EXTM3U'):
                            continue
                        lines.append(stripped)
                    else:
                        new_line = self._add_src_to_url(stripped, src)
                        lines.append(new_line)

            self.logger.log("播放列表生成完成", {
                "直连源频道数": len(self.ext_manager.channels),
                "子模块源数量": len(self._module_m3u_cache)
            })
            self.logger.log("=" * 60)

        return '\n'.join(lines)

    # ---------- 辅助函数 ----------
    def _is_whitelisted(self, source):
        if not self.whitelist_sources:
            return False
        for pat in self.whitelist_sources:
            if pat.search(source):
                return True
        return False

    def _filter_channels(self, ch_list):
        if not self.keywords and not self.exclude_keywords:
            return ch_list

        result = []
        for ch in ch_list:
            if self.exclude_keywords and self._match_exclude(ch):
                continue
            if self.keywords:
                if self._match_channel(ch):
                    result.append(ch)
            else:
                result.append(ch)
        return result

    def _match_channel(self, ch):
        for pat in self.keywords:
            for field in self.match_fields:
                value = self._get_field(ch, field)
                if pat.search(value):
                    return True
        return False

    def _match_exclude(self, ch):
        for pat in self.exclude_keywords:
            for field in self.match_fields:
                value = self._get_field(ch, field)
                if pat.search(value):
                    return True
        return False

    def _get_field(self, ch, field):
        if field == 'name':
            return ch.get('name', '')
        elif field == 'group':
            return ch.get('group', '')
        elif field == 'url':
            return ch.get('url', '')
        elif field == 'id':
            return ch.get('id', '')
        else:
            return ''

    def _rename_duplicate_groups(self, groups):
        name_count = {}
        new_groups = {}
        for gname, items in groups.items():
            if gname not in name_count:
                name_count[gname] = 0
            count = name_count[gname] + 1
            name_count[gname] = count
            if count == 1:
                new_name = gname
            else:
                new_name = f"{gname}_{count}"
            new_groups[new_name] = items
        return new_groups

    def _dedup_groups_with_count(self, groups, threshold):
        if threshold <= 0:
            return groups, 0

        seen_addresses = set()
        result = {}
        total_removed = 0

        for gname, items in groups.items():
            dup_count = 0
            for ch in items:
                url = ch.get('url', '')
                if url in seen_addresses:
                    dup_count += 1
            if dup_count >= threshold:
                total_removed += len(items)
                continue
            result[gname] = items
            for ch in items:
                seen_addresses.add(ch.get('url', ''))
        return result, total_removed

    # ==================== 刷新线程 ====================
    def _start_refresh_thread(self):
        if self.refresh_interval > 0 and self._refresh_thread is None:
            self._stop_refresh = False
            self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
            self._refresh_thread.start()
            self.logger.log("启动刷新线程", {"间隔(秒)": self.refresh_interval})

    def _refresh_loop(self):
        while not self._stop_refresh:
            time.sleep(self.refresh_interval)
            if self._stop_refresh:
                break
            self.logger.log("=" * 60)
            self.logger.log("开始自动刷新")
            try:
                merged_lives = self._fetch_and_merge_lives(self._last_extend)
                self._last_lives = merged_lives
                self._load_all_sources(merged_lives)
                self.logger.log("自动刷新完成", {
                    "直连源频道数": len(self.ext_manager.channels),
                    "子模块源数量": len(self._module_m3u_cache)
                })
            except Exception as e:
                self.logger.log("自动刷新异常", {"错误": repr(e)})
            self.logger.log("=" * 60)

    # ==================== 本地代理 ====================
    def localProxy(self, params):
        src = params.get('__src')
        if src and src in self._loaded_modules:
            spider = self._loaded_modules[src]
            clean_params = {k: v for k, v in params.items() if k != '__src'}
            try:
                result = spider.localProxy(clean_params)
                if result and isinstance(result, list) and len(result) >= 2:
                    if result[0] != 500:
                        return result
            except Exception as e:
                self.logger.log(f"子模块 {src}.localProxy 异常", {"错误": repr(e)})

        fun = params.get('fun')
        req_type = params.get('type')
        for src, spider in self._loaded_modules.items():
            try:
                result = spider.localProxy(params)
                if result and isinstance(result, list) and len(result) >= 2:
                    if result[0] != 500:
                        return result
            except Exception:
                continue

        if fun == 'external':
            return self._handle_external(params)
        elif fun == 'ts':
            return self._handle_ts(params)
        elif fun == 'placeholder':
            return self._error_response("占位频道，请切换到其他频道")

        self.logger.log("无法处理请求", {"params": params})
        return self._error_response("无法处理该请求")

    # ---------- 外部频道代理 ----------
    def _handle_external(self, params):
        ch_id = params.get('id')
        if not ch_id:
            return self._error_response("缺少频道ID")
        with self._lock:
            ch = self.ext_manager.get_channel_by_id(ch_id)
        if not ch:
            return self._error_response("无效的频道ID")

        if 'ts' in params:
            ts_url = self._b64_decode(params['ts'])
            try:
                resp = self._http_get_with_proxy(ts_url, ch['headers'], ch['proxy'])
                if resp.status_code != 200:
                    return self._error_response(f"TS 请求失败 {resp.status_code}")
                return [200, "video/MP2T", resp.content, {
                    'Content-Type': 'video/MP2T',
                    'Content-Length': str(len(resp.content)),
                    'Cache-Control': 'no-cache'
                }]
            except Exception as e:
                return self._error_response(f"TS 代理异常: {str(e)}")

        url = ch['url']
        headers = ch['headers']
        proxy = ch['proxy']
        try:
            resp = self._http_get_with_proxy(url, headers, proxy)
            if resp.status_code != 200:
                return self._error_response(f"外部频道请求失败 {resp.status_code}")
            content_type = resp.headers.get('Content-Type', '')
            if 'mpegurl' in content_type or 'application/vnd.apple.mpegurl' in content_type or '#EXTM3U' in resp.text[:1000]:
                m3u8_content = self._rewrite_external_m3u8(resp.text, ch_id, url)
                return [200, "application/vnd.apple.mpegurl", m3u8_content]
            else:
                return [200, resp.headers.get('Content-Type', 'application/octet-stream'), resp.content, {
                    'Content-Type': resp.headers.get('Content-Type', 'application/octet-stream'),
                    'Content-Length': str(len(resp.content)),
                    'Cache-Control': 'no-cache'
                }]
        except Exception as e:
            self.logger.log("外部频道请求异常", {"id": ch_id, "error": repr(e)})
            return self._error_response(f"请求异常: {str(e)}")

    def _rewrite_external_m3u8(self, text, ch_id, base_url):
        lines = text.splitlines()
        rewritten = []
        for line in lines:
            if line.startswith('#'):
                rewritten.append(line)
            else:
                ts_url = urljoin(base_url, line.strip())
                encoded_ts = self._b64_encode(ts_url)
                proxy_ts = f"http://127.0.0.1:9978/proxy?do=py&fun=ts&url={encoded_ts}&channel={ch_id}"
                rewritten.append(proxy_ts)
        return '\n'.join(rewritten) + '\n'

    def _handle_ts(self, params):
        b64_url = params.get('url', '')
        if not b64_url:
            return [500, "text/plain", "缺少 TS URL"]
        try:
            ts_url = self._b64_decode(b64_url)
        except:
            return [500, "text/plain", "TS URL 解码失败"]
        try:
            ch_id = params.get('channel')
            headers = {}
            if ch_id:
                with self._lock:
                    ch = self.ext_manager.get_channel_by_id(ch_id)
                if ch:
                    headers = ch['headers']
            resp = self._http_get_with_proxy(ts_url, headers, True)
            if resp.status_code != 200:
                return [500, "text/plain", f"TS 请求失败 {resp.status_code}"]
            return [200, "video/MP2T", resp.content, {
                'Content-Type': 'video/MP2T',
                'Content-Length': str(len(resp.content)),
                'Cache-Control': 'no-cache'
            }]
        except Exception as e:
            return [500, "text/plain", f"TS 代理异常: {str(e)}"]

    def _b64_encode(self, s):
        return base64.urlsafe_b64encode(s.encode()).decode().rstrip('=')

    def _b64_decode(self, s):
        padding = 4 - (len(s) % 4)
        if padding != 4:
            s += '=' * padding
        return base64.urlsafe_b64decode(s).decode()

    def _error_response(self, msg):
        error_m3u = "#EXTM3U\n#EXT-X-ENDLIST\n# " + msg
        return [500, "application/vnd.apple.mpegurl", error_m3u]

    def getName(self):
        return "全能聚合（多源合并+解密）"

    def destroy(self):
        self._stop_refresh = True
        if self._refresh_thread:
            self._refresh_thread.join(timeout=2)
        for name, spider in self._loaded_modules.items():
            try:
                spider.destroy()
            except Exception as e:
                self.logger.log(f"销毁子模块 {name} 异常", {"错误": repr(e)})
        if self.session:
            self.session.close()
        self.logger.log("=" * 60)
        self.logger.log("Spider 销毁")
        self.logger.log("=" * 60)