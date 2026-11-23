import os
import re
import sys
import time
import json
import tempfile
import random
import requests
import selenium
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver import ActionChains
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
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


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

def with_retry(func, max_retries=5, delay=1):
    """如果函数返回None或抛出异常，静默重试"""
    def wrapper(*args, **kwargs):
        for attempt in range(max_retries):
            try:
                result = func(*args, **kwargs)
                if result is not None:
                    return result
                time.sleep(delay + random.uniform(0, 1))  # 随机延迟
            except Exception:
                time.sleep(delay + random.uniform(0, 1))  # 随机延迟
        return None
    return wrapper

@with_retry
def extract_token_from_local_storage(driver):
    """从 localStorage 提取 X-JLC-AccessToken"""
    try:
        token = driver.execute_script("return window.localStorage.getItem('X-JLC-AccessToken');")
        if token:
            log(f"成功从 localStorage 提取 token: {token[:30]}...")
            return token
        else:
            alternative_keys = [
                "x-jlc-accesstoken",
                "accessToken", 
                "token",
                "jlc-token"
            ]
            for key in alternative_keys:
                token = driver.execute_script(f"return window.localStorage.getItem('{key}');")
                if token:
                    log(f"从 localStorage 的 {key} 提取到 token: {token[:30]}...")
                    return token
    except Exception as e:
        log(f"从 localStorage 提取 token 失败: {e}")
    
    return None

@with_retry
def extract_secretkey_from_devtools(driver):
    """使用 DevTools 从网络请求中提取 secretkey"""
    secretkey = None
    
    try:
        logs = driver.get_log('performance')
        
        for entry in logs:
            try:
                message = json.loads(entry['message'])
                message_type = message.get('message', {}).get('method', '')
                
                if message_type == 'Network.requestWillBeSent':
                    request = message.get('message', {}).get('params', {}).get('request', {})
                    url = request.get('url', '')
                    
                    if 'm.jlc.com' in url:
                        headers = request.get('headers', {})
                        secretkey = (
                            headers.get('secretkey') or 
                            headers.get('SecretKey') or
                            headers.get('secretKey') or
                            headers.get('SECRETKEY')
                        )
                        
                        if secretkey:
                            log(f"从请求中提取到 secretkey: {secretkey[:20]}...")
                            return secretkey
                
                elif message_type == 'Network.responseReceived':
                    response = message.get('message', {}).get('params', {}).get('response', {})
                    url = response.get('url', '')
                    
                    if 'm.jlc.com' in url:
                        headers = response.get('requestHeaders', {})
                        secretkey = (
                            headers.get('secretkey') or 
                            headers.get('SecretKey') or
                            headers.get('secretKey') or
                            headers.get('SECRETKEY')
                        )
                        
                        if secretkey:
                            log(f"从响应中提取到 secretkey: {secretkey[:20]}...")
                            return secretkey
                            
            except:
                continue
                
    except Exception as e:
        log(f"DevTools 提取 secretkey 出错: {e}")
    
    return secretkey

def get_oshwhub_points(driver, account_index):
    """获取开源平台积分数量"""
    max_retries = 5
    for attempt in range(max_retries):
        try:
            # 获取当前页面的Cookie
            cookies = driver.get_cookies()
            cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
            
            headers = {
                'user-agent': _current_ua(driver),
                'accept': 'application/json, text/plain, */*',
                'referer': 'https://oshwhub.com/sign_in',
                'cookie': cookie_str
            }
            
            # 调用用户信息API获取积分
            response = requests.get("https://oshwhub.com/api/users", headers=headers, timeout=10)
            if response.status_code == 418:
                log(f"账号 {account_index} - /api/users 被 WAF 拦截(418)，"
                    f"通常是 UA/指纹被判为自动化")
            if response.status_code == 200:
                data = response.json()
                if data and data.get('success'):
                    points = data.get('result', {}).get('points', 0)
                    return points
        except Exception:
            pass  # 静默重试
        
        # 重试前刷新页面
        if attempt < max_retries - 1:
            try:
                driver.refresh()
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                time.sleep(1 + random.uniform(0, 1))
            except:
                pass
    
    log(f"账号 {account_index} - 无法获取积分信息")
    return 0

