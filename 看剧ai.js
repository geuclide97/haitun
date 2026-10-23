// 看剧ai — 官方线路专用版 v2 (fongmi/ok)
// 设置框直接粘贴浏览器 cookie 整串（kanju.ai 域，以 ai_movie_session= 开头那种）
// 只出官方线路（含 4K），剔除全部第三方资源站

import 'assets://js/lib/crypto-js.js';

function getCallerFunctionName() {
    try {
        const stackLines = new Error().stack?.split('\n') || [];
        if (stackLines.length > 2) {
            const match = stackLines[2].match(/at\s+([^\s]+)\s+/);
            if (match && match[1]) return match[1];
        }
    } catch (e) {}
    return 'anonymous';
}

function backErr(err) {
    const functionName = getCallerFunctionName();
    console.log(`backErr in ${functionName} err:`, err);
    return JSON.stringify({ msg: err?.message });
}

function mylog(...args) {
    const functionName = getCallerFunctionName();
    console.log(`【${Tag}】[${functionName}方法打印日志]`, ...args);
}

function generateNonce() {
    let result = '';
    const characters = '0123456789abcdef';
    for (let i = 0; i < 32; i++) {
        result += characters.charAt(Math.floor(Math.random() * characters.length));
    }
    return result;
}

const Tag = '看剧ai'
const baseUrl = 'https://kanju.ai'

// 官方线路白名单（含 4K）
const OFFICIAL_PROVIDER_IDS = [
    'bytedance',           // 高清-官方B
    'bytevod-cloudflare',  // 海外-官方C / 1080P-官方Z (bytevod-lv2)
    'bytevod-lv2',         // 1080P-官方Z
    'official-mj-hls',     // AI4K-专享J
    'official-v',          // 1080P-官方V
    'bytevod-cloudflare-4k',// 4K海外-官方C（真 4K）
]

// 兜底：display_label 含"官方/超清/4K/专享"也算官方（防未来新增 provider 被漏掉）
function isOfficial(line) {
    if (!line) return false;
    if (OFFICIAL_PROVIDER_IDS.includes(line.provider_id)) return true;
    const name = (line.display_label || line.provider_name || '')
    return name.includes('官方') || name.includes('超清') || name.includes('4K') || name.includes('4k') || name.includes('专享')
}

let CK = ''

async function requestWithSign(urlStr, options = {}, needSign = true) {
    const method = (options?.method || 'GET').toUpperCase();
    if (!needSign) {
        let res = await http(urlStr, {
            method: method,
            ...options,
            headers: options?.headers,
        });
        return typeof res.content === 'string' ? JSON.parse(res.content) : res.content;
    }
    const SECRET_KEY = '557d0e4ae929f438da6bd84412374e6086b8af09b3fed54bf22601d5bf8c54a0';

    const cleanUrl = urlStr.replace(/^https?:\/\/[^\/]+/, '');
    const pathAndQuery = cleanUrl.split('?')[0] + (cleanUrl.includes('?') ? '?' + cleanUrl.split('?')[1] : '');

    const timestamp = Date.now().toString();
    const nonce = generateNonce();

    let payload = `${method}\n${pathAndQuery}\n${timestamp}\n${nonce}`;
    const signature = CryptoJS.HmacSHA256(payload, SECRET_KEY).toString(CryptoJS.enc.Hex);

    const reqHeaders = {
        'accept': 'application/json',
        'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36',
        'x-ai-movie-build-version': 'dianyingtiantang-v2026.09.15.3-774bcc0013f5-web',
        'x-ai-movie-client-name': 'dianyingtiantang-frontend',
        'x-ai-movie-client-version': '1.0.0',
        'x-ai-movie-protocol-version': '2026-07-05.library-v2.playback-v1',
        'x-ai-movie-timestamp': timestamp,
        'x-ai-movie-nonce': nonce,
        'x-ai-movie-signature': signature,
        ...options?.headers
    };
    // cookie 只在非空时发（空 cookie 会被网关误判）
    if (CK) {
        reqHeaders['Cookie'] = CK;
    }

    let finalOptions = {
        method: method,
        ...options,
        headers: reqHeaders,
    };

    if (['POST', 'PUT', 'PATCH'].includes(method) && options.data) {
        finalOptions.headers['content-type'] = 'application/json; charset=utf-8';
        finalOptions.body = typeof options.data === 'object' ? JSON.stringify(options.data) : options.data;
        delete finalOptions.data;
    }

    let hsJson = JSON.stringify(finalOptions.headers || {});
    mylog('DEBUG json session pos:', hsJson.indexOf('ai_movie_session'), '| json len:', hsJson.length, '| keys:', Object.keys(finalOptions.headers || {}).join(','));
    let res = await http(urlStr, finalOptions);
    return typeof res.content === 'string' ? JSON.parse(res.content) : res.content;
}

