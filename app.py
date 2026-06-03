import asyncio
import json
import re
import random
import requests
import os
import shutil
import time
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright
try:
    from playwright_stealth import Stealth
except ImportError:
    Stealth = None
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Meta Ads Tool", version="2.1")

SECURE_MODE = os.getenv("SECURE_MODE", "True").lower() == "true"
CARDS_SOURCE = os.getenv("CARDS_SOURCE_URL", "https://gist.githubusercontent.com/canu101/11856a0eb14a32cfc738d84b697c30bb/raw/gistfile1.txt")

# Supabase config for license checking
SUPABASE_URL = os.getenv("NEXT_PUBLIC_SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY", "")

# ══ بصمات عشوائية لكل جلسة ══
_UAS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0',
]
_LOCALES = ['ar-EG','en-US','en-GB','fr-FR','de-DE','es-ES','it-IT']
_TZS = ['Africa/Cairo','America/New_York','Europe/London','Asia/Tokyo','Europe/Paris','America/Los_Angeles','Europe/Berlin']
_VIEWS = [(1280,900),(1366,768),(1440,900),(1536,864),(1920,1080),(1400,1050)]

Path("templates").mkdir(exist_ok=True)
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_client_ip(request: Request) -> str:
    """Extract client IP from request headers or connection"""
    # Check X-Forwarded-For header (for proxies/load balancers)
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Take the first IP in the chain
        return forwarded.split(",")[0].strip()
    # Check X-Real-IP header
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    # Fallback to direct connection IP
    if request.client:
        return request.client.host
    return ""


def normalize_cookie(c: Dict) -> Dict:
    if "domain" not in c or not c.get("domain"):
        c["domain"] = ".facebook.com"
    if not c["domain"].startswith("."):
        c["domain"] = "." + c["domain"]
    if "path" not in c or not c.get("path"):
        c["path"] = "/"
    allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    return {k: v for k, v in c.items() if k in allowed and v is not None}


def parse_cookies(cookies_input: str | list | dict) -> List[Dict]:
    try:
        if isinstance(cookies_input, str):
            stripped = cookies_input.strip()
            if stripped.startswith('['):
                raw = json.loads(stripped)
            elif stripped.startswith('{'):
                obj = json.loads(stripped)
                raw = [{"name": k, "value": str(v)} for k, v in obj.items()]
            else:
                raw = []
                for pair in stripped.split(';'):
                    if '=' in pair:
                        k, v = pair.strip().split('=', 1)
                        if k.strip():
                            raw.append({"name": k.strip(), "value": v.strip()})
        elif isinstance(cookies_input, dict):
            raw = [{"name": k, "value": str(v)} for k, v in cookies_input.items()]
        elif isinstance(cookies_input, list):
            raw = cookies_input
        else:
            raw = []
        return [normalize_cookie(c) for c in raw if c.get("name")]
    except Exception as e:
        raise ValueError(f"صيغة الكوكيز غير صحيحة: {str(e)}")


def extract_act_id(ad_account_input: str) -> str:
    """استخرج رقم الحساب الإعلاني من act=xxx / act_xxx / asset_id / payment_account_id / رقم مجرد"""
    if not ad_account_input:
        return ''
    s = ad_account_input.strip()
    # أولوية 1: act= أو act_ في الرابط أو النص
    m = re.search(r'act[=\_](\d+)', s)
    if m:
        return m.group(1)
    # أولوية 2: ?act= أو &act= (query param)
    m = re.search(r'[?&]act=(\d+)', s)
    if m:
        return m.group(1)
    # أولوية 3: asset_id= أو payment_account_id= (لينكات الفوترة الجديدة)
    m = re.search(r'[?&](?:asset_id|payment_account_id)=(\d+)', s)
    if m:
        return m.group(1)
    # أولوية 4: لو المستخدم كتب رقم صافي (بدون حروف)، نعتبره حساب
    if re.fullmatch(r'\d+', s):
        return s
    return ''


async def inject_popup_remover(page):
    try:
        await page.evaluate('''() => {
            function killPopups(){
                document.querySelectorAll('div[role="dialog"]').forEach(function(el){
                    var p = el.closest('div[class*="x1dr59a3"]');
                    if(p){ p.remove(); document.body.style.overflow='auto'; }
                });
            }
            killPopups();
            if(!window.__popupKiller){
                window.__popupKiller = new MutationObserver(function(mutations){
                    killPopups();
                });
                window.__popupKiller.observe(document.body, {childList:true, subtree:true});
            }
        }''')
    except Exception as e:
        print(f"[!] Popup remover failed: {e}")


def _get_fp_from_cookies(cookies: List[Dict]) -> random.Random:
    """بصمة ثابتة لنفس الحساب: نعمل hash للكوكيز ونستخدمه seed"""
    # serialise each cookie, sort strings, then hash
    items = [json.dumps({k: str(v) for k, v in sorted(c.items())}, sort_keys=True) for c in cookies]
    raw = ''.join(sorted(items))
    seed = int(hashlib.md5(raw.encode()).hexdigest(), 16) % (2**32)
    return random.Random(seed)


BROWSER_WS_ENDPOINT = os.getenv("BROWSER_WS_ENDPOINT", "").strip()
# ══ ⚠️ TEMP: testing with Browserless stealth mode ══
if not BROWSER_WS_ENDPOINT:
    # wss://production-sfo.browserless.io/stealth?token=YOUR_API_TOKEN&solveCaptchas=true
    BROWSER_WS_ENDPOINT = "wss://production-sfo.browserless.io/stealth?token=2UaK0vpFTjvcSqm28762ae06a9fcfb116d85fb9f88f897021&solveCaptchas=true"
# ══ TODO: move this token to env var before production ══