class JLCClient:
    """调用嘉立创接口"""
    
    def __init__(self, access_token, secretkey, account_index, driver):
        self.base_url = "https://m.jlc.com"
        self.headers = {
            'user-agent': _current_ua(driver),
            'x-jlc-clienttype': 'WEB',
            'accept': 'application/json, text/plain, */*',
            'x-jlc-accesstoken': access_token,
            'secretkey': secretkey,
            'Referer': 'https://m.jlc.com/mapp/pages/my/index',
        }
        self.account_index = account_index
        self.driver = driver
        self.message = ""
        self.initial_jindou = 0  # 签到前金豆数量
        self.final_jindou = 0    # 签到后金豆数量
        self.jindou_reward = 0   # 本次获得金豆（通过差值计算）
        self.sign_status = "未知"  # 签到状态
        self.has_reward = False  # 是否领取了额外奖励
        
    def send_request(self, url, method='GET'):
        """发送 API 请求"""
        try:
            if method.upper() == 'GET':
                response = requests.get(url, headers=self.headers, timeout=10)
            else:
                response = requests.post(url, headers=self.headers, timeout=10)
            
            if response.status_code == 200:
                return response.json()
            else:
                log(f"账号 {self.account_index} - 请求失败，状态码: {response.status_code}")
                return None
        except Exception as e:
            log(f"账号 {self.account_index} - 请求异常 ({url}): {e}")
            return None
    
    def get_user_info(self):
        """获取用户信息"""
        log(f"账号 {self.account_index} - 获取用户信息...")
        url = f"{self.base_url}/api/appPlatform/center/setting/selectPersonalInfo"
        data = self.send_request(url)
        
        if data and data.get('success'):
            log(f"账号 {self.account_index} - 用户信息获取成功")
            return True
        else:
            error_msg = data.get('message', '未知错误') if data else '请求失败'
            log(f"账号 {self.account_index} - 获取用户信息失败: {error_msg}")
            return False
    
    def get_points(self):
        """获取金豆数量"""
        url = f"{self.base_url}/api/activity/front/getCustomerIntegral"
        max_retries = 5
        for attempt in range(max_retries):
            data = self.send_request(url)
            
            if data and data.get('success'):
                jindou_count = data.get('data', {}).get('integralVoucher', 0)
                return jindou_count
            
            # 重试前刷新页面，重新提取 token 和 secretkey
            if attempt < max_retries - 1:
                try:
                    self.driver.get("https://m.jlc.com/")
                    self.driver.refresh()
                    WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                    time.sleep(1 + random.uniform(0, 1))
                    navigate_and_interact_m_jlc(self.driver, self.account_index)
                    access_token = extract_token_from_local_storage(self.driver)
                    secretkey = extract_secretkey_from_devtools(self.driver)
                    if access_token:
                        self.headers['x-jlc-accesstoken'] = access_token
                    if secretkey:
                        self.headers['secretkey'] = secretkey
                except:
                    pass  # 静默继续
        
        log(f"账号 {self.account_index} - 获取金豆数量失败")
        return 0
    
    def check_sign_status(self):
        """检查签到状态"""
        log(f"账号 {self.account_index} - 检查签到状态...")
        url = f"{self.base_url}/api/activity/sign/getCurrentUserSignInConfig"
        data = self.send_request(url)
        
        if data and data.get('success'):
            have_sign_in = data.get('data', {}).get('haveSignIn', False)
            if have_sign_in:
                log(f"账号 {self.account_index} - 今日已签到")
                self.sign_status = "已签到过"
                return True
            else:
                log(f"账号 {self.account_index} - 今日未签到")
                self.sign_status = "未签到"
                return False
        else:
            error_msg = data.get('message', '未知错误') if data else '请求失败'
            log(f"账号 {self.account_index} - 检查签到状态失败: {error_msg}")
            self.sign_status = "检查失败"
            return None
    
    def sign_in(self):
        """执行签到"""
        log(f"账号 {self.account_index} - 执行签到...")
        url = f"{self.base_url}/api/activity/sign/signIn?source=4"
        data = self.send_request(url)
        
        if data and data.get('success'):
            gain_num = data.get('data', {}).get('gainNum')
            if gain_num:
                # 直接签到成功，获得金豆
                log(f"账号 {self.account_index} - 签到成功，签到使金豆+{gain_num}")
                self.sign_status = "签到成功"
                return True
            else:
                # 有奖励可领取，先领取奖励
                log(f"账号 {self.account_index} - 有奖励可领取，先领取奖励")
                self.has_reward = True
                
                # 领取奖励
                if self.receive_voucher():
                    # 领取奖励成功后，视为签到完成
                    log(f"账号 {self.account_index} - 奖励领取成功，签到完成")
                    self.sign_status = "领取奖励成功"
                    return True
                else:
                    self.sign_status = "领取奖励失败"
                    return False
        else:
            error_msg = data.get('message', '未知错误') if data else '请求失败'
            log(f"账号 {self.account_index} - 签到失败: {error_msg}")
            self.sign_status = "签到失败"
            return False
    
    def receive_voucher(self):
        """领取奖励"""
        log(f"账号 {self.account_index} - 领取奖励...")
        url = f"{self.base_url}/api/activity/sign/receiveVoucher"
        data = self.send_request(url)
        
        if data and data.get('success'):
            log(f"账号 {self.account_index} - 领取成功")
            return True
        else:
            error_msg = data.get('message', '未知错误') if data else '请求失败'
            log(f"账号 {self.account_index} - 领取奖励失败: {error_msg}")
            return False
    
    def calculate_jindou_difference(self):
        """计算金豆差值"""
        self.jindou_reward = self.final_jindou - self.initial_jindou
        if self.jindou_reward > 0:
            reward_text = f" (+{self.jindou_reward})"
            if self.has_reward:
                reward_text += "（有奖励）"
            log(f"账号 {self.account_index} - 总金豆增加: {self.initial_jindou} → {self.final_jindou}{reward_text}")
        elif self.jindou_reward == 0:
            log(f"账号 {self.account_index} - 总金豆无变化，可能今天已签到过: {self.initial_jindou} → {self.final_jindou} (0)")
        else:
            log(f"账号 {self.account_index} - 金豆减少: {self.initial_jindou} → {self.final_jindou} ({self.jindou_reward})")
        
        return self.jindou_reward
    
    def execute_full_process(self):
        """执行金豆签到流程"""        
        # 1. 获取用户信息
        if not self.get_user_info():
            return False
        
        time.sleep(random.randint(1, 2))
        
        # 2. 获取签到前金豆数量
        self.initial_jindou = self.get_points()
        if self.initial_jindou is None:
            self.initial_jindou = 0
        log(f"账号 {self.account_index} - 签到前金豆??: {self.initial_jindou}")
        
        time.sleep(random.randint(1, 2))
        
        # 3. 检查签到状态
        sign_status = self.check_sign_status()
        if sign_status is None:  # 检查失败
            return False
        elif sign_status:  # 已签到
            # 已签到，直接获取金豆数量
            log(f"账号 {self.account_index} - 今日已签到，跳过签到操作")
        else:  # 未签到
            # 4. 执行签到
            time.sleep(random.randint(2, 3))
            if not self.sign_in():
                return False
        
        time.sleep(random.randint(1, 2))
        
        # 5. 获取签到后金豆数量
        self.final_jindou = self.get_points()
        if self.final_jindou is None:
            self.final_jindou = 0
        log(f"账号 {self.account_index} - 签到后金豆??: {self.final_jindou}")
        
        # 6. 计算金豆差值
        self.calculate_jindou_difference()
        
        return True

