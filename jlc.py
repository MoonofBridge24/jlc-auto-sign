import os
import re
import sys
import time
import json
import tempfile
import random
import requests
import selenium
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    import captcha_solver
except ImportError:
    captcha_solver = None

try:
    import stealth
except ImportError:
    stealth = None

from jlc_api import (AuthExpired, ApiError, OshwhubClient, JlcBeanClient, MOBILE_UA)

# 默认无头运行（GitHub Actions 没有显示器）；本地调试可以设 JLC_HEADLESS=0 打开窗口
HEADLESS = os.getenv("JLC_HEADLESS", "1") != "0"
# 浏览器类型：chrome / edge，默认 chrome（本地没有 Chrome 时可指定 edge）
BROWSER = os.getenv("JLC_BROWSER", "chrome").strip().lower()
SELENIUM_MAJOR = int(str(getattr(selenium, "__version__", "4")).split(".")[0])


# 浏览器真实 UA（无头模式被 CDP 覆盖后的值），供 requests 侧统一使用
BROWSER_UA = ""

# 全局变量用于收集总结日志
in_summary = False
summary_logs = []

def log(msg):
    full_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(full_msg, flush=True)
    if in_summary:
        summary_logs.append(msg)  # 只收集纯消息，无时间戳

def _apply_ua_override(driver):
    """
    无头模式下把 UA 里的 HeadlessChrome 改掉（会话级，UA 请求头一起改）。

    这是「有头能过、无头过不去」的根因：--headless 启动的浏览器，HTTP 请求头里
    的 User-Agent 是 "...HeadlessChrome/158.0.0.0..."。只在 JS 里改 navigator.userAgent
    没用——服务端和 WAF 看的是请求头，于是 oshwhub 的瑞数 CloudWAF 直接返回
    418「访问被拦截！」，登录后取用户信息、签到接口全部失败。

    用 CDP 在会话层覆盖，UA 请求头和 navigator.userAgent 会同时生效，
    且不写死版本号：直接取当前浏览器的真实 UA，去掉 Headless 字样。
    Linux（GitHub Actions）下也就自然得到 X11; Linux x86_64 的正常 UA。
    """
    if not hasattr(driver, "execute_cdp_cmd"):
        return
    ua = ""
    try:
        info = driver.execute_cdp_cmd("Browser.getVersion", {}) or {}
        ua = info.get("userAgent") or ""
    except Exception:
        # 个别驱动不支持 Browser.getVersion，退回页面里的 UA
        ua = ""

    if not ua:
        try:
            ua = driver.execute_script("return navigator.userAgent;") or ""
        except Exception:
            return

    if "Headless" not in ua:
        return

    clean = ua.replace("HeadlessChrome", "Chrome").replace("Headless", "")
    global BROWSER_UA
    BROWSER_UA = clean
    if "Windows" in clean:
        platform = "Win32"
    elif "Linux" in clean:
        platform = "Linux x86_64"
    else:
        platform = "MacIntel"

    for cmd in ("Emulation.setUserAgentOverride", "Network.setUserAgentOverride"):
        try:
            driver.execute_cdp_cmd(cmd, {
                "userAgent": clean,
                "acceptLanguage": "zh-CN,zh;q=0.9,en;q=0.8",
                "platform": platform,
            })
            log("已覆盖无头 UA 请求头（去掉 Headless 标识）")
            try:
                now = driver.execute_script("return navigator.userAgent;") or ""
                if "Headless" in now:
                    log("警告: UA 覆盖未生效，当前 UA 仍含 Headless，"
                        "登录后接口可能被 WAF 拦截")
            except Exception:
                pass
            return
        except Exception:
            continue


def _current_ua(driver):
    """取当前浏览器的 UA（优先用 CDP 覆盖后的值），避免请求头与浏览器不一致"""
    if BROWSER_UA:
        return BROWSER_UA
    try:
        return driver.execute_script("return navigator.userAgent;") or ""
    except Exception:
        return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _chrome_major(ua):
    m = re.search(r"Chrome/(\d+)", ua or "")
    return m.group(1) if m else "120"


def _ua_metadata(ua, mobile=False):
    """UA-CH 低熵信息：默认从 UA 里取版本号，不写死"""
    major = _chrome_major(ua)
    return {
        "brands": [
            {"brand": "Google Chrome", "version": major},
            {"brand": "Chromium", "version": major},
            {"brand": "Not.A/Brand", "version": "99"},
        ],
        "fullVersion": "%s.0.0.0" % major,
        "platform": "iOS" if mobile else "Windows",
        "platformVersion": "17.2.1" if mobile else "10.0.0",
        "architecture": "",
        "model": "iPhone" if mobile else "",
        "mobile": bool(mobile),
        "bitness": "",
        "wow64": False,
    }


def switch_ua(driver, ua, mobile=False):
    """
    会话级切换 UA：请求头与 navigator.userAgent 同时生效。

    m.jlc.com 按 UA 分端：桌面 UA 会被 302 到 www.jlc.com（PC 站），
    PC 站前端不存 X-JLC-AccessToken，所以取 token 前必须先切成移动端 UA。
    """
    if not ua or not hasattr(driver, "execute_cdp_cmd"):
        return False

    params = {
        "userAgent": ua,
        "acceptLanguage": "zh-CN,zh;q=0.9,en;q=0.8",
        "platform": "iPhone" if mobile else "Win32",
        "userAgentMetadata": _ua_metadata(ua, mobile),
    }
    switched = False
    for cmd in ("Emulation.setUserAgentOverride", "Network.setUserAgentOverride"):
        try:
            driver.execute_cdp_cmd(cmd, params)
            switched = True
            break
        except Exception:
            continue
    if not switched:
        return False

    try:
        if mobile:
            driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                "width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": True,
            })
            driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled",
                                   {"enabled": True, "maxTouchPoints": 5})
        else:
            driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
            driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": False})
    except Exception:
        pass
    return True


_INPAGE_FETCH_JS = """
var cb = arguments[arguments.length - 1];
var url = arguments[0];
var headers = (arguments[1] || {}).headers || {};
fetch(url, {credentials: 'include', headers: headers})
  .then(function (r) {
    return r.text().then(function (t) {
      cb({status: r.status, body: (t || '').slice(0, 300)});
    });
  })
  .catch(function (e) { cb({status: 0, body: String(e)}); });
"""


def inpage_fetch(driver, url, headers=None, timeout=15):
    """用页面自身上下文发请求：自动继承 Cookie / Origin / Referer"""
    try:
        driver.set_script_timeout(timeout)
        result = driver.execute_async_script(
            _INPAGE_FETCH_JS, url, {"headers": headers or {}})
        return result or {}
    except Exception as e:
        return {"status": 0,
                "body": "页面内请求失败: %s" % str(e).splitlines()[0][:120]}


def wait_oshwhub_login(driver, account_index, timeout=15.0):
    """
    等开源平台登录态真正生效。

    SSO 回跳到 oshwhub.com 时 Cookie 还没换好（前端拿到 ticket 后再调一次接口才写
    session Cookie），只看 URL 就取凭证会拿到匿名会话，接着调 /api/users 就是 401。
    这里在页面里轮询 /api/users，200 才算登录完成。
    """
    try:
        if "oshwhub.com" not in (driver.current_url or ""):
            # 不在开源平台页面时，页面内 fetch 是跨域请求（会被 CORS 挡掉），先回站内
            log("账号 %s - 当前不在开源平台页面，先回到 sign_in 再校验" % account_index)
            driver.get("https://oshwhub.com/sign_in")
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(1)
    except Exception as e:
        log("账号 %s - 回到开源平台页面失败: %s" % (
            account_index, str(e).splitlines()[0][:120]))

    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        res = inpage_fetch(driver, "https://oshwhub.com/api/users", timeout=10)
        status = res.get("status")
        if status == 200:
            log("账号 %s - 页面内校验登录态通过（第 %d 次）" % (account_index, attempt))
            return True
        log("账号 %s - 页面内校验登录态: HTTP %s（第 %d 次），继续等" % (
            account_index, status or "无响应", attempt))
        time.sleep(1.2)
    return False