async def get_stealth_browser(playwright, cookies: List[Dict], proxy: Optional[str] = None, headless: bool = True):
    proxy_cfg = None
    if proxy:
        parts = proxy.split(':')
        if len(parts) == 4:
            proxy_cfg = {'server': f'http://{parts[0]}:{parts[1]}', 'username': parts[2], 'password': parts[3]}
        elif len(parts) == 2:
            proxy_cfg = {'server': f'http://{parts[0]}:{parts[1]}'}

    # ══ بصمة ثابتة لنفس الحساب ══
    rng = _get_fp_from_cookies(cookies) if cookies else random
    ua = rng.choice(_UAS)
    loc = rng.choice(_LOCALES)
    tz = rng.choice(_TZS)
    vw, vh = rng.choice(_VIEWS)
    lang_arg = loc.replace('-','_')

    # ══ device memory / hardware concurrency ثابتين ══
    device_memory = rng.choice([2, 4, 8, 16])
    hardware_concurrency = rng.choice([2, 4, 6, 8, 12])

    # ══ Browserless (Vercel) vs Local Chromium ══
    if BROWSER_WS_ENDPOINT:
        browser = await playwright.chromium.connect_over_cdp(BROWSER_WS_ENDPOINT)
    else:
        system_chromium = shutil.which("chromium") or shutil.which("chromium-browser")
        launch_kwargs = dict(
            headless=headless,
            args=[
                '--no-sandbox', '--disable-setuid-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--disable-features=ChromeWhatsNewUI,ChromeWhatsNewUI2024',
                '--disable-gpu', f'--lang={lang_arg}', f'--window-size={vw},{vh}',
                '--disable-extensions', '--disable-default-apps',
                '--disable-background-networking', '--disable-background-timer-throttling',
                '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows',
            ],
            ignore_default_args=['--enable-automation', '--enable-logging']
        )
        if system_chromium:
            launch_kwargs['executable_path'] = system_chromium
        browser = await playwright.chromium.launch(**launch_kwargs)

    ctx = await browser.new_context(
        proxy=proxy_cfg,
        user_agent=ua,
        viewport={'width': vw, 'height': vh},
        locale=loc,
        timezone_id=tz,
        accept_downloads=True,
        ignore_https_errors=True,
        extra_http_headers={
            'Accept-Language': loc.replace('_','-'),
            'Sec-CH-UA': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        }
    )
    if cookies:
        await ctx.add_cookies(cookies)
    if Stealth:
        await Stealth().apply_stealth_async(ctx)

    # ══ Anti-detection scripts ══
    await ctx.add_init_script(f'''
    () => {{
        // 1. window.screen = viewport (consistency)
        Object.defineProperty(window, 'screen', {{
            get: () => ({{
                width: {vw},
                height: {vh},
                availWidth: {vw},
                availHeight: {vh - 40},
                colorDepth: 24,
                pixelDepth: 24,
                availLeft: 0,
                availTop: 0
            }})
        }});

        // 2. deviceMemory + hardwareConcurrency
        Object.defineProperty(navigator, 'deviceMemory', {{get: () => {device_memory}}});
        Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => {hardware_concurrency}}});

        // 3. plugins (non-empty list)
        const fakePlugins = [
            {{description: "Portable Document Format", filename: "internal-pdf-viewer", name: "PDF Viewer", version: undefined}},
            {{description: "Portable Document Format", filename: "internal-pdf-viewer2", name: "Chrome PDF Viewer", version: undefined}},
            {{description: "Portable Document Format", filename: "internal-pdf-viewer3", name: "Chromium PDF Viewer", version: undefined}},
            {{description: "Portable Document Format", filename: "internal-pdf-viewer4", name: "Microsoft Edge PDF Viewer", version: undefined}},
            {{description: "Portable Document Format", filename: "internal-pdf-viewer5", name: "WebKit built-in PDF", version: undefined}}
        ];
        Object.defineProperty(navigator, 'plugins', {{get: () => {{
            fakePlugins.length = 5;
            fakePlugins.item = (i) => fakePlugins[i];
            fakePlugins.namedItem = (n) => fakePlugins.find(p => p.name === n);
            return fakePlugins;
        }}}});

        // 4. chrome.runtime
        if (typeof window.chrome === 'undefined') {{
            window.chrome = {{}};
        }}
        if (!window.chrome.runtime) {{
            window.chrome.runtime = {{
                OnInstalledReason: {{CHROME_UPDATE: "chrome_update", SHARED_MODULE_UPDATE: "shared_module_update", INSTALL: "install", UPDATE: "update", BROWSER_UPDATE: "browser_update"}},
                OnRestartRequiredReason: {{APP_UPDATE: "app_update", OS_UPDATE: "os_update", PERIODIC: "periodic"}},
                PlatformArch: {{ARM: "arm", ARM64: "arm64", MIPS: "mips", MIPS64: "mips64", X86_32: "x86-32", X86_64: "x86-64"}},
                PlatformNaclArch: {{ARM: "arm", MIPS: "mips", MIPS64: "mips64", X86_32: "x86-32", X86_64: "x86-64"}},
                PlatformOs: {{ANDROID: "android", CROS: "cros", LINUX: "linux", MAC: "mac", OPENBSD: "openbsd", WIN: "win"}},
                RequestUpdateCheckStatus: {{NO_UPDATE: "no_update", THROTTLED: "throttled", UPDATE_AVAILABLE: "update_available"}}
            }};
        }}

        // 5. notification permission
        const origNotification = window.Notification;
        Object.defineProperty(window, 'Notification', {{
            get: () => origNotification,
            set: (v) => {{}}
        }});
        if (origNotification) {{
            Object.defineProperty(origNotification, 'permission', {{get: () => 'default'}});
        }}
    }}
    ''')

    return browser, ctx