def navigate_and_interact_m_jlc(driver, account_index):
    """在 m.jlc.com 进行导航和交互以触发网络请求"""
    log(f"账号 {account_index} - 在 m.jlc.com 进行交互操作...")
    
    try:
        WebDriverWait(driver, 12).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        driver.execute_script("window.scrollTo(0, 300);")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        
        nav_selectors = [
            "//div[contains(text(), '我的')]",
            "//div[contains(text(), '个人中心')]",
            "//div[contains(text(), '用户中心')]",
            "//a[contains(@href, 'user')]",
            "//a[contains(@href, 'center')]",
        ]
        
        for selector in nav_selectors:
            try:
                element = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, selector)))
                element.click()
                log(f"账号 {account_index} - 点击导航元素: {selector}")
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                break
            except:
                continue
        
        driver.execute_script("window.scrollTo(0, 500);")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        driver.refresh()
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        
    except Exception as e:
        log(f"账号 {account_index} - 交互操作出错: {e}")

def is_sunday():
    """检查今天是否是周日"""
    return datetime.now().weekday() == 6

def is_last_day_of_month():
    """检查今天是否是当月最后一天"""
    today = datetime.now()
    next_month = today.replace(day=28) + timedelta(days=4)
    last_day = next_month - timedelta(days=next_month.day)
    return today.day == last_day.day

def capture_reward_info(driver, account_index, gift_type):
    """抓取并输出奖励信息，返回礼包领取结果"""
    try:
        reward_elem = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, '//p[contains(text(), "恭喜获取")]'))
        )
        reward_text = reward_elem.text.strip()
        gift_name = "七日礼包" if gift_type == "7天" else "月度礼包"
        log(f"账号 {account_index} - {gift_name}领取结果：{reward_text}")
        return f"开源平台{gift_name}领取结果: {reward_text}"
    except Exception as e:
        log(f"账号 {account_index} - 已点击{gift_type}好礼，未获取到奖励信息(可能已领取过或未达到领取条件)，请自行前往开源平台查看。")
        return None

def click_gift_buttons(driver, account_index):
    """根据日期条件点击7天好礼和月度好礼按钮，并抓取奖励信息，返回所有领取结果"""
    reward_results = []
    
    if not is_sunday() and not is_last_day_of_month():
        return reward_results

    try:
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        
        log(f"账号 {account_index} - 开始点击礼包按钮...")
        
        sunday = is_sunday()
        last_day = is_last_day_of_month()

        if sunday:
            # 尝试点击7天好礼
            try:
                seven_day_gift = driver.find_element(By.XPATH, '//div[contains(@class, "sign_text__r9zaN")]/span[text()="7天好礼"]')
                seven_day_gift.click()
                log(f"账号 {account_index} - 检测到今天是周日，成功点击7天好礼，祝你周末愉快~")
                
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                reward_result = capture_reward_info(driver, account_index, "7天")
                if reward_result:
                    reward_results.append(reward_result)
                
                # 如果也是月底，刷新页面
                if last_day:
                    driver.refresh()
                    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                    time.sleep(12)
                
            except Exception as e:
                log(f"账号 {account_index} - 无法点击7天好礼: {e}")

        if last_day:
            # 尝试点击月度好礼
            try:
                monthly_gift = driver.find_element(By.XPATH, '//div[contains(@class, "sign_text__r9zaN")]/span[text()="月度好礼"]')
                monthly_gift.click()
                log(f"账号 {account_index} - 检测到今天是月底，成功点击月度好礼～")          
                
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                reward_result = capture_reward_info(driver, account_index, "月度")
                if reward_result:
                    reward_results.append(reward_result)
                
            except Exception as e:
                log(f"账号 {account_index} - 无法点击月度好礼: {e}")
            
    except Exception as e:
        log(f"账号 {account_index} - 点击礼包按钮时出错: {e}")

    return reward_results