def _cookie_header_from_performance(driver, url_part):
    """从 performance 日志里取浏览器真实发出的 Cookie 请求头"""
    try:
        logs = driver.get_log("performance")
    except Exception:
        return ""

    header = ""
    for entry in logs:
        try:
            message = json.loads(entry["message"]).get("message", {}) or {}
            request = ((message.get("params") or {}).get("request") or {})
            if url_part not in (request.get("url") or ""):
                continue
            for key, value in (request.get("headers") or {}).items():
                if key.lower() == "cookie" and value:
                    header = value
        except Exception:
            continue
    return header


def _cookie_header_from_dict(cookies):
    return "; ".join("%s=%s" % (name, value) for name, value in cookies.items())


def create_driver():
    """
    创建浏览器驱动（兼容 selenium 3.x / 4.x）。

    注意：这里不再加 --blink-settings=imagesEnabled=false。
    阿里云验证码的滑块是背景图渲染的，禁用图片后元素尺寸会变成 0，
    既定位不到滑块，也过不了验证。
    """
    if BROWSER == "edge":
        from selenium.webdriver.edge.options import Options as EdgeOptions
        opts = EdgeOptions()
        driver_class = webdriver.Edge
    else:
        opts = Options()
        driver_class = webdriver.Chrome

    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--user-data-dir=%s" % tempfile.mkdtemp())
    opts.add_argument("--lang=zh-CN")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    # selenium 3.141 的 Edge Options 没有 add_experimental_option，这里做兼容
    for _name, _value in (("excludeSwitches", ["enable-automation"]),
                          ("useAutomationExtension", False)):
        try:
            opts.add_experimental_option(_name, _value)
        except (AttributeError, KeyError):
            pass

    if SELENIUM_MAJOR >= 4:
        opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    def _build():
        if SELENIUM_MAJOR >= 4:
            return driver_class(options=opts)
        if BROWSER == "edge":
            # selenium 3 对 Edge Chromium 支持有限，只传 options
            return driver_class(options=opts)
        caps = DesiredCapabilities.CHROME
        caps['goog:loggingPrefs'] = {'performance': 'ALL'}
        return driver_class(options=opts, desired_capabilities=caps)

    try:
        driver = _build()
    except Exception as e:
        # Chrome 109 以下不认识 --headless=new，退回旧的无头参数再试一次
        if not HEADLESS or "--headless=new" not in (opts.arguments or []):
            raise
        log("--headless=new 启动失败（%s），改用 --headless 重试" % str(e).splitlines()[0][:120])
        opts.arguments.remove("--headless=new")
        opts.add_argument("--headless")
        driver = _build()

    # 先修 UA 请求头：无头模式的 HeadlessChrome 会被 WAF 直接拦掉（418）
    if HEADLESS:
        _apply_ua_override(driver)

    # 再抹掉驱动特征（cdc_* / navigator.webdriver 等），否则阿里云滑块必被判风险
    if stealth:
        stealth.apply(driver)

    # 老版本 selenium 的 Edge 驱动没有 CDP 接口，滑块无法稳定通过，提前提示
    if not hasattr(driver, "execute_cdp_cmd"):
        log("警告: 当前驱动不支持 CDP 鼠标事件，滑块可能无法通过；"
            "建议使用 Chrome（JLC_BROWSER=chrome）并搭配 selenium 4")

    return driver

def format_nickname(nickname):
    """格式化昵称，只显示第一个字和最后一个字，中间用星号代替"""
    if not nickname or len(nickname.strip()) == 0:
        return "未知用户"
    
    nickname = nickname.strip()
    if len(nickname) == 1:
        return f"{nickname}*"
    elif len(nickname) == 2:
        return f"{nickname[0]}*"
    else:
        return f"{nickname[0]}{'*' * (len(nickname)-2)}{nickname[-1]}"

# ==================== 凭证：缓存 / 提取 / 回写 ====================

# 本次运行拿到的凭证，跑完写到这个文件，工作流再回写到 secret
CREDENTIAL_FILE = os.getenv("JLC_CREDENTIAL_FILE", "credentials.json")

credential_cache = {}
credential_loaded = False
credential_dirty = False
# 本次运行里已经真正登录过的账号：最终重试时不再重复登录，避免连续登录触发风控
login_done_accounts = set()


def _now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_credential_cache():
    """
    读取上次运行留下的凭证：优先环境变量 JLC_CREDENTIALS（工作流从 secret 注入），
    其次本地 credentials.json。格式：
        {"version": 1, "accounts": {"用户名": {"cookies": {...}, "access_token": "..."}}}
    没有 accounts 层时，直接当成「用户名 -> 凭证」的映射，方便手工填写。
    """
    global credential_loaded
    if credential_loaded:
        return credential_cache
    credential_loaded = True

    raw = (os.getenv("JLC_CREDENTIALS") or "").strip()
    source = "环境变量 JLC_CREDENTIALS"

    if not raw and CREDENTIAL_FILE and os.path.exists(CREDENTIAL_FILE):
        try:
            with open(CREDENTIAL_FILE, "r", encoding="utf-8") as fp:
                raw = fp.read()
            source = CREDENTIAL_FILE
        except Exception as e:
            log("凭证缓存: 读取 %s 失败: %s" % (CREDENTIAL_FILE, e))
            raw = ""

    if not raw:
        log("凭证缓存: 未找到，本次需要登录")
        return credential_cache

    try:
        data = json.loads(raw)
    except ValueError:
        log("凭证缓存: 内容不是合法 JSON，本次需要登录")
        return credential_cache

    accounts = data.get("accounts") if isinstance(data, dict) and "accounts" in data else data
    if not isinstance(accounts, dict):
        accounts = {}

    for username, cred in accounts.items():
        if isinstance(cred, dict):
            credential_cache[str(username)] = cred

    log("凭证缓存: 已从 %s 载入 %d 个账号" % (source, len(credential_cache)))
    return credential_cache


def get_cached_credentials(username):
    return credential_cache.get(username)


def set_cached_credentials(username, creds):
    global credential_dirty
    credential_cache[username] = creds
    credential_dirty = True


def drop_cached_credentials(username, parts=('points', 'beans')):
    """
    确认失效且没能换新时，把对应部分的旧凭证剔掉：
    - points 失效就清 Cookie，beans 失效就清 token；
    - 两边都没了才整条删除。
    """
    global credential_dirty
    cred = credential_cache.get(username)
    if not cred:
        return
    if 'points' in parts:
        cred['cookies'] = {}
        cred['cookie_header'] = ''
    if 'beans' in parts:
        cred['access_token'] = ''
    if not cred.get('cookies') and not cred.get('cookie_header') and not cred.get('access_token'):
        credential_cache.pop(username, None)
    else:
        credential_cache[username] = cred
    credential_dirty = True