# ======================= API: التحقق واستخراج التوكن =======================
@app.post("/api/verify_and_extract")
async def verify_and_extract(request: Request):
    data = await request.json()
    cookies_raw = data.get("cookies", "")
    proxy = data.get("proxy", "").strip() or None
    billing_url = data.get("billing_url", "").strip()

    try:
        cookies = parse_cookies(cookies_raw)
    except ValueError as e:
        return {"ok": False, "reason": str(e)}

    # استخراج act_id من اللينك
    act_id = extract_act_id(billing_url)

    if billing_url:
        target_url = billing_url
        resolved_account = f"act_{act_id}" if act_id else None
    else:
        target_url = 'https://www.facebook.com/ads/manager/'
        resolved_account = None

    try:
        async with async_playwright() as p:
            browser, ctx = await get_stealth_browser(p, cookies, proxy, headless=False)
            page = await ctx.new_page()

            # ══ افتح لينك الفوترة مباشرة ══
            await page.goto(target_url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(3000)
            await inject_popup_remover(page)

            if 'login' in page.url or 'checkpoint' in page.url:
                await browser.close()
                return {"ok": False, "reason": "كوكيز منتهية أو حساب محظور أو طلب تحقق"}

            token = await page.evaluate('''() => {
                try {
                    let t = localStorage.getItem('token') || localStorage.getItem('AccessToken') || localStorage.getItem('access_token');
                    if(t && t.startsWith('EAA')) return t;
                    if(window.__accessToken) return window.__accessToken;
                    try {
                        const mod = require('DTSGInitialData');
                        if(mod && mod.token) return mod.token;
                    }catch(e){}
                    const scripts = document.querySelectorAll('script');
                    for(let s of scripts){
                        const text = s.textContent;
                        const patterns = [
                            /"accessToken":"(EAA[A-Za-z0-9]+)"/,
                            /"accessToken":"(EAAB[A-Za-z0-9]+)"/,
                            /"token":"(EAA[A-Za-z0-9]+)"/,
                            /(EAA[A-Za-z0-9]{50,})/,
                            /(EAAB[A-Za-z0-9]{50,})/
                        ];
                        for(let p of patterns){
                            const m = text.match(p);
                            if(m && m[1]) return m[1];
                        }
                    }
                    const bodyMatch = document.body.innerText.match(/(EAA[A-Za-z0-9]{50,})/);
                    if(bodyMatch) return bodyMatch[1];
                    return null;
                } catch(e) { return null; }
            }''')

            # لو التوكن مش موجود في صفحة الفوترة، نروح Ads Manager
            if not token and billing_url:
                await page.goto('https://www.facebook.com/ads/manager/', wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(3000)
                token = await page.evaluate('''() => {
                    try {
                        let t = localStorage.getItem('token') || localStorage.getItem('AccessToken') || localStorage.getItem('access_token');
                        if(t && t.startsWith('EAA')) return t;
                        const scripts = document.querySelectorAll('script');
                        for(let s of scripts){
                            const m = s.textContent.match(/(EAA[A-Za-z0-9]{50,})/);
                            if(m) return m[1];
                        }
                        return null;
                    } catch(e) { return null; }
                }''')

            content = await page.content()
            name_match = re.search(r'<title>([^<]+)</title>', content)
            name = name_match.group(1).replace('Facebook', '').strip() if name_match else 'مستخدم'

            # استخراج الحساب من الصفحة لو مفيش
            if not resolved_account:
                found = list(set(re.findall(r'act_(\d+)', content)))
                ad_like = [x for x in found if len(x) <= 15]
                resolved_account = f"act_{ad_like[0]}" if ad_like else ""

            await browser.close()
            return {
                "ok": True,
                "name": name,
                "token": token,
                "ad_account": resolved_account,
                "billing_url": billing_url
            }
    except Exception as e:
        return {"ok": False, "reason": f"خطأ: {str(e)[:100]}"}


# ======================= API: استخراج معلومات المنشور =======================
@app.post("/api/extract_post_info")
async def extract_post_info(request: Request):
    data = await request.json()
    url = data.get("url", "").strip()
    token = data.get("token", "")
    cookies_raw = data.get("cookies", "")
    proxy = data.get("proxy", "").strip() or None

    if not url:
        return {"ok": False, "reason": "الرجاء إدخال رابط المنشور"}

    post_id = None
    page_id = None
    page_slug = None

    patterns = [
        # Legacy numeric story_fbid: story_fbid=12345&id=PAGEID
        (r'story_fbid=(\d+).*?[&?]id=(\d+)', (1, 2)),
        # pfbid story_fbid: story_fbid=pfbidABC...&id=PAGEID
        (r'story_fbid=([A-Za-z0-9_]+).*?[&?]id=(\d+)', (1, 2)),
        (r'facebook\.com/(?:groups/\d+/)?([^/]+)/(?:posts|videos|photos)/(\d+)', (1, 2, True)),
        (r'[?&]fbid=(\d+)', (1,)),
        (r'/reel/(\d+)', (1,)),
    ]

    for pattern, groups in patterns:
        m = re.search(pattern, url)
        if m:
            if len(groups) == 2:
                post_id, page_id = m.group(groups[0]), m.group(groups[1])
            elif len(groups) == 3:
                page_slug, post_id = m.group(groups[0]), m.group(groups[1])
            else:
                post_id = m.group(groups[0])
            break

    if not post_id:
        return {"ok": False, "reason": "لم يتم التعرف على صيغة الرابط"}

    # ══ لو post_id هو pfbid، نحاول نحوله لرقمى ══
    if post_id and post_id.startswith('pfbid') and cookies_raw:
        try:
            cookies = parse_cookies(cookies_raw)
            async with async_playwright() as p:
                browser, ctx = await get_stealth_browser(p, cookies, proxy, headless=True)
                page = await ctx.new_page()
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(4000)

                # 1. نشوف الـ URL النهائى بعد الـ redirect
                final_url = page.url
                m = re.search(r'/posts/(\d+)', final_url)
                if m:
                    post_id = m.group(1)
                else:
                    # 2. نبحث فى صفحة HTML عن رقم المنشور
                    content = await page.content()
                    # ابحث عن "post_id":"123..." أو similar patterns
                    m = re.search(r'"post_id":"(\d+)"', content)
                    if m:
                        post_id = m.group(1)
                    else:
                        # 3. ابحث عن "id":"PAGEID_POSTID"
                        if page_id:
                            m = re.search(rf'"id":"{page_id}_(\d+)"', content)
                            if m:
                                post_id = m.group(1)

                await browser.close()
        except Exception as e:
            print(f"[!] pfbid resolution failed: {e}")
            # لو فشل التحويل، نرجع الـ pfbid زى ما هو (ممكن الـ API يقبله)
            pass

    if page_slug and not page_id and token:
        try:
            res = requests.get(f"https://graph.facebook.com/v18.0/{page_slug}",
                               params={"fields": "id,name", "access_token": token}, timeout=10).json()
            if "id" in res:
                page_id = res["id"]
        except:
            pass

    return {"ok": True, "post_id": post_id, "page_id": page_id or "", "page_slug": page_slug or ""}


# ======================= API: إضافة البطاقات =======================
@app.post("/api/add_cards")
async def add_cards(request: Request):
    data = await request.json()
    cookies_raw = data.get("cookies", "")
    proxy = data.get("proxy", "").strip() or None
    ad_account = data.get("ad_account", "")
    mode = data.get("mode", "manual")
    cards_text = data.get("cards_text", "").strip()
    billing_url = data.get("billing_url", "").strip()

    try:
        cookies = parse_cookies(cookies_raw)
    except ValueError as e:
        return {"ok": False, "reason": str(e)}

    if not billing_url and ad_account:
        act_id = ad_account.replace('act_', '').strip()
        billing_url = f'https://www.facebook.com/ads/manager/account_settings/account_billing/?act={act_id}'

    if not billing_url:
        return {"ok": False, "reason": "لم يتم تحديد حساب إعلاني — أدخل رابط الفوترة في التبويب الأول"}

    # ========== دالة تربط كارت واحد في جلسة مستقلة ==========
    async def link_one_card(card_line: str) -> dict:
        parts = card_line.strip().split('|')
        if len(parts) < 4:
            return {"card": card_line[:20], "status": "❌ صيغة خاطئة (card|mm|yyyy|cvv|name)"}

        card_num = parts[0].strip()
        mm = parts[1].strip()
        yyyy = parts[2].strip()
        cvv = parts[3].strip()
        name_on_card = parts[4].strip() if len(parts) >= 5 else "Card Holder"
        masked = f"{card_num[:6]}****{card_num[-4:]}"

        try:
            async with async_playwright() as p:
                browser, ctx = await get_stealth_browser(p, cookies, proxy, headless=False)
                page = await ctx.new_page()

                print(f"[*] جلسة جديدة: فتح {billing_url}")
                await page.goto(billing_url, wait_until='networkidle', timeout=45000)
                await page.wait_for_timeout(5000)
                await inject_popup_remover(page)

                if 'checkpoint' in page.url or 'login' in page.url:
                    await browser.close()
                    return {"card": masked, "status": "❌ كوكيز منتهية أو checkpoint"}

                # 1. Add payment method
                add_btn = page.get_by_role("button", name="Add payment method")
                await add_btn.click(timeout=15000)
                await page.wait_for_timeout(3000)

                # 2. Next
                next_btn = page.get_by_role("button", name="Next")
                await next_btn.click(timeout=10000)
                await page.wait_for_timeout(4000)

                # 3. Fill card data
                name_input = page.get_by_role("textbox", name="Name on card")
                await name_input.wait_for(timeout=8000)
                await name_input.fill(name_on_card)

                card_input = page.get_by_role("textbox", name="Card number")
                await card_input.wait_for(timeout=5000)
                await card_input.fill(card_num)

                expiry_input = page.get_by_role("textbox", name="MM/YY")
                await expiry_input.wait_for(timeout=5000)
                await expiry_input.fill(f"{mm}/{yyyy[-2:]}")

                cvv_input = page.get_by_role("textbox", name="CVV")
                await cvv_input.wait_for(timeout=5000)
                await cvv_input.fill(cvv)

                # 4. Save
                save_btn = page.get_by_role("button", name="Save")
                await save_btn.click(timeout=5000)

                # 5. انتظار طويل جداً للتأكد (20 ثانية)
                await page.wait_for_timeout(20000)

                # 6. Verify (محاولات متعددة)
                page_content = await page.content()
                text_lower = page_content.lower()

                success_keywords = [
                    "تمت إضافة ��لبطاقة", "card added", "successfully added",
                    "payment method added", "has been added", "you've added",
                    "تم إضافة", "added a new payment", "new card", "payment method saved"
                ]
                fail_keywords = [
                    "invalid", "declined", "unable to", "error", "failed",
                    "could not", "wasn't added", "not added", "try again",
                    "غير صالح", "مرفوضة", "rejected", "unfortunately",
                    "we couldn't", "something went wrong", "problem", "can't add"
                ]

                # النجاح الحقيقي = وجود كلمة نجاح + عدم وجود أي كلمة فشل
                has_success = any(k in page_content for k in success_keywords)
                has_fail = any(k in text_lower for k in fail_keywords)
                success = has_success and not has_fail
                fail = has_fail

                # لو لسه غير مؤكد، نستنى 10 ثواني تاني ونفحص تاني
                if not success and not fail:
                    await page.wait_for_timeout(10000)
                    page_content = await page.content()
                    text_lower = page_content.lower()
                    has_success = any(k in page_content for k in success_keywords)
                    has_fail = any(k in text_lower for k in fail_keywords)
                    success = has_success and not has_fail
                    fail = has_fail

                # لو لسه غير مؤكد، نستنى 10 ثواني أخرى (إجمالي 40 ثانية)
                if not success and not fail:
                    await page.wait_for_timeout(10000)
                    page_content = await page.content()
                    text_lower = page_content.lower()
                    has_success = any(k in page_content for k in success_keywords)
                    has_fail = any(k in text_lower for k in fail_keywords)
                    success = has_success and not has_fail
                    fail = has_fail

                # لو غير مؤكد، نحفظ صفحة HTML للـ debugging
                if not success and not fail:
                    try:
                        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                        snap_path = f"/tmp/card_snap_{masked}_{ts}.html"
                        with open(snap_path, 'w', encoding='utf-8') as f:
                            f.write(page_content)
                        print(f"[!] غير مؤكد — حفظت snapshot: {snap_path}")
                    except Exception as snap_err:
                        print(f"[!] فشل حفظ snapshot: {snap_err}")

                await browser.close()

                if success:
                    return {"card": masked, "status": "✅ تم الربط بنجاح"}
                elif fail:
                    return {"card": masked, "status": "❌ لم يتم الربط"}
                else:
                    return {"card": masked, "status": "⚠️ تحقق يدوياً"}

        except Exception as e:
            return {"card": masked, "status": f"❌ فشل: {str(e)[:80]}"}

    results = []

    # ========== Manual: كل كارت في جلسة مستقلة ==========
    if mode == "manual":
        lines = [l.strip() for l in cards_text.strip().split('\n') if l.strip()]
        if not lines:
            return {"ok": False, "reason": "لا توجد بطاقات صالحة"}
        for line in lines:
            result = await link_one_card(line)
            results.append(result)

    # ========== Auto: نجرب لحد ما ينجح (max 5) — كروت مختلفة ==========
    else:
        if not CARDS_SOURCE:
            return {"ok": False, "reason": "لم يتم تكوين مصدر البطاقات (CARDS_SOURCE_URL)"}

        max_attempts = 5
        tried_cards = set()
        for attempt in range(max_attempts):
            # جلب كارت عشوائي (مش متكرر)
            try:
                resp = requests.get(CARDS_SOURCE, timeout=10)
                if resp.status_code != 200:
                    results.append({"card": "—", "status": f"❌ فشل جلب المصدر: {resp.status_code}"})
                    break
                lines = [l.strip() for l in resp.text.strip().splitlines() if l.strip()]
                if not lines:
                    results.append({"card": "—", "status": "❌ قائمة البطاقات فارغة"})
                    break
                # نختار كارت مختلف عن اللى جربناه قبل كده
                available = [l for l in lines if l not in tried_cards]
                if not available:
                    results.append({"card": "—", "status": "❌ جربنا كل الكروت المتاحة"})
                    break
                card_line = random.choice(available)
                tried_cards.add(card_line)
            except Exception as e:
                results.append({"card": "—", "status": f"❌ فشل جلب البطاقات: {str(e)[:50]}"})
                break

            result = await link_one_card(card_line)
            results.append(result)

            if "✅" in result["status"]:
                break  # نجح، نوقف

            if attempt < max_attempts - 1:
                await asyncio.sleep(3)

    return {"ok": True, "results": results}


# ======================= API: تنشيط الإعلان =======================
@app.post("/api/activate_ad")
async def activate_ad(request: Request):
    data = await request.json()
    token = data.get("token", "")
    ad_id = data.get("ad_id", "")

    if not token or not ad_id:
        return {"ok": False, "reason": "بيانات ناقصة"}

    headers = {"Authorization": f"Bearer {token}"}
    base = "https://graph.facebook.com/v18.0"

    try:
        # نجيب campaign_id و adset_id تلقائياً من الـ ad
        info = requests.get(f"{base}/{ad_id}", params={"fields": "campaign_id,adset_id", "access_token": token}, timeout=10).json()
        if "error" in info:
            return {"ok": False, "reason": f"خطأ طلب معلومات الإعلان: {info['error'].get('message','')}"}
        campaign_id = info.get("campaign_id")
        adset_id = info.get("adset_id")

        if campaign_id:
            r = requests.post(f"{base}/{campaign_id}", headers=headers, data={"status": "ACTIVE"}, timeout=10).json()
            if "error" in r:
                return {"ok": False, "reason": f"خطأ الحملة: {r['error'].get('message','')}"}
        if adset_id:
            r = requests.post(f"{base}/{adset_id}", headers=headers, data={"status": "ACTIVE"}, timeout=10).json()
            if "error" in r:
                return {"ok": False, "reason": f"خطأ المجموعة: {r['error'].get('message','')}"}
        r = requests.post(f"{base}/{ad_id}", headers=headers, data={"status": "ACTIVE"}, timeout=10).json()
        if "error" in r:
            return {"ok": False, "reason": f"خطأ الإعلان: {r['error'].get('message','')}"}
        return {"ok": True, "message": "✅ تم تشغيل الإعلان"}
    except Exception as e:
        return {"ok": False, "reason": f"خطأ: {str(e)[:100]}"}


# ======================= Helper: extract fb_dtsg + lsd from FB page =======================
async def extract_fb_tokens(page) -> dict:
    """يستخرج fb_dtsg و lsd من صفحة فيسبوك بالـ Playwright"""
    try:
        await page.goto('https://www.facebook.com/', wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(2000)
        await inject_popup_remover(page)

        fb_dtsg = await page.evaluate('''() => {
            try {
                // 1. window.require modules (DTSGInitialData / DTSGInitData)
                if (typeof window.require === 'function') {
                    try {
                        const t = window.require('DTSGInitialData')?.token;
                        if (t) return t;
                    } catch(e) {}
                    try {
                        const t = window.require('DTSGInitData')?.token;
                        if (t) return t;
                    } catch(e) {}
                }
                // 2. DOM input element
                const input = document.querySelector('input[name="fb_dtsg"]');
                if (input && input.value) return input.value;
                // 3. Cookies (dtsg_ag)
                const ck = document.cookie.match(/dtsg_ag=([^;]+)/);
                if (ck && ck[1]) return decodeURIComponent(ck[1]);
                // 4. Global window.__DTSG
                if (window.__DTSG?.token) return window.__DTSG.token;
                // 5. Script tag text search
                const script = Array.from(document.querySelectorAll('script')).find(
                    s => s.textContent.includes('DTSGInitialData') || s.textContent.includes('"fb_dtsg"')
                );
                if (script) {
                    const m = script.textContent.match(/"token":"([^"]+)"/);
                    if (m) return m[1];
                }
                return null;
            } catch(e) { return null; }
        }''')

        lsd = await page.evaluate('''() => {
            try {
                const input = document.querySelector('input[name="lsd"]');
                if(input) return input.value;
                const meta = document.querySelector('meta[name="lsd"]');
                if(meta) return meta.content;
                const m = document.documentElement.innerHTML.match(/"LSD",\[\d+\],\{token:"([^"]+)"/);
                if(m) return m[1];
                return null;
            } catch(e){ return null; }
        }''')

        return {"fb_dtsg": fb_dtsg or "", "lsd": lsd or ""}
    except Exception as e:
        print(f"[!] Token extraction error: {e}")
        return {"fb_dtsg": "", "lsd": ""}


# ======================= API: Boost Ad via GraphQL (unofficial) =======================
@app.post("/api/boost_ad")
async def boost_ad(request: Request):
    data = await request.json()
    cookies_raw = data.get("cookies", "")
    proxy = data.get("proxy", "").strip() or None
    page_id = data.get("page_id", "").strip()
    post_id = data.get("post_id", "").strip()
    budget = data.get("budget", "10")
    days = int(data.get("days", 1) or 1)
    objective = data.get("objective", "MESSAGES")
    message = data.get("message", "").strip()
    cta_type = data.get("cta_type", "MESSAGE_PAGE")
    app_destination = data.get("app_destination", "MESSENGER")
    countries = data.get("countries", ["EG"])

    try:
        cookies = parse_cookies(cookies_raw)
    except ValueError as e:
        return {"ok": False, "reason": str(e)}

    if not page_id or not post_id:
        return {"ok": False, "reason": "page_id و post_id مطلوبين"}

    # ─── 1) افتح فيسبوك واستخرج fb_dtsg + lsd ───
    try:
        async with async_playwright() as p:
            browser, ctx = await get_stealth_browser(p, cookies, proxy, headless=False)
            page = await ctx.new_page()

            tokens = await extract_fb_tokens(page)
            fb_dtsg = tokens.get("fb_dtsg", "")
            lsd = tokens.get("lsd", "")

            if not fb_dtsg:
                await browser.close()
                return {"ok": False, "reason": "مش قادر استخرج fb_dtsg — جرب كوكيز تانية أو صفحة تانية"}

            # ─── 2) بناء الـ GraphQL payload ───
            creation_spec = {
                "budget": int(float(budget) * 100),
                "budget_type": "DAILY_BUDGET",
                "objective": objective,
                "targeting_spec_string": json.dumps({
                    "geo_locations": {"countries": countries},
                    "age_min": None,
                    "age_max": None
                }),
                "adgroup_specs": [
                    {
                        "creative": {
                            "object_story_spec": {
                                "link_data": {
                                    "message": message,
                                    "image_hash": "",
                                    "name": "",
                                    "call_to_action": {
                                        "type": cta_type,
                                        "value": {"app_destination": app_destination}
                                    }
                                }
                            }
                        }
                    }
                ],
                "duration_in_days": days,
                "page_id": page_id,
                "target_id": post_id
            }

            graphql_payload = {
                "fb_dtsg": fb_dtsg,
                "variables": json.dumps({"input": {"creation_spec": creation_spec}}),
                "doc_id": "9955578997835249"
            }

            # ─── 3) بناء الـ cookies string ───
            cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies if 'name' in c and 'value' in c])

            # ─── 4) إرسال الـ GraphQL request من الـ page نفسها (تجاوز CORS) ───
            boost_result = await page.evaluate(f'''async () => {{
                try {{
                    const res = await fetch("https://business.facebook.com/api/graphql/", {{
                        method: "POST",
                        headers: {{
                            "Content-Type": "application/x-www-form-urlencoded",
                            "X-FB-Friendly-Name": "LWICometCreateBoostedComponentMutation",
                            "X-FB-LSD": "{lsd or ''}"
                        }},
                        body: new URLSearchParams({json.dumps(graphql_payload)}).toString(),
                        credentials: "include"
                    }});
                    const text = await res.text();
                    try {{ return JSON.parse(text); }} catch(e) {{ return {{raw: text}}; }}
                }} catch(err) {{ return {{error: err.message}}; }}
            }}''')

            await browser.close()

            # ─── 5) Parse response واستخرج ad_id ───
            boost_json = {}
            if isinstance(boost_result, dict) and "raw" in boost_result:
                try:
                    boost_json = json.loads(boost_result["raw"])
                except:
                    boost_json = {"raw_text": boost_result["raw"][:500]}
            elif isinstance(boost_result, dict):
                boost_json = boost_result
            else:
                boost_json = {"unknown": str(boost_result)[:200]}

            # استخراج ad_id/boosted_id من أى مكان ممكن
            ad_id = None
            if "data" in boost_json and boost_json["data"]:
                for key, val in boost_json["data"].items():
                    if isinstance(val, dict):
                        # nested boosted_item
                        if "boosted_item" in val:
                            item = val["boosted_item"]
                            ad_id = item.get("id") or item.get("ad_id") or item.get("boosted_id")
                        # direct
                        ad_id = ad_id or val.get("id") or val.get("ad_id") or val.get("boosted_id")
            # fallback: regex
            if not ad_id:
                raw = json.dumps(boost_json)
                m = re.search(r'"(act_\d+)"', raw)
                if m:
                    ad_id = m.group(1)

            # ─── 6) إيقاف فورى (REST API fallback) ───
            paused = False
            if ad_id:
                act_id = ad_id.replace("act_", "").strip()
                # نحاول نجيب token من نفس الكوكيز
                token = data.get("token", "")
                if token:
                    try:
                        r = requests.post(
                            f"https://graph.facebook.com/v18.0/{ad_id}",
                            data={"status": "PAUSED", "access_token": token},
                            timeout=10
                        ).json()
                        paused = "error" not in r
                    except Exception as pause_err:
                        print(f"[!] Pause error: {pause_err}")

            return {
                "ok": True,
                "ad_id": ad_id,
                "paused": paused,
                "fb_dtsg_present": bool(fb_dtsg),
                "lsd_present": bool(lsd),
                "response_preview": json.dumps(boost_json)[:500],
                "message": "✅ تم النشر والإيقاف فوراً" if (ad_id and paused) else ("⚠️ تم النشر لكن لم يتم الإيقاف" if ad_id else "⚠️ تم الإرسال لكن لم يتم استخراج ad_id")
            }

    except Exception as e:
        return {"ok": False, "reason": f"خطأ: {str(e)[:150]}"}


# ======================= API: Check License Key with IP Protection =======================
@app.post("/api/check_license")
async def check_license(request: Request):
    data = await request.json()
    license_key = data.get("license_key", "").strip()
    verify_session = data.get("verify_session", False)
    
    if not license_key:
        return {"ok": False, "reason": "مفتاح الترخيص مطلوب"}
    
    # Get client IP
    client_ip = get_client_ip(request)
    
    # Check Supabase
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json"
            }
            
            # Query subscriptions table
            url = f"{SUPABASE_URL}/rest/v1/subscriptions?license_key=eq.{license_key}&select=*"
            res = requests.get(url, headers=headers, timeout=10)
            
            if res.status_code == 200:
                subs = res.json()
                if subs and len(subs) > 0:
                    sub = subs[0]
                    sub_id = sub.get("id")
                    is_active = sub.get("is_active", False)
                    admin_frozen = sub.get("admin_frozen", False)
                    expires_at = sub.get("expires_at")
                    allowed_ip = sub.get("allowed_ip")
                    
                    # Check if subscription is active
                    if not is_active:
                        return {"ok": False, "reason": "الاشتراك غير مفعل"}
                    
                    # Check if admin has frozen the subscription
                    if admin_frozen:
                        return {"ok": False, "reason": "تم تجميد الاشتراك من قبل المسؤول"}
                    
                    # Check expiration
                    if expires_at:
                        exp_date = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                        if exp_date < datetime.now(exp_date.tzinfo):
                            return {"ok": False, "reason": "انتهت صلاحية الاشتراك"}
                    
                    # IP Protection Logic
                    if verify_session:
                        # This is a session verification (user already logged in before)
                        # Check if IP matches the allowed_ip
                        if allowed_ip and allowed_ip != client_ip:
                            return {"ok": False, "reason": "تم فتح الأداة من جهاز آخر. لا يمكن استخدام نفس المفتاح من عدة أجهزة."}
                    else:
                        # This is a new login
                        if allowed_ip and allowed_ip != client_ip:
                            # IP already set and different - reject
                            return {"ok": False, "reason": "هذا المفتاح مرتبط بجهاز آخر. تواصل مع المسؤول لإعادة التعيين."}
                        elif not allowed_ip:
                            # First login - lock IP
                            update_url = f"{SUPABASE_URL}/rest/v1/subscriptions?id=eq.{sub_id}"
                            update_data = {
                                "allowed_ip": client_ip,
                                "current_ip": client_ip,
                                "last_login_at": datetime.now().isoformat()
                            }
                            requests.patch(update_url, headers=headers, json=update_data, timeout=10)
                        else:
                            # Same IP - update last login
                            update_url = f"{SUPABASE_URL}/rest/v1/subscriptions?id=eq.{sub_id}"
                            update_data = {
                                "current_ip": client_ip,
                                "last_login_at": datetime.now().isoformat()
                            }
                            requests.patch(update_url, headers=headers, json=update_data, timeout=10)
                    
                    return {
                        "ok": True,
                        "user_name": sub.get("user_name") or sub.get("user_email", "مستخدم"),
                        "expires_at": expires_at,
                        "allowed_ip": allowed_ip or client_ip
                    }
                else:
                    return {"ok": False, "reason": "مفتاح غير صالح"}
            else:
                return {"ok": False, "reason": "خطأ في التحقق"}
        except Exception as e:
            print(f"[!] License check error: {e}")
            return {"ok": False, "reason": "خطأ في الاتصال"}
    
    # Fallback: accept any key if no Supabase configured (for testing)
    return {
        "ok": True,
        "user_name": "مستخدم تجريبي",
        "expires_at": (datetime.now() + timedelta(days=30)).isoformat(),
        "allowed_ip": client_ip
    }