@with_retry
def _api_via_browser(driver, url):
    """
    在页面内用 fetch 请求接口，自动继承浏览器的 Cookie / Origin / Referer / UA。

    返回 {'status': int, 'body': str}，status 为 -1 表示请求本身没发出去。
    """
    script = """
    var url = arguments[0], done = arguments[arguments.length - 1];
    fetch(url, {
      credentials: 'include',
      headers: { 'accept': 'application/json, text/plain, */*' }
    }).then(function (r) {
      return r.text().then(function (t) {
        done({ status: r.status, body: (t || '').slice(0, 1200) });
      });
    }).catch(function (e) {
      done({ status: -1, body: 'fetch 异常: ' + String(e).slice(0, 200) });
    });
    """
    try:
        driver.set_script_timeout(20)
        result = driver.execute_async_script(script, url)
        if isinstance(result, dict):
            return result
        return {"status": -1, "body": "返回值异常: %r" % (result,)}
    except Exception as e:
        return {"status": -1, "body": "执行 fetch 失败: %s" % str(e).splitlines()[0][:200]}


def get_user_nickname_from_api(driver, account_index):
    """
    通过 /api/users 获取用户昵称。

    原来是手工拼 Cookie 头交给 requests，有三个问题：
    - UA 被写死成 Chrome/120，跟实际浏览器（Edge/158）对不上；
    - 不带 Origin / Referer，服务端容易判成非浏览器请求；
    - 失败时只打印「无法获取用户昵称」，看不到状态码，无法排查。
    现在优先在页面内 fetch（真实上下文），失败再退回 requests 并打印状态码。
    """
    resp = _api_via_browser(driver, "https://oshwhub.com/api/users")
    status, body = resp.get("status"), resp.get("body") or ""

    data = None
    if status == 200:
        try:
            data = json.loads(body)
        except Exception:
            data = None

    if data and data.get("success"):
        nickname = (data.get("result") or {}).get("nickname", "")
        if nickname:
            formatted_nickname = format_nickname(nickname)
            log(f"账号 {account_index} - 昵称: {formatted_nickname}")
            return formatted_nickname

    # 页面内请求失败时，退回 requests 再试一次（保留旧路径，兼容个别环境）
    try:
        cookies = driver.get_cookies()
        cookie_str = "; ".join(["%s=%s" % (c["name"], c["value"]) for c in cookies])
        ua = driver.execute_script("return navigator.userAgent;") or ""
        headers = {
            "user-agent": ua,
            "accept": "application/json, text/plain, */*",
            "referer": "https://oshwhub.com/sign_in",
            "cookie": cookie_str,
        }
        r = requests.get("https://oshwhub.com/api/users", headers=headers, timeout=10)
        log(f"账号 {account_index} - /api/users 页面内请求 status={status}，"
            f"requests 重试 status={r.status_code}")
        if r.status_code == 200:
            fallback = r.json()
            if fallback and fallback.get("success"):
                nickname = (fallback.get("result") or {}).get("nickname", "")
                if nickname:
                    formatted_nickname = format_nickname(nickname)
                    log(f"账号 {account_index} - 昵称: {formatted_nickname}")
                    return formatted_nickname
        if status == 418:
            log(f"账号 {account_index} - /api/users 被 WAF 拦截(418)，"
                f"说明无头 UA / 指纹仍被识别为自动化")
        log(f"账号 {account_index} - /api/users 返回内容(status={status}): {str(body)[:200]}")
    except Exception as e:
        log(f"账号 {account_index} - /api/users 备用请求异常: {str(e).splitlines()[0][:120]}")

    log(f"账号 {account_index} - 无法获取用户昵称")
    return None


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