def save_credential_cache(usernames=None):
    """把本次拿到的可用凭证落到 credentials.json，工作流随后回写 secret。"""
    if not credential_dirty:
        log("凭证缓存: 本次没有更新，跳过回写")
        return None

    accounts = credential_cache
    if usernames:
        # 只保留当前配置里的账号，避免换号后旧凭证一直堆在 secret 里
        accounts = {u: credential_cache[u] for u in usernames if u in credential_cache}
        dropped = len(credential_cache) - len(accounts)
        if dropped > 0:
            log("凭证缓存: 清理 %d 个已不在配置里的账号" % dropped)

    payload = {
        "version": 1,
        "updated_at": _now_text(),
        "accounts": accounts,
    }
    try:
        with open(CREDENTIAL_FILE, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
    except Exception as e:
        log("凭证缓存: 写入 %s 失败: %s" % (CREDENTIAL_FILE, e))
        return None

    log("凭证缓存: 已更新 %d 个账号 → %s（工作流会回写到 secret）" % (
        len(accounts), CREDENTIAL_FILE))
    return CREDENTIAL_FILE


def _mask_secret(value, keep=6):
    """日志里不打印完整 token / session"""
    if not value:
        return "空"
    value = str(value)
    if len(value) <= keep:
        return value[:1] + "***"
    return value[:keep] + "***"


def _local_storage_tokens(driver):
    """扫 localStorage 里所有名字带 token 的键（不同站点命名不一样，光靠固定 key 会漏）"""
    try:
        items = driver.execute_script(
            "var out=[];"
            "for (var i=0;i<localStorage.length;i++){"
            "  var k=localStorage.key(i);"
            "  if (/token/i.test(k)) out.push([k, localStorage.getItem(k)]);"
            "}"
            "return out;") or []
    except Exception:
        return []

    tokens = []
    for pair in items:
        try:
            key, value = pair[0], pair[1]
        except (IndexError, TypeError):
            continue
        if value and len(str(value)) >= 16:
            tokens.append((str(key), str(value)))
    return tokens


def _read_token_from_local_storage(driver):
    """从 localStorage 读 X-JLC-AccessToken（各站点 key 大小写略有差异）"""
    for key in ("X-JLC-AccessToken", "x-jlc-accesstoken", "accessToken", "token"):
        try:
            value = driver.execute_script(
                "return window.localStorage.getItem(arguments[0]);", key)
        except Exception:
            continue
        if value:
            log("从 localStorage(%s) 取到 access token: %s" % (key, _mask_secret(value)))
            return value

    for key, value in _local_storage_tokens(driver):
        log("从 localStorage(%s) 取到 access token: %s" % (key, _mask_secret(value)))
        return value
    return None


def _read_token_from_performance(driver):
    """兜底：从已发生的请求头里找 x-jlc-accesstoken"""
    try:
        logs = driver.get_log("performance")
    except Exception:
        return None

    for entry in logs:
        try:
            message = json.loads(entry["message"]).get("message", {})
            headers = (message.get("params") or {}).get("request", {}).get("headers") or {}
            for key in ("x-jlc-accesstoken", "X-JLC-AccessToken", "x-jlc-AccessToken"):
                token = headers.get(key)
                if token:
                    log("从请求头取到 access token: %s" % _mask_secret(token))
                    return token
        except Exception:
            continue
    return None


def fetch_mobile_token(driver, account_index, desktop_ua, timeout=12.0):
    """
    切移动端 UA 进 m.jlc.com 取 X-JLC-AccessToken。

    桌面 UA 访问 m.jlc.com 会被强制重定向到 www.jlc.com（PC 站），
    PC 站前端不落 token，所以无论如何翻 localStorage / 请求头都拿不到。
    """
    log("账号 %s - 切换移动端 UA 访问 m.jlc.com 获取 access token" % account_index)
    if not switch_ua(driver, MOBILE_UA, mobile=True):
        log("账号 %s - 切换移动端 UA 失败（驱动不支持 CDP 覆盖 UA）" % account_index)

    token = ""
    for url in ("https://m.jlc.com/", "https://m.jlc.com/mapp/pages/my/index"):
        try:
            driver.get(url)
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.TAG_NAME, "body")))
        except Exception as e:
            log("账号 %s - 打开 %s 失败: %s" % (
                account_index, url, str(e).splitlines()[0][:120]))
            continue

        deadline = time.time() + timeout
        while time.time() < deadline:
            token = (_read_token_from_local_storage(driver)
                     or _read_token_from_performance(driver))
            if token:
                break
            time.sleep(1)

        if token:
            break
        log("账号 %s - %s 未取到 token，当前地址: %s" % (
            account_index, url, driver.current_url))

    if token:
        check = inpage_fetch(
            driver,
            "https://m.jlc.com/api/appPlatform/center/assets/selectPersonalAssetsInfo",
            headers={"x-jlc-accesstoken": token, "x-jlc-clienttype": "WEB"},
            timeout=10)
        body = (check.get("body") or "")
        log("账号 %s - token 校验: HTTP %s%s" % (
            account_index, check.get("status") or "无响应",
            "，接口可用" if '"success":true' in body.replace(" ", "") else ""))

    # 切回桌面端 UA，避免影响后续页面状态
    if desktop_ua:
        switch_ua(driver, desktop_ua, mobile=False)
    return token


def extract_credentials_from_browser(driver, account_index):
    """
    登录成功后一次性取走凭证，之后全部走 requests。

    1. 先在页面里调 /api/users，确认登录态真的生效（SSO 回跳后 Cookie 是异步写入的）；
    2. 抓浏览器实际发出的 Cookie 头（含 WAF 会话 Cookie），requests 侧原样带过去；
    3. 切移动端 UA 进 m.jlc.com 取 X-JLC-AccessToken。
    """
    if not wait_oshwhub_login(driver, account_index):
        log("账号 %s - 页面内登录态校验未通过，仍尝试提取凭证" % account_index)

    cookies = {}
    try:
        for cookie in driver.get_cookies():
            cookies[cookie["name"]] = cookie["value"]
    except Exception as e:
        log("账号 %s - 读取 Cookie 失败: %s" % (account_index, e))

    cookie_header = _cookie_header_from_performance(driver, "/api/users")
    if cookie_header:
        log("账号 %s - 取到浏览器实际 Cookie 头（%d 字节）" % (
            account_index, len(cookie_header)))
    elif cookies:
        cookie_header = _cookie_header_from_dict(cookies)
        log("账号 %s - 未抓到 Cookie 头，按 get_cookies() 拼了 %d 个" % (
            account_index, len(cookies)))

    desktop_ua = _current_ua(driver)
    token = _read_token_from_local_storage(driver) or _read_token_from_performance(driver)
    if not token:
        token = fetch_mobile_token(driver, account_index, desktop_ua)

    if not token:
        # 个别环境把 token 放在 Cookie 里
        for name, value in cookies.items():
            if name.lower().replace("-", "") in ("xjlcaccesstoken", "jlcaccesstoken"):
                token = value
                log("从 Cookie(%s) 取到 access token: %s" % (name, _mask_secret(value)))
                break

    log("账号 %s - 凭证提取结果: access_token=%s, Cookie=%d 个" % (
        account_index, _mask_secret(token), len(cookies)))

    return {
        "cookies": cookies,
        "cookie_header": cookie_header,
        "access_token": token or "",
        "ua": desktop_ua,
        "updated_at": _now_text(),
    }


PASSPORT_LOGIN_FALLBACK = (
    "https://passport.jlc.com/login?appId=JLC_OSHWHUB"
    "&redirectUrl=https%3A%2F%2Foshwhub.com%2Fsign_in"
)


def _find_login_href(driver):
    """
    在签到页里找一个「可见的」登录链接，返回 href，没有就返回 None。

    坑：页面上通常有两个 a[href*=passport.jlc.com/login]，一个是 0x0 的隐藏占位，
    另一个才是头部真实入口。find_element 会返回 DOM 里靠前的隐藏那个，
    点击直接抛 ElementNotInteractableException（还被 except 吞掉），看起来就像点了没反应。
    所以这里必须按 is_displayed() 挑。
    """
    try:
        for el in driver.find_elements(By.CSS_SELECTOR,
                                       'a[href*="passport.jlc.com/login"]'):
            href = el.get_attribute("href")
            if href and el.is_displayed():
                return href
    except Exception:
        pass
    return None