# ======================= API: Create Ad via Graph API =======================
@app.post("/api/create_ad")
async def create_ad(request: Request):
    data = await request.json()
    token = data.get("token", "").strip()
    ad_account = data.get("ad_account", "").strip()
    page_id = data.get("page_id", "").strip()
    post_id = data.get("post_id", "").strip()
    budget = data.get("budget", "10")
    days = int(data.get("days", 0) or 0)
    objective = data.get("objective", "OUTCOME_ENGAGEMENT")
    traffic_url = data.get("traffic_url", "").strip()
    targeting = data.get("targeting", {"geo_locations": {"countries": ["EG"]}, "age_min": 18, "age_max": 65})

    if not token:
        return {"ok": False, "reason": "التوكن مطلوب — استخرجه أولاً من التبويب الأول"}
    if not ad_account:
        return {"ok": False, "reason": "الحساب الإعلاني مطلوب"}
    if not page_id or not post_id:
        return {"ok": False, "reason": "page_id و post_id مطلوبين — أدخل رابط المنشور أولاً"}

    # تنظيف act_id
    act_raw = re.sub(r'[^0-9]', '', ad_account.replace("act_", ""))
    if not act_raw:
        return {"ok": False, "reason": "تنسيق الحساب الإعلاني غير صحيح"}
    act = f"act_{act_raw}"
    base = "https://graph.facebook.com/v18.0"
    headers = {"Content-Type": "application/json"}

    # mapping الهدف → optimization_goal + billing_event
    OBJ_MAP = {
        "OUTCOME_ENGAGEMENT":  ("POST_ENGAGEMENT", "POST_ENGAGEMENT"),
        "OUTCOME_TRAFFIC":     ("LINK_CLICKS",      "LINK_CLICKS"),
        "OUTCOME_AWARENESS":   ("REACH",             "IMPRESSIONS"),
        "OUTCOME_LEADS":       ("LEAD_GENERATION",   "IMPRESSIONS"),
        "OUTCOME_SALES":       ("OFFSITE_CONVERSIONS","IMPRESSIONS"),
    }
    opt_goal, billing_ev = OBJ_MAP.get(objective, ("REACH", "IMPRESSIONS"))

    try:
        daily_budget = int(float(budget) * 100)

        # ─── 1) إنشاء الحملة (form-encoded) ───
        camp_r = requests.post(f"{base}/{act}/campaigns", data={
            "name": f"Campaign_{post_id[:30]}",
            "objective": objective,
            "status": "PAUSED",
            "special_ad_categories": "[]",
            "access_token": token
        }, timeout=15).json()
        if "error" in camp_r:
            return {"ok": False, "reason": f"خطأ إنشاء الحملة: {camp_r['error'].get('message', str(camp_r['error']))}"}
        campaign_id = camp_r.get("id")

        # ─── 2) إنشاء AdSet ───
        adset_data = {
            "name": f"AdSet_{post_id[:30]}",
            "campaign_id": campaign_id,
            "daily_budget": str(daily_budget),
            "billing_event": billing_ev,
            "optimization_goal": opt_goal,
            "targeting": json.dumps(targeting),
            "status": "PAUSED",
            "access_token": token
        }
        if days and days > 0:
            end_dt = datetime.utcnow() + timedelta(days=days)
            adset_data["end_time"] = end_dt.strftime("%Y-%m-%dT%H:%M:%S+0000")
        if objective == "OUTCOME_TRAFFIC" and traffic_url:
            adset_data["optimization_goal"] = "LINK_CLICKS"
            adset_data["billing_event"] = "LINK_CLICKS"

        adset_r = requests.post(f"{base}/{act}/adsets", data=adset_data, timeout=15).json()
        if "error" in adset_r:
            return {"ok": False, "reason": f"خطأ إنشاء المجموعة: {adset_r['error'].get('message', str(adset_r['error']))}"}
        adset_id = adset_r.get("id")

        # ─── 3) إنشاء Ad Creative ───
        if objective == "OUTCOME_TRAFFIC" and traffic_url:
            story_spec = {"page_id": page_id, "link_data": {"link": traffic_url, "message": ""}}
        else:
            story_spec = {"page_id": page_id, "post_id": post_id}

        creative_r = requests.post(f"{base}/{act}/adcreatives", data={
            "name": f"Creative_{post_id[:30]}",
            "object_story_spec": json.dumps(story_spec),
            "access_token": token
        }, timeout=15).json()
        if "error" in creative_r:
            return {"ok": False, "reason": f"خطأ إنشاء الكريتيف: {creative_r['error'].get('message', str(creative_r['error']))}"}
        creative_id = creative_r.get("id")

        # ─── 4) إنشاء الإعلان ───
        ad_r = requests.post(f"{base}/{act}/ads", data={
            "name": f"Ad_{post_id[:30]}",
            "adset_id": adset_id,
            "creative": json.dumps({"creative_id": creative_id}),
            "status": "PAUSED",
            "access_token": token
        }, timeout=15).json()
        if "error" in ad_r:
            return {"ok": False, "reason": f"خطأ إنشاء الإعلان: {ad_r['error'].get('message', str(ad_r['error']))}"}
        ad_id = ad_r.get("id")

        return {
            "ok": True,
            "campaign_id": campaign_id,
            "adset_id": adset_id,
            "ad_id": ad_id,
            "message": "✅ تم إنشاء الإعلان بنجاح (متوقف — اضغط تنشيط للتشغيل)"
        }

    except Exception as e:
        return {"ok": False, "reason": f"خطأ: {str(e)[:150]}"}