def sign_in_account(username, password, account_index, total_accounts, retry_count=0, is_final_retry=False):
    """为单个账号执行完整的签到流程（包含重试机制）"""
    retry_label = ""
    if retry_count > 0:
        retry_label = f" (重试{retry_count})"
    if is_final_retry:
        retry_label = " (最终重试)"
    
    log(f"开始处理账号 {account_index}/{total_accounts}{retry_label}")
    
    driver = create_driver()
    wait = WebDriverWait(driver, 25)
    
    # 记录详细结果
    result = {
        'account_index': account_index,
        'nickname': '未知',
        'oshwhub_status': '未知',
        'oshwhub_success': False,
        'initial_points': 0,      # 签到前积分
        'final_points': 0,        # 签到后积分
        'points_reward': 0,       # 本次获得积分
        'reward_results': [],     # 礼包领取结果
        'jindou_status': '未知',
        'jindou_success': False,
        'initial_jindou': 0,
        'final_jindou': 0,
        'jindou_reward': 0,
        'has_jindou_reward': False,  # 金豆是否有额外奖励
        'token_extracted': False,
        'secretkey_extracted': False,
        'retry_count': retry_count,
        'is_final_retry': is_final_retry,
        'password_error': False  #标记密码错误
    }

    try:
        # 1. 确保进入登录页面（必要时会重启浏览器，返回新的 driver）
        login_ready, driver = ensure_login_page(driver, account_index)
        if not login_ready:
            result['oshwhub_status'] = '无法进入登录页'
            return result
        wait = WebDriverWait(driver, 25)

        current_url = driver.current_url

        # 2. 登录流程
        log(f"账号 {account_index} - 检测到未登录状态，正在执行登录流程...")

        try:
            phone_btn = wait.until(
                EC.element_to_be_clickable((By.XPATH, '//button[contains(text(),"账号登录")]'))
            )
            phone_btn.click()
            log(f"账号 {account_index} - 已切换账号登录")
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, '//input[@placeholder="请输入手机号码 / 客户编号 / 邮箱"]')))
        except Exception as e:
            log(f"账号 {account_index} - 账号登录按钮可能已默认选中: {e}")

        # 输入账号密码
        try:
            user_input = wait.until(
                EC.presence_of_element_located((By.XPATH, '//input[@placeholder="请输入手机号码 / 客户编号 / 邮箱"]'))
            )
            user_input.clear()
            user_input.send_keys(username)

            pwd_input = wait.until(
                EC.presence_of_element_located((By.XPATH, '//input[@type="password"]'))
            )
            pwd_input.clear()
            pwd_input.send_keys(password)
            log(f"账号 {account_index} - 已输入账号密码")
        except Exception as e:
            log(f"账号 {account_index} - 登录输入框未找到: {e}")
            result['oshwhub_status'] = '登录失败'
            return result

        # 点击登录
        try:
            login_btn = wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "button.submit"))
            )
            login_btn.click()
            log(f"账号 {account_index} - 已点击登录按钮")
        except Exception as e:
            log(f"账号 {account_index} - 登录按钮定位失败: {e}")
            result['oshwhub_status'] = '登录失败'
            return result

        # 立即检查密码错误提示（点击登录按钮后）
        time.sleep(1)  # 给错误提示一点时间显示
        if check_password_error(driver, account_index):
            result['password_error'] = True
            result['oshwhub_status'] = '密码错误'
            return result

        # 处理安全验证：现网是阿里云验证码 2.0（aliyunCaptcha 滑块），旧版易盾作兜底
        captcha_kind = None
        if captcha_solver:
            try:
                captcha_kind, _ = captcha_solver.wait_for_captcha(driver, timeout=12)
            except Exception as e:
                log(f"账号 {account_index} - 检测安全验证时出错: {e}")

        if captcha_kind:
            captcha_label = "阿里云" if captcha_kind == "aliyun" else "易盾"
            log(f"账号 {account_index} - 检测到{captcha_label}滑块验证，开始处理...")

            def _captcha_log(msg, _idx=account_index):
                log(f"账号 {_idx} {msg}")

            try:
                captcha_result = captcha_solver.solve_captcha(
                    driver, log=_captcha_log, kind=captcha_kind)
            except Exception as e:
                log(f"账号 {account_index} - 滑块处理异常: {e}")
                captcha_result = "failed"

            if captcha_result != "passed":
                log(f"账号 {account_index} - 滑块验证未通过")
                time.sleep(1)
                if check_password_error(driver, account_index):
                    result['password_error'] = True
                    result['oshwhub_status'] = '密码错误'
                    return result
                result['oshwhub_status'] = '滑块验证失败'
                return result

            log(f"账号 {account_index} - 滑块验证通过")
        else:
            log(f"账号 {account_index} - 未检测到滑块验证，继续等待登录跳转")

        # 验证通过后立刻检查密码错误提示
        time.sleep(1)
        if check_password_error(driver, account_index):
            result['password_error'] = True
            result['oshwhub_status'] = '密码错误'
            return result

        # 等待跳转
        log(f"账号 {account_index} - 等待登录跳转...")
        max_wait = 15
        jumped = False
        for i in range(max_wait):
            current_url = driver.current_url
            
            # 检查是否成功跳转回签到页面
            if "oshwhub.com" in current_url and "passport.jlc.com" not in current_url:
                log(f"账号 {account_index} - 成功跳转回签到页面")
                jumped = True
                break
            
            time.sleep(1)
        
        if not jumped:
            current_title = driver.title
            log(f"账号 {account_index} - 跳转超时，当前页面标题: {current_title}")
            result['oshwhub_status'] = '跳转失败'
            return result

        # 3. 获取用户昵称
        time.sleep(1)
        nickname = get_user_nickname_from_api(driver, account_index)
        if nickname:
            result['nickname'] = nickname
        else:
            result['nickname'] = '未知'

        # 4. 获取签到前积分数量
        initial_points = get_oshwhub_points(driver, account_index)
        result['initial_points'] = initial_points if initial_points is not None else 0
        log(f"账号 {account_index} - 签到前积分??: {result['initial_points']}")

        # 5. 开源平台签到
        log(f"账号 {account_index} - 正在签到中...")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        try:
            driver.refresh()
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except:
            pass
        time.sleep(6)
        # 执行开源平台签到
        try:
            # 先检查是否已经签到
            try:
                signed_element = driver.find_element(By.XPATH, '//span[contains(text(),"已签到")]')
                log(f"账号 {account_index} - 今天已经在开源平台签到过了！")
                result['oshwhub_status'] = '已签到过'
                result['oshwhub_success'] = True
                
                # 即使已签到，也尝试点击礼包按钮
                result['reward_results'] = click_gift_buttons(driver, account_index)
                
            except:
                # 如果没有找到"已签到"元素，则尝试点击"立即签到"按钮，并验证是否变为"已签到"
                signed = False
                max_attempts = 5
                for attempt in range(max_attempts):
                    try:
                        sign_btn = wait.until(
                            EC.element_to_be_clickable((By.XPATH, '//span[contains(text(),"立即签到")]'))
                        )
                        sign_btn.click()
                        time.sleep(2)  # 等待页面更新
                        driver.refresh()  # 刷新页面以确保状态更新
                        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                        time.sleep(2)  # 额外等待

                        # 检查是否变为"已签到"
                        signed_element = driver.find_element(By.XPATH, '//span[contains(text(),"已签到")]')
                        signed = True
                        break  # 成功，退出循环
                    except:
                        pass  # 静默继续下一次尝试

                if signed:
                    log(f"账号 {account_index} - 开源平台签到成功！")
                    result['oshwhub_status'] = '签到成功'
                    result['oshwhub_success'] = True
                    
                    # 等待签到完成
                    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                    
                    # 6. 签到完成后点击7天好礼和月度好礼
                    result['reward_results'] = click_gift_buttons(driver, account_index)
                else:
                    log(f"账号 {account_index} - 开源平台签到失败")
                    result['oshwhub_status'] = '签到失败'
                    
        except Exception as e:
            log(f"账号 {account_index} - 开源平台签到异常: {e}")
            result['oshwhub_status'] = '签到异常'

        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        # 7. 获取签到后积分数量
        final_points = get_oshwhub_points(driver, account_index)
        result['final_points'] = final_points if final_points is not None else 0
        log(f"账号 {account_index} - 签到后积分??: {result['final_points']}")

        # 8. 计算积分差值
        result['points_reward'] = result['final_points'] - result['initial_points']
        if result['points_reward'] > 0:
            log(f"账号 {account_index} - 总积分增加: {result['initial_points']} → {result['final_points']} (+{result['points_reward']})")
        elif result['points_reward'] == 0:
            log(f"账号 {account_index} - 总积分无变化，可能今天已签到过: {result['initial_points']} → {result['final_points']} (0)")
        else:
            log(f"账号 {account_index} - 积分减少: {result['initial_points']} → {result['final_points']} ({result['points_reward']})")

        # 9. 金豆签到流程
        log(f"账号 {account_index} - 开始金豆签到流程...")
        driver.get("https://m.jlc.com/")
        log(f"账号 {account_index} - 已访问 m.jlc.com，等待页面加载...")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        
        navigate_and_interact_m_jlc(driver, account_index)
        
        access_token = extract_token_from_local_storage(driver)
        secretkey = extract_secretkey_from_devtools(driver)
        
        result['token_extracted'] = bool(access_token)
        result['secretkey_extracted'] = bool(secretkey)
        
        if access_token and secretkey:
            log(f"账号 {account_index} - 成功提取 token 和 secretkey")
            
            jlc_client = JLCClient(access_token, secretkey, account_index, driver)
            jindou_success = jlc_client.execute_full_process()
            
            # 记录金豆签到结果
            result['jindou_success'] = jindou_success
            result['jindou_status'] = jlc_client.sign_status
            result['initial_jindou'] = jlc_client.initial_jindou
            result['final_jindou'] = jlc_client.final_jindou
            result['jindou_reward'] = jlc_client.jindou_reward
            result['has_jindou_reward'] = jlc_client.has_reward
            
            if jindou_success:
                log(f"账号 {account_index} - 金豆签到流程完成")
            else:
                log(f"账号 {account_index} - 金豆签到流程失败")
        else:
            log(f"账号 {account_index} - 无法提取到 token 或 secretkey，跳过金豆签到")
            result['jindou_status'] = 'Token提取失败'

    except Exception as e:
        log(f"账号 {account_index} - 程序执行错误: {e}")
        result['oshwhub_status'] = '执行异常'
    finally:
        driver.quit()
        log(f"账号 {account_index} - 浏览器已关闭")
    
    return result