def _wait_login_entry(driver, timeout=12.0):
    """
    等「进入登录页」或「登录链接出现」，谁先出现就返回谁，省掉固定空等。

    返回值：'done' 表示已经在登录页；字符串 href 表示拿到了登录链接；
    None 表示超时。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = driver.current_url or ""
            if "passport.jlc.com" in url and "login" in url:
                return "done"
            if driver.find_elements(By.CSS_SELECTOR, "input[type=password]"):
                return "done"
        except Exception:
            pass

        href = _find_login_href(driver)
        if href:
            return href
        time.sleep(0.3)
    return None


def _wait_passport_login(driver, timeout=8.0):
    """等待进入 passport 登录页（URL 命中或登录框已渲染）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = driver.current_url or ""
            if "passport.jlc.com" in url and "login" in url:
                return True
            # 前端路由切换时 URL 可能滞后，登录框出现也算到位
            if driver.find_elements(By.CSS_SELECTOR, "input[type=password]"):
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def ensure_login_page(driver, account_index):
    """
    确保进入登录页面；如需重启浏览器则新建并返回，调用方必须使用返回的 driver。

    oshwhub.com/sign_in 现在不会自动跳转到 passport，需要自己找登录入口：
    1) 先等页面自动跳转；
    2) 找不到就取「可见」的登录链接，直接 get 过去（比点击稳，不受遮挡影响）；
    3) 再不行用默认的 passport 登录地址兜底。
    """
    max_restarts = 3
    restarts = 0

    while True:
        try:
            driver.get("https://oshwhub.com/sign_in")
            log(f"账号 {account_index} - 已打开 JLC 签到页")

            entry = _wait_login_entry(driver, timeout=12)
            if entry == "done":
                log(f"账号 {account_index} - 检测到未登录状态")
                return True, driver

            if entry:
                log(f"账号 {account_index} - 页面未自动跳转，打开登录入口")
                driver.get(entry)
            else:
                log(f"账号 {account_index} - 页面未自动跳转，使用默认登录地址")
                driver.get(PASSPORT_LOGIN_FALLBACK)

            if _wait_passport_login(driver, timeout=12):
                log(f"账号 {account_index} - 检测到未登录状态")
                return True, driver

            log(f"账号 {account_index} - 仍未进入登录页（上次 URL: {driver.current_url[:80]}）")

        except Exception as e:
            log(f"账号 {account_index} - 打开登录页异常: {str(e).splitlines()[0][:120]}")

        restarts += 1
        if restarts >= max_restarts:
            log(f"账号 {account_index} - 重启浏览器{max_restarts}次后仍无法进入登录页面")
            return False, driver

        try:
            driver.quit()
        except Exception:
            pass
        driver = create_driver()
        time.sleep(2)


def check_password_error(driver, account_index, timeout=2.5):
    """
    检查页面是否出现密码错误提示。

    原来是对 8 个 XPath 逐个 WebDriverWait(2s)，命中前要白等十几秒（多账号时很致命）。
    这里改成直接读 body 文本轮询，通常几十毫秒就能判定。
    """
    keywords = ('账号或密码不正确', '用户名或密码错误', '密码错误',
                '账号或密码错误', '登录失败',
                # 风控限流：继续重试只会越试越久，直接判定这个账号本次不可用
                '次数过多', '分钟后再试', '登录异常')
    deadline = time.time() + timeout
    while True:
        try:
            page_text = driver.execute_script(
                "var b = document.body; return b ? b.innerText.slice(0, 4000) : '';") or ''
        except Exception as e:
            log(f"账号 {account_index} - 检查密码错误时出现异常: {e}")
            return False

        for kw in keywords:
            if kw in page_text:
                log(f"账号 {account_index} - 检测到登录被拒（{kw}），跳过此账号")
                return True

        if time.time() >= deadline:
            return False
        time.sleep(0.25)

def _empty_account_result(account_index, retry_count=0, is_final_retry=False):
    return {
        'account_index': account_index,
        'nickname': '未知',
        'customer_code': '',
        'credential_source': '无',
        'oshwhub_status': '未知',
        'oshwhub_success': False,
        'oshwhub_message': '',
        'initial_points': 0,
        'final_points': 0,
        'points_reward': 0,
        'jindou_status': '未知',
        'jindou_success': False,
        'jindou_message': '',
        'initial_jindou': 0,
        'final_jindou': 0,
        'jindou_reward': 0,
        'has_jindou_reward': False,
        'retry_count': retry_count,
        'is_final_retry': is_final_retry,
        'password_error': False,
    }


def login_for_credentials(username, password, account_index):
    """
    浏览器只负责登录，登录成功立刻把 Cookie / access token 取走并关掉浏览器。

    返回 (creds, error)：
    - 成功：creds 为 extract_credentials_from_browser 的结果，error 为 None；
    - 失败：creds 为 None，error 形如 {'status': '密码错误', 'password_error': True}
    """
    log("账号 %s - 开始浏览器登录（仅用于获取凭证）" % account_index)
    driver = None

    def fail(status, password_error=False):
        return None, {'status': status, 'password_error': password_error}

    try:
        driver = create_driver()

        # 1. 确保进入登录页面（必要时会重启浏览器，返回新的 driver）
        login_ready, driver = ensure_login_page(driver, account_index)
        if not login_ready:
            return fail('无法进入登录页')

        wait = WebDriverWait(driver, 25)

        # 2. 输入账号密码
        try:
            phone_btn = wait.until(
                EC.element_to_be_clickable((By.XPATH, '//button[contains(text(),"账号登录")]'))
            )
            phone_btn.click()
            log("账号 %s - 已切换账号登录" % account_index)
            WebDriverWait(driver, 10).until(EC.presence_of_element_located(
                (By.XPATH, '//input[@placeholder="请输入手机号码 / 客户编号 / 邮箱"]')))
        except Exception as e:
            log("账号 %s - 账号登录按钮可能已默认选中: %s" % (account_index, e))

        try:
            user_input = wait.until(EC.presence_of_element_located(
                (By.XPATH, '//input[@placeholder="请输入手机号码 / 客户编号 / 邮箱"]')))
            user_input.clear()
            user_input.send_keys(username)

            pwd_input = wait.until(
                EC.presence_of_element_located((By.XPATH, '//input[@type="password"]')))
            pwd_input.clear()
            pwd_input.send_keys(password)
            log("账号 %s - 已输入账号密码" % account_index)
        except Exception as e:
            log("账号 %s - 登录输入框未找到: %s" % (account_index, e))
            return fail('登录失败')

        try:
            login_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button.submit")))
            login_btn.click()
            log("账号 %s - 已点击登录按钮" % account_index)
        except Exception as e:
            log("账号 %s - 登录按钮定位失败: %s" % (account_index, e))
            return fail('登录失败')

        # 3. 密码错误 / 风控限流
        time.sleep(1)
        if check_password_error(driver, account_index):
            return fail('密码错误', password_error=True)

        # 4. 安全验证：现网是阿里云验证码 2.0（aliyunCaptcha 滑块），旧版易盾作兜底
        captcha_kind = None
        if captcha_solver:
            try:
                captcha_kind, _ = captcha_solver.wait_for_captcha(driver, timeout=12)
            except Exception as e:
                log("账号 %s - 检测安全验证时出错: %s" % (account_index, e))

        if captcha_kind:
            captcha_label = "阿里云" if captcha_kind == "aliyun" else "易盾"
            log("账号 %s - 检测到%s滑块验证，开始处理..." % (account_index, captcha_label))

            def _captcha_log(msg, _idx=account_index):
                log("账号 %s %s" % (_idx, msg))

            try:
                captcha_result = captcha_solver.solve_captcha(
                    driver, log=_captcha_log, kind=captcha_kind)
            except Exception as e:
                log("账号 %s - 滑块处理异常: %s" % (account_index, e))
                captcha_result = "failed"

            if captcha_result != "passed":
                log("账号 %s - 滑块验证未通过" % account_index)
                time.sleep(1)
                if check_password_error(driver, account_index):
                    return fail('密码错误', password_error=True)
                return fail('滑块验证失败')

            log("账号 %s - 滑块验证通过" % account_index)
        else:
            log("账号 %s - 未检测到滑块验证，继续等待登录跳转" % account_index)

        # 验证通过后立刻再查一次密码错误
        time.sleep(1)
        if check_password_error(driver, account_index):
            return fail('密码错误', password_error=True)

        # 5. 等待跳转回签到页
        log("账号 %s - 等待登录跳转..." % account_index)
        jumped = False
        for _ in range(15):
            current_url = driver.current_url
            if "oshwhub.com" in current_url and "passport.jlc.com" not in current_url:
                log("账号 %s - 成功跳转回签到页面" % account_index)
                jumped = True
                break
            time.sleep(1)

        if not jumped:
            log("账号 %s - 跳转超时，当前页面标题: %s" % (account_index, driver.title))
            return fail('跳转失败')

        # 6. 取凭证
        creds = extract_credentials_from_browser(driver, account_index)
        if not creds.get('cookies') and not creds.get('access_token'):
            return fail('凭证提取失败')
        return creds, None

    except Exception as e:
        log("账号 %s - 登录流程异常: %s" % (account_index, str(e).splitlines()[0][:200]))
        return fail('执行异常')
    finally:
        if driver is not None:
            try:
                driver.quit()
                log("账号 %s - 浏览器已关闭（凭证已取走，后续走接口）" % account_index)
            except Exception:
                pass