# ======================= API: Verify Page Access =======================
@app.post("/api/verify_page_access")
async def verify_page_access(request: Request):
    data = await request.json()
    token = data.get("token", "").strip()
    page_id = data.get("page_id", "").strip()

    if not token or not page_id:
        return {"ok": False, "reason": "التوكن و page_id مطلوبين"}

    try:
        base = "https://graph.facebook.com/v18.0"
        # tasks field requires page token — use name + fan_count instead
        r = requests.get(
            f"{base}/{page_id}",
            params={"fields": "name,fan_count", "access_token": token},
            timeout=10
        ).json()

        if "error" in r:
            return {"ok": False, "reason": f"خطأ: {r['error'].get('message', str(r['error']))}"}

        page_name = r.get("name", page_id)
        # إذا قدرنا نقرأ الصفحة يعني عندنا صلاحية
        can_post = True

        return {"ok": True, "page_name": page_name, "can_post": can_post}

    except Exception as e:
        return {"ok": False, "reason": f"خطأ: {str(e)[:100]}"}


# ======================= Admin Routes =======================
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "besho2024")  # Change this in production!

def generate_license_key():
    """Generate a unique license key"""
    import uuid
    return f"BSH-{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[:4].upper()}"


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    return templates.TemplateResponse(request, "admin.html")