async function resolveLineUrl(targetLine) {
    if (!targetLine) return "";

    mylog('正在解析指定线路:', targetLine.display_label || targetLine.provider_name, 'url:', targetLine.url);

    // 直连 M3U8
    if (targetLine.url_kind === 'm3u8' && !targetLine.resolve_required) {
        return targetLine.url;
    }
    // 票据源二次解析（官方线路全走这里）
    else if (targetLine.url_kind === 'resolve_ticket' || targetLine.resolve_required) {
        let ticket = targetLine.url.replace('resolve://', '');
        let lineRes = await requestWithSign(`${baseUrl}/v1/playback/resolve-line?view=compact`, {
            method: 'POST',
            data: { ticket: ticket }
        });

        mylog("票据源二次解析 lineRes", lineRes)
        if (lineRes.message) {
            // 登录锁 / 配额锁的错误文案会在这里
            throw new Error(lineRes.message)
        }
        if (lineRes && lineRes.line && lineRes.line.url) {
            return lineRes.line.url;
        }
    }
    return "";
}

function mapCard2Vod(card) {
    let playable = card?.availability?.playable
    return {
        vod_id: playable ? card.id : card?.availability?.label,
        vod_name: card.title,
        vod_pic: card.poster_url,
        vod_year: card.year,
        vod_remarks: playable ? card.remarks || card.updated_at || card.language : card?.availability?.label,
    };
}

// ===== init: 兼容字符串 ext（fongmi 标准）+ 对象 ext =====
async function init(ext) {
    let cookie = '';
    if (typeof ext === 'string') {
        cookie = ext.trim();
    } else if (ext && typeof ext === 'object') {
        cookie = (ext.cookie || '').trim();
    }
    // 支持直接贴 http/file 链接，让脚本去拉
    if (cookie.startsWith('http://') || cookie.startsWith('https://') || cookie.startsWith('file://')) {
        try {
            const res = await req(cookie);
            cookie = (res.content || '').trim();
        } catch (e) {
            mylog('拉取 cookie 链接失败:', e);
        }
    }
    CK = cookie;
    mylog('CK length:', CK.length, 'head:', CK.slice(0, 30));
    // === 自测:验证带 cookie 的签名请求是否真的把 cookie 传到服务器 ===
    try {
        let quotaRes = await requestWithSign(`${baseUrl}/v1/users/me/quota`);
        mylog('SELF-TEST quota statusCode:', quotaRes && quotaRes.statusCode, 'state:', quotaRes && quotaRes.state, 'msg:', quotaRes && quotaRes.message, 'raw:', JSON.stringify(quotaRes).slice(0, 400));
    } catch (e) {
        mylog('SELF-TEST quota error:', e.message);
    }
    // === 回显测试：确认 fongmi http 桥接是否把 cookie 发到服务器 ===
    try {
        let echoRes = await http('https://httpbin.org/headers', {
            method: 'GET',
            headers: { 'Cookie': CK, 'X-Test-Echo': 'hello123' }
        });
        let ec = (echoRes && typeof echoRes.content === 'string') ? echoRes.content : JSON.stringify(echoRes);
        mylog('ECHO:', ec.slice(0, 700));
    } catch (e) {
        mylog('ECHO error:', e.message);
    }
    return JSON.stringify({ cookie: CK, cookieLen: CK.length });
}

async function home(ext) {
    return JSON.stringify({
        class: [
            { type_id: 'sort', type_name: '大家都在看(榜单筛选)' },
            { type_id: 'tv_animation', type_name: '动漫新番' },
            { type_id: 'tv_domestic', type_name: '国产新剧' },
            { type_id: 'movie_high_score', type_name: '高分电影' },
            { type_id: 'movie_hot', type_name: '电影热播' },
            { type_id: 'movie_upcoming', type_name: '即将上映(仅展示)' },
            { type_id: 'movie_hidden_gems', type_name: '冷门好片' },
            { type_id: 'tv_korean', type_name: '韩剧在追' },
            { type_id: 'tv_japanese', type_name: '日剧上新' },
            { type_id: 'tv_american', type_name: '美剧续看' },
            { type_id: 'movie_nowplaying', type_name: '院线热映' },
            { type_id: 'show', type_name: '综艺下饭' },
        ],
        filters: {
            "sort": [
                { key: "window", name: "榜单", value: [{ n: "日榜", v: "day" }, { n: "周榜", v: "week" }, { n: "月榜", v: "month" }, { n: "总榜", v: "total" }] },
                { key: "kind", name: "内容", value: [{ "n": "全部", v: "" }, { n: "电影", v: "movie" }, { n: "剧集", v: "series" }, { n: "短剧", v: "short_drama" }, { n: "动漫", v: "anime" }, { n: "综艺", v: "variety" }] }
            ]
        }
    });
}