def _apply_points_result(result, res):
    result['oshwhub_status'] = res.get('status') or '未知'
    result['oshwhub_success'] = bool(res.get('success'))
    result['oshwhub_message'] = res.get('message') or ''
    result['initial_points'] = res.get('initial_points') or 0
    result['final_points'] = res.get('final_points') or 0
    result['points_reward'] = res.get('points_reward') or 0
    if res.get('nickname'):
        result['nickname'] = format_nickname(res['nickname'])
    if res.get('customer_code'):
        result['customer_code'] = res['customer_code']


def _apply_beans_result(result, res):
    result['jindou_status'] = res.get('status') or '未知'
    result['jindou_success'] = bool(res.get('success'))
    result['jindou_message'] = res.get('message') or ''
    result['initial_jindou'] = res.get('initial_jindou') or 0
    result['final_jindou'] = res.get('final_jindou') or 0
    result['jindou_reward'] = res.get('jindou_reward') or 0
    result['has_jindou_reward'] = bool(res.get('has_extra_reward'))
    if result.get('nickname') in (None, '', '未知') and res.get('customer_code'):
        result['nickname'] = mask_account(res['customer_code'])


def sign_with_credentials(creds, account_index, result, only=None):
    """
    用凭证走接口签到（浏览器不参与）。

    only 指定本次要跑的部分（默认积分+金豆都跑）。
    返回需要重新登录才能修复的部分列表，例如 ['points'] 表示 Cookie 失效。
    """
    only = only or ('points', 'beans')
    creds = creds or {}
    expired = []

    # ---- 开源平台积分 ----
    if 'points' in only:
        cookies = creds.get('cookies') or {}
        cookie_header = creds.get('cookie_header') or ''
        if not cookies and not cookie_header:
            log("账号 %s - 没有开源平台 Cookie，积分需要重新登录" % account_index)
            result['oshwhub_status'] = '凭证缺失'
            result['oshwhub_message'] = '没有 oshwhub Cookie'
            expired.append('points')
        else:
            log("账号 %s - 请求积分签到接口（Cookie 方式）..." % account_index)
            # 先按浏览器原样发（带全 WAF 会话 Cookie）；被拒再退回只用会话 Cookie，
            # 因为缓存凭证里的 WAF Cookie 可能属于上一台机器/上一个时段。
            header_options = [cookie_header, ''] if cookie_header else ['']
            done = False
            last_error = None
            for header in header_options:
                try:
                    res = OshwhubClient(
                        cookies,
                        index=account_index,
                        logger=log,
                        cookie_header=header,
                        ua=creds.get('ua') or '',
                    ).run()
                    _apply_points_result(result, res)
                    log("账号 %s - 积分签到: %s，积分 %s → %s (%+d)" % (
                        account_index, result['oshwhub_status'],
                        result['initial_points'], result['final_points'],
                        result['points_reward']))
                    done = True
                    break
                except AuthExpired as e:
                    last_error = e
                    if header:
                        log("账号 %s - 完整 Cookie 头被拒（%s），改用会话 Cookie 再试一次" % (
                            account_index, e))
                        continue
                    result['oshwhub_status'] = '凭证失效'
                    result['oshwhub_message'] = str(e)
                    expired.append('points')
                    log("账号 %s - 积分凭证失效: %s" % (account_index, e))
                    done = True
                    break
                except ApiError as e:
                    result['oshwhub_status'] = '签到失败'
                    result['oshwhub_message'] = str(e)
                    log("账号 %s - 积分签到失败: %s" % (account_index, e))
                    done = True
                    break
                except Exception as e:
                    result['oshwhub_status'] = '签到异常'
                    result['oshwhub_message'] = '%s: %s' % (type(e).__name__, e)
                    log("账号 %s - 积分签到异常: %s" % (account_index, e))
                    done = True
                    break
            if not done and last_error is not None:
                result['oshwhub_status'] = '凭证失效'
                result['oshwhub_message'] = str(last_error)
                expired.append('points')

    # ---- 嘉立创金豆 ----
    if 'beans' in only:
        token = creds.get('access_token') or ''
        if not token:
            log("账号 %s - 没有金豆 access token，需要重新登录" % account_index)
            result['jindou_status'] = '凭证缺失'
            result['jindou_message'] = '没有 X-JLC-AccessToken'
            expired.append('beans')
        else:
            log("账号 %s - 请求金豆签到接口（Token 方式）..." % account_index)
            try:
                res = JlcBeanClient(
                    token,
                    index=account_index,
                    logger=log,
                    secretkey=creds.get('secretkey') or '',
                ).run()
                _apply_beans_result(result, res)
                log("账号 %s - 金豆签到: %s，金豆 %s → %s (%+d)" % (
                    account_index, result['jindou_status'],
                    result['initial_jindou'], result['final_jindou'],
                    result['jindou_reward']))
            except AuthExpired as e:
                result['jindou_status'] = '凭证失效'
                result['jindou_message'] = str(e)
                expired.append('beans')
                log("账号 %s - 金豆 Token 失效: %s" % (account_index, e))
            except ApiError as e:
                result['jindou_status'] = '签到失败'
                result['jindou_message'] = str(e)
                log("账号 %s - 金豆签到失败: %s" % (account_index, e))
            except Exception as e:
                result['jindou_status'] = '签到异常'
                result['jindou_message'] = '%s: %s' % (type(e).__name__, e)
                log("账号 %s - 金豆签到异常: %s" % (account_index, e))

    return expired


def sign_in_account(username, password, account_index, total_accounts,
                    retry_count=0, is_final_retry=False):
    """
    单个账号：优先用缓存凭证走接口；凭证缺失/失效才启动浏览器登录，登录后只取凭证。
    """
    retry_label = ""
    if retry_count > 0:
        retry_label = " (重试%s)" % retry_count
    if is_final_retry:
        retry_label = " (最终重试)"
    log("开始处理账号 %s/%s%s" % (account_index, total_accounts, retry_label))

    result = _empty_account_result(account_index, retry_count, is_final_retry)

    # 1. 先看上次留下的凭证
    creds = get_cached_credentials(username)

    if creds:
        updated = creds.get('updated_at') or '时间未知'
        log("账号 %s - 使用缓存凭证（更新于 %s），先验证" % (account_index, updated))
        result['credential_source'] = '缓存'
        sign_with_credentials(creds, account_index, result)

        if result['oshwhub_success'] and result['jindou_success']:
            log("账号 %s - 缓存凭证可用，未启动浏览器" % account_index)
            return result

        # 任何一边没签成功都重新登录（凭证可能只是部分失效，或接口需要新 Cookie）
        need = []
        if not result['oshwhub_success']:
            need.append('points')
        if not result['jindou_success']:
            need.append('beans')
        log("账号 %s - 缓存凭证签到未全部成功（%s），登录后只重试未成功的部分" % (
            account_index, '、'.join(need)))
    else:
        log("账号 %s - 没有缓存凭证，走登录流程" % account_index)
        need = None

    # 2. 登录拿新凭证（只重跑未成功的部分，避免重复签到）
    if is_final_retry and creds and username in login_done_accounts:
        log("账号 %s - 本次运行已登录过，最终重试不再重复登录，直接用现有凭证重试" % (
            account_index))
        sign_with_credentials(creds, account_index, result, only=need or None)
        return result

    new_creds, error = login_for_credentials(username, password, account_index)

    if error:
        status = error.get('status') or '登录失败'
        log("账号 %s - 登录失败: %s" % (account_index, status))
        if creds:
            drop_cached_credentials(username, need or ('points', 'beans'))
            log("账号 %s - 已丢弃失效的缓存凭证部分: %s" % (
                account_index, '、'.join(need or ('points', 'beans'))))
        if not result['oshwhub_success']:
            result['oshwhub_status'] = status
            result['oshwhub_message'] = status
        if not result['jindou_success']:
            result['jindou_status'] = status
            result['jindou_message'] = status
        result['password_error'] = bool(error.get('password_error'))
        return result

    set_cached_credentials(username, new_creds)
    login_done_accounts.add(username)
    result['credential_source'] = '登录'
    log("账号 %s - 登录成功，凭证已缓存" % account_index)

    still_expired = sign_with_credentials(new_creds, account_index, result, only=need)
    if still_expired:
        log("账号 %s - 新凭证仍无法通过校验: %s" % (
            account_index, '、'.join(still_expired)))
    return result