def should_retry(merged_success, password_error):
    """判断是否需要重试：如果开源平台或金豆签到未成功，且不是密码错误"""
    need_retry = (not merged_success['oshwhub'] or not merged_success['jindou']) and not password_error
    return need_retry

def process_single_account(username, password, account_index, total_accounts):
    """处理单个账号，包含重试机制，并合并多次尝试的最佳结果"""
    max_retries = 3  # 最多重试3次
    merged_result = {
        'account_index': account_index,
        'nickname': '未知',
        'oshwhub_status': '未知',
        'oshwhub_success': False,
        'initial_points': 0,
        'final_points': 0,
        'points_reward': 0,
        'reward_results': [],
        'jindou_status': '未知',
        'jindou_success': False,
        'initial_jindou': 0,
        'final_jindou': 0,
        'jindou_reward': 0,
        'has_jindou_reward': False,
        'token_extracted': False,
        'secretkey_extracted': False,
        'retry_count': 0,  # 记录最后使用的retry_count
        'is_final_retry': False,
        'password_error': False  # 标记密码错误
    }
    
    merged_success = {'oshwhub': False, 'jindou': False}

    for attempt in range(max_retries + 1):  # 第一次执行 + 重试次数
        result = sign_in_account(username, password, account_index, total_accounts, retry_count=attempt)
        
        # 如果检测到密码错误，立即停止重试
        if result.get('password_error'):
            merged_result['password_error'] = True
            merged_result['oshwhub_status'] = '密码错误'
            merged_result['nickname'] = '未知'
            break
        
        # 合并开源平台结果：如果本次成功且之前未成功，则更新
        if result['oshwhub_success'] and not merged_success['oshwhub']:
            merged_success['oshwhub'] = True
            merged_result['oshwhub_status'] = result['oshwhub_status']
            merged_result['initial_points'] = result['initial_points']
            merged_result['final_points'] = result['final_points']
            merged_result['points_reward'] = result['points_reward']
            merged_result['reward_results'] = result['reward_results']  # 合并礼包结果
        
        # 合并金豆结果：如果本次成功且之前未成功，则更新
        if result['jindou_success'] and not merged_success['jindou']:
            merged_success['jindou'] = True
            merged_result['jindou_status'] = result['jindou_status']
            merged_result['initial_jindou'] = result['initial_jindou']
            merged_result['final_jindou'] = result['final_jindou']
            merged_result['jindou_reward'] = result['jindou_reward']
            merged_result['has_jindou_reward'] = result['has_jindou_reward']
        
        # 更新其他字段（如果之前未知）
        if merged_result['nickname'] == '未知' and result['nickname'] != '未知':
            merged_result['nickname'] = result['nickname']
        
        if not merged_result['token_extracted'] and result['token_extracted']:
            merged_result['token_extracted'] = result['token_extracted']
        
        if not merged_result['secretkey_extracted'] and result['secretkey_extracted']:
            merged_result['secretkey_extracted'] = result['secretkey_extracted']
        
        # 更新retry_count为最后一次尝试的
        merged_result['retry_count'] = result['retry_count']
        
        # 检查是否还需要重试（排除密码错误的情况）
        if not should_retry(merged_success, merged_result['password_error']) or attempt >= max_retries:
            break
        else:
            log(f"账号 {account_index} - 准备第 {attempt + 1} 次重试，等待 {random.randint(2, 6)} 秒后重新开始...")
            time.sleep(random.randint(2, 6))
    
    # 最终设置success字段基于合并
    merged_result['oshwhub_success'] = merged_success['oshwhub']
    merged_result['jindou_success'] = merged_success['jindou']
    
    return merged_result