async function homeVod() {
    try {
        let videos = [];
        let next_cursor = null;
        let isFirstRequest = true;

        do {
            let url = isFirstRequest
                ? `${baseUrl}/v1/feed/home?scope=public&mode=preview&sections=3&cards=10`
                : `${baseUrl}/v1/feed/home?scope=public&mode=page&sections=3&cards=12&cursor=${next_cursor}`;

            isFirstRequest = false;

            let data = await requestWithSign(url);

            if (!data || !data.sections) break;

            let sections = data.sections;
            for (let i = 0, secLen = sections.length; i < secLen; i++) {
                let cards = sections[i].cards;
                if (!cards) continue;
                for (let j = 0, cardLen = cards.length; j < cardLen; j++) {
                    videos.push(mapCard2Vod(cards[j]));
                }
            }

            next_cursor = data.next_cursor;

        } while (next_cursor);

        return JSON.stringify({ list: videos });
    } catch (error) {
        return backErr(error);
    }
}

async function category(tid, pg, filter, ext) {
    try {
        let categoryUrl = '';

        if (tid === 'sort') {
            mylog('大家都在看 榜单');
            let windowType = ext?.window || 'day';
            let kindType = ext?.kind || '';

            categoryUrl = `${baseUrl}/v1/browse/catalog?sort=trending&window=${windowType}&page=${pg}&limit=20&kind=${kindType}`;
        } else {
            categoryUrl = `${baseUrl}/v1/browse/catalog?hot_list_key=${tid}&page=${pg}&limit=20`;
        }

        if (tid === 'movie_upcoming') {
            categoryUrl += '&include_unavailable=true'
        }

        mylog({ categoryUrl });
        let data = await requestWithSign(categoryUrl);

        let cards = data?.cards || []

        if (tid === 'movie_upcoming') {
            cards = cards?.filter(item => !item.availability.playable)
        }

        let videos = cards ? cards.map(card => mapCard2Vod(card)) : [];
        mylog(videos.length, videos[0]);

        let hasMore = videos.length > 0;
        let pageCount = hasMore ? pg + 1 : pg;
        return JSON.stringify({
            list: videos,
            pagecount: pageCount
        });
    } catch (error) {
        mylog('Category Error: ' + error);
        return JSON.stringify({ list: [], pagecount: pg });
    }
}

async function search(keyword, quick, pg) {
    try {
        if (pg > 1) {
            return JSON.stringify({ list: [] })
        }
        let url = `${baseUrl}/v1/threads`;
        let postData = {
            "title": keyword,
            "metadata": {
                "search_fields": "title",
                "search_scope_label": "影片名",
                "search_mode": "fast",
                "source": "movie_composer_route",
                "submission_id": "64d8a1a6-598e-4ec1-90c0-faf61a68dde1"
            }
        };

        let res = await requestWithSign(url, { method: "post", data: postData });
        let thread_id = res.id;

        if (!thread_id) {
            return JSON.stringify({ list: [] });
        }

        let searchUrl = `${baseUrl}/v1/browse/catalog?page=${pg || 1}&limit=20&query_mode=fast_v3&thread_id=${thread_id}&search_fields=title&q=${encodeURIComponent(keyword)}`;
        let searchRes = await requestWithSign(searchUrl);

        let cards = searchRes.cards || [];
        let videos = [];
        cards.forEach(card => {
            videos.push(mapCard2Vod(card));
        });

        return JSON.stringify({ list: videos, pagecount: 1 });
    } catch (e) {
        return backErr(e)
    }
}

// 官方线路权重：4K 优先 > 官方1080 > 官方标清 > 其他官方
function officialScore(name) {
    name = name.toLowerCase()
    if (name.includes('4k') || name.includes('超清')) return 5;
    if (name.includes('官方') && name.includes('1080')) return 4;
    if (name.includes('1080')) return 3;
    if (name.includes('官方') || name.includes('专享')) return 2;
    return 1;
}

function sortOfficialLines(lines) {
    return [...lines].sort((a, b) => {
        let nameA = (a.display_label || a.provider_name || '')
        let nameB = (b.display_label || b.provider_name || '')
        return officialScore(nameB) - officialScore(nameA);
    });
}