def _merge_results(best, new):
    """合并两次尝试：积分/金豆各自成功过就保留成功结果，失败的部分用最新状态"""
    merged = dict(best)

    if new.get('oshwhub_success') and not merged.get('oshwhub_success'):
        for key in ('oshwhub_success', 'oshwhub_status', 'oshwhub_message',
                    'initial_points', 'final_points', 'points_reward'):
            merged[key] = new.get(key)
    if new.get('jindou_success') and not merged.get('jindou_success'):
        for key in ('jindou_success', 'jindou_status', 'jindou_message',
                    'initial_jindou', 'final_jindou', 'jindou_reward',
                    'has_jindou_reward'):
            merged[key] = new.get(key)

    if not merged.get('oshwhub_success'):
        merged['oshwhub_status'] = new.get('oshwhub_status', merged.get('oshwhub_status'))
        merged['oshwhub_message'] = new.get('oshwhub_message', merged.get('oshwhub_message'))
    if not merged.get('jindou_success'):
        merged['jindou_status'] = new.get('jindou_status', merged.get('jindou_status'))
        merged['jindou_message'] = new.get('jindou_message', merged.get('jindou_message'))

    for key in ('nickname', 'customer_code', 'credential_source'):
        if merged.get(key) in (None, '', '未知') and new.get(key) not in (None, '', '未知'):
            merged[key] = new.get(key)

    merged['retry_count'] = new.get('retry_count', merged.get('retry_count', 0))
    merged['is_final_retry'] = new.get('is_final_retry', merged.get('is_final_retry', False))
    merged['password_error'] = new.get('password_error', merged.get('password_error', False))
    return merged


def process_single_account(username, password, account_index, total_accounts):
    """处理单个账号；失败的账号留到最后的「最终重试」再跑一次，避免连续登录触发风控"""
    max_retries = 0
    best = None

    for attempt in range(max_retries + 1):
        result = sign_in_account(
            username, password, account_index, total_accounts, retry_count=attempt)

        if result.get('password_error'):
            return result

        best = result if best is None else _merge_results(best, result)

        if best['oshwhub_success'] and best['jindou_success']:
            break
        if attempt < max_retries:
            wait_time = random.randint(2, 5)
            log("账号 %s - 准备第 %s 次重试，等待 %s 秒..." % (
                account_index, attempt + 1, wait_time))
            time.sleep(wait_time)

    return best


def execute_final_retry_for_failed_accounts(all_results, usernames, passwords, total_accounts):
    """对仍然失败的账号做最后一次重试（排除密码错误）"""
    log("=" * 70)
    log("执行最终重试 - 处理仍失败的账号")
    log("=" * 70)

    failed = [r for r in all_results
              if (not r['oshwhub_success'] or not r['jindou_success'])
              and not r.get('password_error', False)]

    if not failed:
        log("没有需要最终重试的账号")
        return all_results

    log("需要最终重试的账号: %s" % ', '.join(str(r['account_index']) for r in failed))
    time.sleep(random.randint(2, 3))

    for pos, original in enumerate(failed):
        account_index = original['account_index']
        log("开始最终重试账号 %s" % account_index)

        final_result = sign_in_account(
            usernames[account_index - 1],
            passwords[account_index - 1],
            account_index,
            total_accounts,
            retry_count=original.get('retry_count', 0) + 1,
            is_final_retry=True,
        )

        if final_result.get('password_error'):
            original['password_error'] = True
            original['oshwhub_status'] = '密码错误'
            original['jindou_status'] = '密码错误'
            original['nickname'] = '未知'
            original['is_final_retry'] = True
            log("账号 %s - 最终重试检测到密码错误" % account_index)
        else:
            original.update(_merge_results(original, final_result))
            original['is_final_retry'] = True
            log("账号 %s - 最终重试: 积分=%s, 金豆=%s" % (
                account_index, original['oshwhub_status'], original['jindou_status']))

        if pos < len(failed) - 1:
            wait_time = random.randint(2, 4)
            log("等待 %s 秒后处理下一个重试账号..." % wait_time)
            time.sleep(wait_time)

    log("最终重试完成")
    return all_results

# 推送函数
SERVERCHAN3_TAGS = "嘉立创|签到"


def parse_serverchan3_keys(total_accounts):
    """
    解析 Server酱3 的 sendkey 配置，返回 {账号序号: sendkey}。

    SERVERCHAN3_SCKEY 支持用逗号（或换行/分号）分隔多个 key，按账号顺序一一对应：
        SERVERCHAN3_SCKEY=sctp111tAAA,sctp222tBBB
    - 账号 1 用第 1 个，账号 2 用第 2 个，以此类推；
    - 某个账号没配（数量不够或该位置留空）就回落到第一个 key；
    - 完全没配置则返回空 dict，调用方跳过推送。
    """
    raw = os.getenv('SERVERCHAN3_SCKEY', '') or ''
    parts = [p.strip() for p in re.split(r'[,\n;]+', raw)]

    first = next((p for p in parts if p), None)
    if not first:
        return {}

    mapping = {}
    for index in range(1, total_accounts + 1):
        key = parts[index - 1] if index - 1 < len(parts) else ''
        mapping[index] = key or first
    return mapping


def _mask_key(sendkey):
    """日志里不打印完整 sendkey"""
    if len(sendkey) <= 10:
        return sendkey[:2] + "***"
    return sendkey[:8] + "***" + sendkey[-4:]


def _serverchan3_endpoint(sendkey):
    """
    Server酱3 的发送地址是 https://{uid}.push.ft07.com/send/{sendkey}.send，
    uid 可以直接从 sendkey 里取（sctp{uid}t...）。
    """
    match = re.match(r'^sctp(\d+)t', sendkey)
    if not match:
        return None
    return "https://{}.push.ft07.com/send/{}.send".format(match.group(1), sendkey)


def mask_account(account):
    """
    账号打码：前后各保留 3 位，中间固定 4 个星号（少留一位容易撞车）。
    12345678A -> 123****78A，abcde@qq.com -> abc****@qq.com
    """
    text = (account or '').strip()
    if not text:
        return "未知账号"
    if '@' in text:
        local, _, domain = text.partition('@')
        head = local[:3] if len(local) > 3 else local[:1]
        return "{}****@{}".format(head, domain)
    if len(text) <= 3:
        return "{}****".format(text[:1])
    if len(text) <= 6:
        return "{}****{}".format(text[:1], text[-1:])
    return "{}****{}".format(text[:3], text[-3:])


def _fmt_reward(value):
    """金豆数量去掉多余的 .0：2.0 -> 2，2.5 -> 2.5"""
    try:
        return "{:g}".format(float(value))
    except (TypeError, ValueError):
        return str(value)


def _short_reason(text, limit=40):
    text = (text or '').replace('\n', ' ').strip()
    return text[:limit] + ('…' if len(text) > limit else '')


def _points_text(result):
    """积分部分的一句话描述"""
    status = result.get('oshwhub_status') or '未知'
    initial = result.get('initial_points') or 0
    final = result.get('final_points') or 0
    reward = result.get('points_reward') or 0

    if status == '签到成功':
        return "积分 +{}（{}→{}）".format(
            _fmt_reward(reward), _fmt_reward(initial), _fmt_reward(final))
    if status == '已签到过':
        return "积分今日已签到（{}）".format(_fmt_reward(final))
    return "积分签到失败（{}）".format(
        _short_reason(result.get('oshwhub_message') or status))