def execute_final_retry_for_failed_accounts(all_results, usernames, passwords, total_accounts):
    """对失败的账号执行最终重试（排除密码错误的账号）"""
    log("=" * 70)
    log("执行最终重试 - 处理所有重试后仍失败的账号")
    log("=" * 70)
    
    # 找出需要最终重试的账号（排除密码错误的）
    failed_accounts = []
    for i, result in enumerate(all_results):
        if (not result['oshwhub_success'] or not result['jindou_success']) and not result.get('password_error', False):
            failed_accounts.append({
                'index': i,
                'account_index': result['account_index'],
                'username': usernames[result['account_index'] - 1],
                'password': passwords[result['account_index'] - 1],
                'previous_retry_count': result['retry_count']
            })
    
    if not failed_accounts:
        log("没有需要最终重试的账号")
        return all_results
    
    log(f"需要最终重试的账号: {', '.join(str(acc['account_index']) for acc in failed_accounts)}")
    
    # 等待一段时间再开始最终重试
    wait_time = random.randint(2, 3)
    log(f"等待 {wait_time} 秒后开始最终重试...")
    time.sleep(wait_time)
    
    # 执行最终重试
    for failed_acc in failed_accounts:
        log(f"开始最终重试账号 {failed_acc['account_index']}")
        
        # 执行最终重试（只执行一次），retry_count 设置为之前的 +1，但不超过3+1
        final_result = sign_in_account(
            failed_acc['username'], 
            failed_acc['password'], 
            failed_acc['account_index'], 
            total_accounts, 
            retry_count=failed_acc['previous_retry_count'] + 1,
            is_final_retry=True
        )
        
        # 如果最终重试检测到密码错误，标记但不更新其他状态
        if final_result.get('password_error'):
            original_result = all_results[failed_acc['index']]
            original_result['password_error'] = True
            original_result['oshwhub_status'] = '密码错误'
            original_result['nickname'] = '未知'
            original_result['is_final_retry'] = True
            original_result['retry_count'] = failed_acc['previous_retry_count'] + 1
            log(f"账号 {failed_acc['account_index']} - 最终重试检测到密码错误")
            continue
        
        original_result = all_results[failed_acc['index']]
        
        # 更新开源平台结果
        if final_result['oshwhub_success'] and not original_result['oshwhub_success']:
            original_result['oshwhub_success'] = True
            original_result['oshwhub_status'] = final_result['oshwhub_status']
            original_result['initial_points'] = final_result['initial_points']
            original_result['final_points'] = final_result['final_points']
            original_result['points_reward'] = final_result['points_reward']
            original_result['reward_results'] = final_result['reward_results']
            log(f"账号 {failed_acc['account_index']} - 开源平台签到成功")
        
        # 更新金豆结果
        if final_result['jindou_success'] and not original_result['jindou_success']:
            original_result['jindou_success'] = True
            original_result['jindou_status'] = final_result['jindou_status']
            original_result['initial_jindou'] = final_result['initial_jindou']
            original_result['final_jindou'] = final_result['final_jindou']
            original_result['jindou_reward'] = final_result['jindou_reward']
            original_result['has_jindou_reward'] = final_result['has_jindou_reward']
            log(f"账号 {failed_acc['account_index']} - 金豆签到成功")
        
        # 更新其他信息
        if original_result['nickname'] == '未知' and final_result['nickname'] != '未知':
            original_result['nickname'] = final_result['nickname']
        
        if not original_result['token_extracted'] and final_result['token_extracted']:
            original_result['token_extracted'] = final_result['token_extracted']
        
        if not original_result['secretkey_extracted'] and final_result['secretkey_extracted']:
            original_result['secretkey_extracted'] = final_result['secretkey_extracted']
        
        original_result['is_final_retry'] = True
        original_result['retry_count'] = failed_acc['previous_retry_count'] + 1
        
        # 如果不是最后一个账号，等待一段时间
        if failed_acc != failed_accounts[-1]:
            wait_time = random.randint(3, 5)
            log(f"等待 {wait_time} 秒后处理下一个重试账号...")
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