@app.post("/api/admin/login")
async def admin_login(request: Request):
    data = await request.json()
    password = data.get("password", "")
    if password == ADMIN_PASSWORD:
        return {"ok": True}
    return {"ok": False, "reason": "كلمة السر غير صحيحة"}


@app.get("/api/admin/subscriptions")
async def get_subscriptions(request: Request):
    """Get all subscriptions"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {"ok": False, "reason": "Supabase غير مكون"}
    
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"
        }
        url = f"{SUPABASE_URL}/rest/v1/subscriptions?select=*&order=created_at.desc"
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return {"ok": True, "subscriptions": resp.json()}
        return {"ok": False, "reason": "خطأ في جلب البيانات"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


@app.post("/api/admin/create_subscription")
async def create_subscription(request: Request):
    """Create a new subscription"""
    data = await request.json()
    
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {"ok": False, "reason": "Supabase غير مكون"}
    
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
        
        license_key = generate_license_key()
        days = int(data.get("days", 30))
        expires_at = (datetime.now() + timedelta(days=days)).isoformat()
        
        new_sub = {
            "license_key": license_key,
            "user_name": data.get("user_name", ""),
            "user_email": data.get("user_email", ""),
            "is_active": True,
            "expires_at": expires_at,
            "admin_frozen": False
        }
        
        url = f"{SUPABASE_URL}/rest/v1/subscriptions"
        resp = requests.post(url, headers=headers, json=new_sub, timeout=10)
        
        if resp.status_code in [200, 201]:
            created = resp.json()
            return {"ok": True, "subscription": created[0] if isinstance(created, list) else created}
        return {"ok": False, "reason": f"خطأ: {resp.text}"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


@app.post("/api/admin/update_subscription")
async def update_subscription(request: Request):
    """Update a subscription"""
    data = await request.json()
    sub_id = data.get("id")
    
    if not sub_id:
        return {"ok": False, "reason": "معرف الاشتراك مطلوب"}
    
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {"ok": False, "reason": "Supabase غير مكون"}
    
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"
        }
        
        update_data = {}
        if "is_active" in data:
            update_data["is_active"] = data["is_active"]
        if "admin_frozen" in data:
            update_data["admin_frozen"] = data["admin_frozen"]
        if "user_name" in data:
            update_data["user_name"] = data["user_name"]
        if "user_email" in data:
            update_data["user_email"] = data["user_email"]
        if "days" in data:
            update_data["expires_at"] = (datetime.now() + timedelta(days=int(data["days"]))).isoformat()
        if "reset_ip" in data and data["reset_ip"]:
            update_data["allowed_ip"] = None
            update_data["current_ip"] = None
        
        url = f"{SUPABASE_URL}/rest/v1/subscriptions?id=eq.{sub_id}"
        resp = requests.patch(url, headers=headers, json=update_data, timeout=10)
        
        if resp.status_code in [200, 204]:
            return {"ok": True}
        return {"ok": False, "reason": f"خطأ: {resp.text}"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


@app.post("/api/admin/delete_subscription")
async def delete_subscription(request: Request):
    """Delete a subscription"""
    data = await request.json()
    sub_id = data.get("id")
    
    if not sub_id:
        return {"ok": False, "reason": "معرف الاشتراك مطلوب"}
    
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {"ok": False, "reason": "Supabase غير مكون"}
    
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"
        }
        
        url = f"{SUPABASE_URL}/rest/v1/subscriptions?id=eq.{sub_id}"
        resp = requests.delete(url, headers=headers, timeout=10)
        
        if resp.status_code in [200, 204]:
            return {"ok": True}
        return {"ok": False, "reason": f"خطأ: {resp.text}"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(request, "index.html")


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 5000))
    uvicorn.run(app, host=host, port=port)