def _beans_text(result):
    """金豆部分的一句话描述"""
    status = result.get('jindou_status') or '未知'
    initial = result.get('initial_jindou') or 0
    final = result.get('final_jindou') or 0
    reward = result.get('jindou_reward') or 0

    if status == '签到成功':
        text = "金豆 +{}（{}→{}）".format(
            _fmt_reward(reward), _fmt_reward(initial), _fmt_reward(final))
        if result.get('has_jindou_reward'):
            text += "，含额外奖励"
        return text
    if status == '已签到过':
        return "金豆今日已签到（{}）".format(_fmt_reward(final))
    return "金豆签到失败（{}）".format(
        _short_reason(result.get('jindou_message') or status))


def _account_line(account, result):
    """返回 (类别, 一行描述)；类别为 ok / already / failed"""
    if result.get('password_error'):
        return 'failed', "❌ 账号({})：账号或密码错误".format(account)

    body = "{}；{}".format(_points_text(result), _beans_text(result))

    if not result.get('oshwhub_success') or not result.get('jindou_success'):
        return 'failed', "❌ 账号({})：{}".format(account, body)

    if (result.get('oshwhub_status') == '签到成功'
            or result.get('jindou_status') == '签到成功'):
        return 'ok', "✅ 账号({})：{}".format(account, body)
    return 'already', "✅ 账号({})：{}".format(account, body)


def _build_serverchan3_desp(group_results, usernames):
    """
    生成推送正文（Server酱3 走 markdown，单换行会被拼在一起，所以行间用双换行）：

        签到统计
        成功：4
        已签到：0
        失败：0
        【签到成功】
        ✅ 账号(123****78A)：积分 +5（140→145）；金豆 +2（8→10）
        ...
    """
    ok = already = failed = 0
    details = []

    for result in group_results:
        index = result.get('account_index')
        account = ""
        if usernames and isinstance(index, int) and 0 <= index - 1 < len(usernames):
            account = mask_account(usernames[index - 1])

        kind, line = _account_line(account, result)
        if kind == 'ok':
            ok += 1
        elif kind == 'already':
            already += 1
        else:
            failed += 1
        details.append(line)

    lines = [
        "签到统计",
        "成功：{}".format(ok),
        "已签到：{}".format(already),
        "失败：{}".format(failed),
        "【签到成功】" if failed == 0 else "【签到失败】",
    ]
    lines.extend(details)
    return "\n\n".join(lines), failed


def push_serverchan3(all_results, total_accounts, usernames=None):
    """按 sendkey 分组推送；同一个 sendkey 的账号合并成一条消息"""
    mapping = parse_serverchan3_keys(total_accounts)
    if not mapping:
        log("Server酱3-未配置 SERVERCHAN3_SCKEY，跳过推送")
        return

    result_map = {r['account_index']: r for r in all_results}

    # 保持账号顺序分组，避免同一个 key 多次推送
    groups = {}
    order = []
    for index in sorted(mapping):
        key = mapping[index]
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(index)

    for key in order:
        indexes = groups[key]
        endpoint = _serverchan3_endpoint(key)
        if not endpoint:
            log("Server酱3-第 {} 个 sendkey 格式不正确（应以 sctp 开头），跳过".format(
                order.index(key) + 1))
            continue

        group_results = [result_map[i] for i in indexes if i in result_map]
        desp, failed = _build_serverchan3_desp(group_results, usernames)
        title = "嘉立创签到失败" if failed else "嘉立创签到成功"

        try:
            response = requests.post(
                endpoint,
                data={"title": title, "desp": desp, "tags": SERVERCHAN3_TAGS},
                timeout=20,
            )
            try:
                payload = response.json()
            except ValueError:
                payload = {}

            if payload.get("code") == 0:
                log("Server酱3-已推送「{}」（{}，合并 {} 个账号）".format(
                    title, _mask_key(key), len(indexes)))
            else:
                reason = payload.get("message") or payload.get("error") or response.text[:120]
                log("Server酱3-推送失败「{}」（{}，合并 {} 个账号）: {}".format(
                    title, _mask_key(key), len(indexes), reason))
        except Exception as e:
            log("Server酱3-推送异常（{}）: {}".format(_mask_key(key), e))


def push_summary(all_results=None, total_accounts=0, usernames=None):
    if not summary_logs:
        return
    
    title = "嘉立创签到总结"
    text = "\n".join(summary_logs)
    full_text = f"{title}\n{text}"  # 有些平台不需要单独标题
    
    # Telegram
    telegram_bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if telegram_bot_token and telegram_chat_id:
        try:
            url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
            params = {'chat_id': telegram_chat_id, 'text': full_text}
            response = requests.get(url, params=params)
            if response.status_code == 200:
                log("Telegram-日志已推送")
        except:
            pass  # 静默失败

    # 企业微信 (WeChat Work)
    wechat_webhook_key = os.getenv('WECHAT_WEBHOOK_KEY')
    if wechat_webhook_key:
        try:
            if wechat_webhook_key.startswith('https://'):
                url = wechat_webhook_key
            else:
                url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={wechat_webhook_key}"
            body = {"msgtype": "text", "text": {"content": full_text}}
            response = requests.post(url, json=body)
            if response.status_code == 200:
                log("企业微信-日志已推送")
        except:
            pass

    # 钉钉 (DingTalk)
    dingtalk_webhook = os.getenv('DINGTALK_WEBHOOK')
    if dingtalk_webhook:
        try:
            if dingtalk_webhook.startswith('https://'):
                url = dingtalk_webhook
            else:
                url = f"https://oapi.dingtalk.com/robot/send?access_token={dingtalk_webhook}"
            body = {"msgtype": "text", "text": {"content": full_text}}
            response = requests.post(url, json=body)
            if response.status_code == 200:
                log("钉钉-日志已推送")
        except:
            pass

    # PushPlus
    pushplus_token = os.getenv('PUSHPLUS_TOKEN')
    if pushplus_token:
        try:
            url = "http://www.pushplus.plus/send"
            body = {"token": pushplus_token, "title": title, "content": text}
            response = requests.post(url, json=body)
            if response.status_code == 200:
                log("PushPlus-日志已推送")
        except:
            pass

    # Server酱
    serverchan_sckey = os.getenv('SERVERCHAN_SCKEY')
    if serverchan_sckey:
        try:
            url = f"https://sctapi.ftqq.com/{serverchan_sckey}.send"
            body = {"title": title, "desp": text}
            response = requests.post(url, data=body)
            if response.status_code == 200:
                log("Server酱-日志已推送")
        except:
            pass

    # Server酱3（同一 sendkey 的账号合并为一条推送）
    if all_results is not None:
        push_serverchan3(all_results, total_accounts, usernames)

    # 酷推 (CoolPush)
    coolpush_skey = os.getenv('COOLPUSH_SKEY')
    if coolpush_skey:
        try:
            url = f"https://push.xuthus.cc/send/{coolpush_skey}?c={full_text}"
            response = requests.get(url)
            if response.status_code == 200:
                log("酷推-日志已推送")
        except:
            pass

    # 自定义API
    custom_webhook = os.getenv('CUSTOM_WEBHOOK')
    if custom_webhook:
        try:
            body = {"title": title, "content": text}
            response = requests.post(custom_webhook, json=body)
            if response.status_code == 200:
                log("自定义API-日志已推送")
        except:
            pass