def _account_line(account, result):
    """返回 (类别, 一行描述)；类别为 ok / already / failed"""
    if result.get('password_error'):
        return 'failed', "❌ 账号({})：账号或密码错误".format(account)

    status = result.get('jindou_status') or '未知'
    if status == '签到成功':
        return 'ok', "✅ 账号({})：获取{}个金豆，当前总数：{}".format(
            account, _fmt_reward(result.get('jindou_reward') or 0),
            result.get('final_jindou') or 0)
    if status == '已签到过':
        return 'already', "✅ 账号({})：今日已签到，当前总数：{}".format(
            account, result.get('final_jindou') or 0)
    return 'failed', "❌ 账号({})：签到失败（{}）".format(account, status)


def _build_serverchan3_desp(group_results, usernames):
    """
    生成推送正文（Server酱3 走 markdown，单换行会被拼在一起，所以行间用双换行）：

        签到统计
        成功：4
        已签到：0
        失败：0
        【签到成功】
        ✅ 账号(12****5A)：获取2个金豆，当前总数：90.0
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
            elif result['points_reward'] == 0 and result['initial_points'] > 0:
                log(f"  ├── 积分变化: {result['initial_points']} → {result['final_points']} (0)")
            else:
                log(f"  ├── 积分状态: 无法获取积分信息")
            
            log(f"  ├── 金豆签到: {result['jindou_status']}")
            
            # 显示金豆变化
            if result['jindou_reward'] > 0:
                jindou_text = f"  ├── 金豆变化: {result['initial_jindou']} → {result['final_jindou']} (+{result['jindou_reward']})"
                if result['has_jindou_reward']:
                    jindou_text += "（有奖励）"
                log(jindou_text)
                total_jindou_reward += result['jindou_reward']
            elif result['jindou_reward'] == 0 and result['initial_jindou'] > 0:
                log(f"  ├── 金豆变化: {result['initial_jindou']} → {result['final_jindou']} (0)")
            else:
                log(f"  ├── 金豆状态: 无法获取金豆信息")
            
            # 显示礼包领取结果
            for reward_result in result['reward_results']:
                log(f"  ├── {reward_result}")
            
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