async function detail(id) {
    try {
        if (!id.startsWith('av_')) {
            throw new Error('无效影片ID: ' + id)
        }
        let detailUrl = `${baseUrl}/v1/catalog/${id}/detail`;
        let episodesUrl = `${baseUrl}/v1/catalog/${id}/episodes?limit=9999&offset=0`;

        let [detailRes, episodesRes] = await Promise.all([
            requestWithSign(detailUrl),
            requestWithSign(episodesUrl)
        ]);

        let vodInfo = detailRes || {};
        let episodes = episodesRes.episodes || [];

        if (episodes.length === 0) {
            throw new Error("未获取到任何剧集信息");
        }

        let sampleToken = episodes[0].token;
        let resolveRes = await requestWithSign(`${baseUrl}/v1/playback/resolve/${sampleToken}`);
        let sampleLineOptions = (resolveRes.line_options || []).filter(isOfficial);

        mylog("官方线路:", sampleLineOptions.map(item => item.display_label || item.provider_name));

        if (sampleLineOptions.length === 0) {
            // 区分：没官方线路 vs 登录锁
            let allLines = resolveRes.line_options || [];
            if (allLines.length > 0 && CK === '') {
                throw new Error("该片有线路但需登录，请在脚本设置里填 cookie 后重试")
            } else if (allLines.length > 0) {
                throw new Error("该片官方线路全部被锁（可能需 4K 权限或 cookie 已过期），请换条 cookie")
            } else {
                throw new Error("当前影片没有找到任何可用的播放线路")
            }
        }

        // 官方线路按权重排序（4K 优先）
        sampleLineOptions = sortOfficialLines(sampleLineOptions);

        let playSources = sampleLineOptions.map(line => line.display_label || line.provider_name || '默认线路');

        let episodesList = [];
        episodes.forEach(ep => {
            let epTitle = ep.title || `第${ep.number}集`;
            let epToken = ep.token;
            if (epToken) {
                episodesList.push(`${epTitle}$${epToken}`);
            }
        });

        let playUrlSingle = episodesList.join('#');
        let playFromList = playSources.join('$$$');
        let playUrl = playSources.map(() => playUrlSingle).join('$$$');

        let vod = {
            vod_id: vodInfo.id || id,
            vod_name: vodInfo.title || '',
            vod_pic: vodInfo.poster_url || '',
            type_name: vodInfo.genres ? vodInfo.genres.join(',') : '',
            vod_remarks: vodInfo.remarks || '',
            vod_year: vodInfo.year || '',
            vod_area: vodInfo.area || '',
            vod_actor: vodInfo.actors ? vodInfo.actors.join(',') : '',
            vod_director: vodInfo.directors ? vodInfo.directors.join(',') : '',
            vod_content: vodInfo.description || '',
            vod_play_from: playFromList,
            vod_play_url: playUrl
        };

        return JSON.stringify({ list: [vod] });
    } catch (e) {
        return backErr(e)
    }
}

async function play(flag, id, vipFlags) {
    try {
        mylog('play start, flag:', flag, 'id:', id, 'CK len:', CK.length);
        let resolveUrl = `${baseUrl}/v1/playback/resolve/${id}`;
        let resolveRes = await requestWithSign(resolveUrl);
        let lineOptions = (resolveRes.line_options || []).filter(isOfficial);

        if (lineOptions.length === 0) {
            let allLines = resolveRes.line_options || [];
            if (CK === '') {
                throw new Error("无官方线路，需登录：脚本设置里填 cookie")
            } else {
                throw new Error("官方线路全部不可用（cookie 可能过期或该片未开官方源），请检查 cookie")
            }
        }

        let targetLine = null;
        if (flag) {
            targetLine = lineOptions.find(line =>
                (line.display_label === flag) || (line.provider_name === flag)
            );
        }
        // 没指定或没匹配上 → 按权重（4K 优先）取最优
        if (!targetLine) {
            targetLine = sortOfficialLines(lineOptions)[0];
        }

        mylog('选择线路:', targetLine.display_label || targetLine.provider_name);

        let finalPlayUrl = await resolveLineUrl(targetLine);

        if (!finalPlayUrl) {
            throw new Error(`官方线路 [${targetLine.display_label || targetLine.provider_name}] 解析失败，请换一条线路或检查 cookie`);
        }

        mylog('播放地址解析成功:', finalPlayUrl);
        return JSON.stringify({
            parse: 0,
            url: finalPlayUrl,
            header: {
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
            }
        });
    } catch (e) {
        return backErr(e);
    }
}

export default { init, home, homeVod, category, detail, search, play };