def main():
    global in_summary
    
    def _split_env(name):
        raw = os.getenv(name, "") or ""
        return [x.strip() for x in re.split(r"[,\n;]+", raw) if x.strip()]

    if len(sys.argv) >= 3:
        usernames = [u.strip() for u in sys.argv[1].split(',') if u.strip()]
        passwords = [p.strip() for p in sys.argv[2].split(',') if p.strip()]
        failure_flag = sys.argv[3] if len(sys.argv) >= 4 else ''
    else:
        # 没传命令行参数时读环境变量，本地调试不必把密码写进命令历史
        usernames = _split_env("JLC_USERNAME")
        passwords = _split_env("JLC_PASSWORD")
        failure_flag = os.getenv("JLC_FAIL_EXIT", "")
        if not usernames or not passwords:
            print("用法: python jlc.py 账号1,账号2,账号3... 密码1,密码2,密码3... [失败退出标志]")
            print("示例: python jlc.py user1,user2,user3 pwd1,pwd2,pwd3")
            print("示例: python jlc.py user1,user2,user3 pwd1,pwd2,pwd3 true")
            print("或改用环境变量: JLC_USERNAME=账号1,账号2  JLC_PASSWORD=密码1,密码2  JLC_FAIL_EXIT=true")
            print("失败退出标志: 不传或任意值-关闭, true-开启(任意账号签到失败时返回非零退出码)")
            sys.exit(1)
        log("已从环境变量 JLC_USERNAME / JLC_PASSWORD 读取账号")

    # 解析失败退出标志，默认为关闭
    enable_failure_exit = (failure_flag or '').lower() == 'true'
    
    log(f"失败退出功能: {'开启' if enable_failure_exit else '关闭'}")
    
    if len(usernames) != len(passwords):
        log("错误: 账号和密码数量不匹配!")
        sys.exit(1)
    
    total_accounts = len(usernames)
    log(f"开始处理 {total_accounts} 个账号的签到任务")

    # 载入上次留下的凭证（工作流从 JLC_CREDENTIALS secret 注入）
    load_credential_cache()
    
    # 存储所有账号的结果
    all_results = []
    
    for i, (username, password) in enumerate(zip(usernames, passwords), 1):
        log(f"开始处理第 {i} 个账号")
        result = process_single_account(username, password, i, total_accounts)
        all_results.append(result)
        
        if i < total_accounts:
            wait_time = random.randint(3, 5)
            log(f"等待 {wait_time} 秒后处理下一个账号...")
            time.sleep(wait_time)
    
    # 检查是否有失败的账号，执行最终重试（排除密码错误的）
    has_failed_accounts = any((not result['oshwhub_success'] or not result['jindou_success']) and not result.get('password_error', False) for result in all_results)
    
    if has_failed_accounts:
        all_results = execute_final_retry_for_failed_accounts(all_results, usernames, passwords, total_accounts)
    
    # 输出详细总结
    log("=" * 70)
    in_summary = True  # 启用总结收集
    log("详细签到任务完成总结")
    log("=" * 70)
    
    oshwhub_success_count = 0
    jindou_success_count = 0
    total_points_reward = 0
    total_jindou_reward = 0
    retried_accounts = []  # 合并所有重试过的账号，包括最终重试
    password_error_accounts = []  # 密码错误的账号
    
    # 记录失败的账号
    failed_accounts = []
    
    for result in all_results:
        account_index = result['account_index']
        nickname = result.get('nickname', '未知')
        retry_count = result.get('retry_count', 0)
        is_final_retry = result.get('is_final_retry', False)
        password_error = result.get('password_error', False)
        
        if password_error:
            password_error_accounts.append(account_index)
        
        if retry_count > 0 or is_final_retry:
            retried_accounts.append(account_index)
        
        # 检查是否有失败情况（排除密码错误）
        if (not result['oshwhub_success'] or not result['jindou_success']) and not password_error:
            failed_accounts.append(account_index)
        
        retry_label = ""
        if retry_count > 0:
             retry_label = f" [重试{retry_count}次]"
        elif is_final_retry:
            retry_label = " [最终重试]"
        
        # 密码错误账号的特殊显示
        if password_error:
            log(f"账号 {account_index} (未知) 详细结果: [密码错误]")
            log("  └── 状态: 账号或密码错误，跳过此账号")
        else:
            log(f"账号 {account_index} ({nickname}) 详细结果:{retry_label}")
            log(f"  ├── 开源平台: {result['oshwhub_status']}")
            
            # 显示积分变化
            if result['points_reward'] > 0:
                log(f"  ├── 积分变化: {result['initial_points']} → {result['final_points']} (+{result['points_reward']})")
                total_points_reward += result['points_reward']
            elif result['oshwhub_success']:
                log(f"  ├── 积分变化: {result['initial_points']} → {result['final_points']} (0)")
            else:
                log("  ├── 积分状态: 无法获取积分信息")
            
            log(f"  ├── 金豆签到: {result['jindou_status']}")
            
            # 显示金豆变化
            if result['jindou_reward'] > 0:
                jindou_text = f"  ├── 金豆变化: {result['initial_jindou']} → {result['final_jindou']} (+{result['jindou_reward']})"
                if result['has_jindou_reward']:
                    jindou_text += "（有奖励）"
                log(jindou_text)
                total_jindou_reward += result['jindou_reward']
            elif result['jindou_success']:
                log(f"  ├── 金豆变化: {result['initial_jindou']} → {result['final_jindou']} (0)")
            else:
                log("  ├── 金豆状态: 无法获取金豆信息")
            
            log(f"  ├── 凭证来源: {result.get('credential_source', '未知')}")
            
            if result['oshwhub_success']:
                oshwhub_success_count += 1
            if result['jindou_success']:
                jindou_success_count += 1
        
        log("  " + "-" * 50)
    
    # 总体统计
    log("总体统计:")
    log(f"  ├── 总账号数: {total_accounts}")
    log(f"  ├── 开源平台签到成功: {oshwhub_success_count}/{total_accounts}")
    log(f"  ├── 金豆签到成功: {jindou_success_count}/{total_accounts}")
    
    if total_points_reward > 0:
        log(f"  ├── 总计获得积分: +{total_points_reward}")
    
    if total_jindou_reward > 0:
        log(f"  ├── 总计获得金豆: +{total_jindou_reward}")
    
    # 计算成功率
    oshwhub_rate = (oshwhub_success_count / total_accounts) * 100 if total_accounts > 0 else 0
    jindou_rate = (jindou_success_count / total_accounts) * 100 if total_accounts > 0 else 0
    
    log(f"  ├── 开源平台成功率: {oshwhub_rate:.1f}%")
    log(f"  └── 金豆签到成功率: {jindou_rate:.1f}%")
    
    # 失败账号列表（排除密码错误）
    failed_oshwhub = [r['account_index'] for r in all_results if not r['oshwhub_success'] and not r.get('password_error', False)]
    failed_jindou = [r['account_index'] for r in all_results if not r['jindou_success'] and not r.get('password_error', False)]
    
    if failed_oshwhub:
        log(f"  开源平台失败账号: {', '.join(map(str, failed_oshwhub))}")
    
    if failed_jindou:
        log(f"  金豆签到失败账号: {', '.join(map(str, failed_jindou))}")
        
    if password_error_accounts:
        log(f"  × 密码错误的账号: {', '.join(map(str, password_error_accounts))}")
       
    if not failed_oshwhub and not failed_jindou and not password_error_accounts:
        log("  所有账号全部签到成功!")
    elif password_error_accounts and not failed_oshwhub and not failed_jindou:
        log("  √ 除了密码错误账号，其他账号全部签到成功!")
    
    log("=" * 70)

    # 先回写凭证（工作流随后更新 secret），再推送总结
    try:
        save_credential_cache(usernames)
    except Exception as e:
        log(f"凭证回写失败: {e}")
    
    # 推送总结（Server酱3 按 sendkey 合并同一批账号）
    try:
        push_summary(all_results, total_accounts, usernames)
    except Exception as e:
        log(f"推送总结时出现异常: {e}")
    
    # 根据失败退出标志决定退出码
    all_failed_accounts = failed_accounts + password_error_accounts
    if enable_failure_exit and all_failed_accounts:
        log(f"检测到失败的账号: {', '.join(map(str, all_failed_accounts))}")
        if password_error_accounts:
            log(f"其中密码错误的账号: {', '.join(map(str, password_error_accounts))}")
        log("由于失败退出功能已开启，返回报错退出码以获得邮件提醒")
        sys.exit(1)
    else:
        if enable_failure_exit:
            log("所有账号签到成功，程序正常退出")
        else:
            log("程序正常退出")
        sys.exit(0)

if __name__ == "__main__":
    main()
