# -*- coding: utf-8 -*-
"""
嘉立创登录页（passport.jlc.com）安全验证 / 滑块处理

现网已经从网易易盾（.nc_scale / .btn_slide）换成了「阿里云验证码 2.0（AFS v2）」，
从抓到的 DOM 可以看到：

    #aliyunCaptcha-window-embed
      #aliyunCaptcha-sliding-wrapper
        #aliyunCaptcha-sliding-body.sliding          <- 392x34 的轨道
          #aliyunCaptcha-sliding-left                <- 已滑过的绿色条
          #aliyunCaptcha-sliding-slider              <- 34x34 滑块，要拖到最右边
          #aliyunCaptcha-sliding-text-box > span     <- "请按住滑块，拖动到最右边"

实现要点（为什么不能直接改 DOM / 用 JS 造事件）：
1. 验证是在页面 JS 内完成的，阿里云会校验事件的 isTrusted。JS 里 dispatchEvent
   造出来的合成事件 isTrusted=false，一定失败，所以只能走浏览器输入管线：
     - 首选 CDP Input.dispatchMouseEvent（Chrome / Edge 都支持，步长与时间戳可控）
     - 兜底 Selenium ActionChains（W3C actions，同样产生可信事件）
2. 阿里云会分析轨迹特征：匀速直线、步长过大、瞬移、全程恒定间隔都会被判机器。
   这里用「加速-匀速-减速 + 纵向抖动 + 随机停顿 + 末端贴近回弹」还原人手轨迹。
3. 无头模式下原来的 --blink-settings=imagesEnabled=false 会让滑块背景图不渲染、
   元素尺寸变成 0，必须去掉，否则连滑块都定位不到。
"""

import math
import random
import time

# ---------------------------------------------------------------- 选择器

ALIYUN_WRAP = "#aliyunCaptcha-window-embed"
ALIYUN_BODY = "#aliyunCaptcha-sliding-body"
ALIYUN_SLIDER = "#aliyunCaptcha-sliding-slider"
ALIYUN_TEXT = "#aliyunCaptcha-sliding-text"

LEGACY_SLIDER = ".btn_slide"      # 旧版易盾
LEGACY_TRACK = ".nc_scale"

# 结束状态里出现这些字样说明这一把没过，需要重拖
_FAIL_WORDS = (
    "失败", "重试", "重新", "错误", "不正确", "再试",
    "failed", "retry", "error",
)


# ---------------------------------------------------------------- 几何读取

_GEO_JS = r"""
return (function () {
  function visible(el) {
    if (!el) return false;
    var st = window.getComputedStyle(el);
    if (!st || st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0) return false;
    var r = el.getBoundingClientRect();
    return r.width > 0.5 && r.height > 0.5;
  }
  var wrap   = document.querySelector('#aliyunCaptcha-window-embed');
  var body   = document.querySelector('#aliyunCaptcha-sliding-body');
  var slider = document.querySelector('#aliyunCaptcha-sliding-slider');
  var textEl = document.querySelector('#aliyunCaptcha-sliding-text');
  if (!visible(wrap) || !visible(body) || !visible(slider)) return null;

  var sr = slider.getBoundingClientRect();
  var br = body.getBoundingClientRect();
  var cs = window.getComputedStyle(body);
  var borderR = parseFloat(cs.borderRightWidth) || 0;
  var borderL = parseFloat(cs.borderLeftWidth) || 0;
  var innerRight = br.left + br.width - borderR;
  var innerLeft = br.left + borderL;

  var startX = sr.left + sr.width / 2;
  var startY = sr.top + sr.height / 2;
  // 关键：阿里云这个「拖到最右边」的模式要求滑块顶到轨道右端（offset 必须达到
  // maxOffset），差 1px 都会被弹回去。这里故意多推 1px，让组件自己的边界夹住。
  var maxOffset = (innerRight - innerLeft) - sr.width;
  var targetX = innerRight - sr.width / 2 + 1;

  return {
    startX: startX,
    startY: startY,
    targetX: targetX,
    distance: targetX - startX,
    maxOffset: maxOffset,
    trackW: br.width,
    sliderW: sr.width,
    sliderLeft: sr.left,
    innerRight: innerRight,
    text: textEl ? (textEl.textContent || '').trim() : '',
    dpr: window.devicePixelRatio || 1,
    vw: window.innerWidth,
    vh: window.innerHeight
  };
})()
"""

_STATUS_JS = r"""
return (function () {
  function visible(el) {
    if (!el) return false;
    var st = window.getComputedStyle(el);
    if (!st || st.display === 'none' || st.visibility === 'hidden') return false;
    var r = el.getBoundingClientRect();
    return r.width > 0.5 && r.height > 0.5;
  }
  var wrap = document.querySelector('#aliyunCaptcha-window-embed');
  var pop  = document.querySelector('.captcha-popup');
  var txt  = document.querySelector('#aliyunCaptcha-sliding-text');
  var slider = document.querySelector('#aliyunCaptcha-sliding-slider');
  var body   = document.querySelector('#aliyunCaptcha-sliding-body');
  var progress = null;
  if (slider && body) {
    progress = slider.getBoundingClientRect().left - body.getBoundingClientRect().left;
  }
  return {
    wrapVisible: visible(wrap),
    popupVisible: visible(pop),
    sliderVisible: visible(slider),
    text: txt ? (txt.textContent || '').trim() : '',
    progress: progress,
    url: location.href
  };
})()
"""

_PROGRESS_JS = r"""
return (function () {
  var slider = document.querySelector('#aliyunCaptcha-sliding-slider');
  var body   = document.querySelector('#aliyunCaptcha-sliding-body');
  var left   = document.querySelector('#aliyunCaptcha-sliding-left');
  if (!slider || !body) return null;
  var sb = slider.getBoundingClientRect(), bb = body.getBoundingClientRect();
  return {
    offset: sb.left - bb.left,
    bodyW: bb.width,
    sliderW: sb.width,
    maxOffset: bb.width - sb.width,
    filled: left ? left.getBoundingClientRect().width : 0
  };
})()
"""


def get_slider_geometry(driver):
    """读取阿里云滑块几何信息，没有滑块返回 None"""
    try:
        return driver.execute_script(_GEO_JS)
    except Exception:
        return None


def read_status(driver):
    """读取验证弹层状态"""
    try:
        return driver.execute_script(_STATUS_JS)
    except Exception:
        return None


def read_progress(driver):
    try:
        return driver.execute_script(_PROGRESS_JS)
    except Exception:
        return None


def legacy_slider_present(driver):
    try:
        return bool(driver.execute_script(
            "var e=document.querySelector('.btn_slide');"
            "return !!(e && e.getBoundingClientRect().width > 5);"
        ))
    except Exception:
        return False


# ---------------------------------------------------------------- 轨迹生成

def build_human_track(distance, rng=None):
    """
    生成一条「像人手」的横向拖动轨迹。

    返回 [{'x': 相对起点位移, 'y': 相对起点纵向偏移, 'dt': 本步耗时(s), 'pause': 停顿(s)}, ...]
    速度模型：起步加速 -> 中段巡航（带噪声） -> 末段减速，末端小幅贴边。
    """
    rng = rng or random.Random()
    distance = float(distance)
    if distance <= 0:
        return []

    vmax = rng.uniform(1250.0, 1950.0)          # px/s
    t_acc = rng.uniform(0.10, 0.17)             # 加速时长
    decel_start = rng.uniform(0.58, 0.72)       # 从总进度百分之几开始减速

    est = distance / (vmax * 0.78)
    t_total = max(0.35, est + t_acc)

    track = []
    x = 0.0
    y = 0.0
    t = 0.0

    # 按住之后先有一下极小的「试劲」，真实用户按下去手会轻微动一下
    track.append({'x': 0.0, 'y': 0.0, 'dt': rng.uniform(0.06, 0.14), 'pause': 0.0})

    guard = 0
    while x < distance - 0.6 and guard < 400:
        guard += 1
        dt = rng.uniform(0.010, 0.019)
        prog = t / t_total

        if t < t_acc:
            v = vmax * (0.30 + 0.70 * (t / max(t_acc, 1e-6)))
        elif prog > decel_start:
            k = min(1.0, (prog - decel_start) / max(1e-6, 1.0 - decel_start))
            v = vmax * (1.0 - 0.88 * (k ** 1.35))
        else:
            v = vmax * rng.uniform(0.92, 1.08)

        v = max(v, 110.0)
        dx = max(0.35, v * dt + rng.uniform(-0.7, 0.7))

        # 别冲过终点
        if x + dx > distance:
            dx = distance - x
            if dx < 0.3:
                break

        x += dx
        t += dt

        # 纵向做慢速随机游走，比纯噪声更像真人
        y += rng.uniform(-0.65, 0.65)
        y = max(-2.2, min(2.2, y))

        pause = 0.0
        if 0.3 < prog < 0.85 and rng.random() < 0.05:
            pause = rng.uniform(0.05, 0.13)
            t += pause

        track.append({'x': x, 'y': y, 'dt': dt, 'pause': pause})

    # 末端：贴到终点后轻微回弹/再压实，模拟人「按到最右边」的动作
    for _ in range(rng.randint(2, 3)):
        track.append({
            'x': x + rng.uniform(0.0, 0.8),
            'y': y + rng.uniform(-1.0, 1.0),
            'dt': rng.uniform(0.018, 0.045),
            'pause': 0.0,
        })
    # 松手前的停顿（松手时也要停在最右端，绝不回退）
    track.append({'x': x + 0.6, 'y': y, 'dt': rng.uniform(0.08, 0.18), 'pause': 0.0})
    return track


# ---------------------------------------------------------------- CDP 鼠标

class CdpMouse(object):
    """
    用 CDP 直接往浏览器输入管线里塞鼠标事件（isTrusted=true）。

    - 自己维护一个「计划时钟」，把事件的 timestamp 按轨迹填好，这样即便
      execute_cdp_cmd 的 HTTP 开销抖动，页面看到的采样间隔依然干净；
    - 实测开销用 EMA 估计，sleep 时扣掉，避免整体越拖越慢。
    """

    def __init__(self, driver):
        self.driver = driver
        self.t0 = time.time()
        self.clock = 0.0
        self.overhead = 0.004
        self.last_ts = 0.0

    def _stamp(self):
        now = time.time()
        planned = self.t0 + self.clock
        if planned > now:            # 不填未来时间
            planned = now
        # 累积落后太多就重设基准（表现为一次自然停顿）
        if now - planned > 0.25:
            self.t0 = now - self.clock
            planned = now
        if planned <= self.last_ts:
            planned = self.last_ts + 0.0005
        self.last_ts = planned
        return planned

    def _dispatch(self, type_, x, y, buttons, click_count=0, force=None):
        params = {
            "type": type_,
            "x": float(x),
            "y": float(y),
            "button": "left",
            "buttons": int(buttons),
            "clickCount": int(click_count),
            "timestamp": self._stamp(),
        }
        # 真实鼠标按住时 PointerEvent.pressure = 0.5，不传 force 会一直是 0，
        # 这是个很容易被采集到的破绽
        if force is not None:
            params["force"] = float(force)
        begin = time.time()
        self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", params)
        cost = time.time() - begin
        self.overhead = self.overhead * 0.7 + cost * 0.3

    def _sleep(self, dt):
        rest = dt - self.overhead
        if rest > 0.002:
            time.sleep(rest)

    def move_to(self, x, y, dt=0.02, jitter=1.0):
        """从当前位置「走」到目标附近（分几步，别瞬移）"""
        steps = random.randint(3, 6)
        for i in range(1, steps + 1):
            k = float(i) / steps
            self._dispatch("mouseMoved",
                           x * k + random.uniform(-jitter, jitter) * (1 - k),
                           y * k + random.uniform(-jitter, jitter) * (1 - k),
                           buttons=0)
            self.clock += dt
            self._sleep(dt)

    def press(self, x, y):
        self._dispatch("mouseMoved", x, y, buttons=0, force=0.0)
        self.clock += 0.03
        self._sleep(0.03)
        self._dispatch("mousePressed", x, y, buttons=1, click_count=1, force=0.5)
        self.clock += 0.02
        self._sleep(0.02)

    def move(self, dx, dy, dt):
        self._dispatch("mouseMoved", dx, dy, buttons=1, force=0.5)
        self.clock += dt
        self._sleep(dt)

    def release(self, x, y):
        self._dispatch("mouseReleased", x, y, buttons=0, click_count=1, force=0.0)


def _warm_up_mouse(driver, rng=None):
    """
    打散「页面加载完就精准一条直线拉到底」的机器特征：
    先在页面里随便晃几下鼠标。
    """
    rng = rng or random.Random()
    try:
        vw = driver.execute_script("return window.innerWidth;") or 1280
        vh = driver.execute_script("return window.innerHeight;") or 800
        mouse = CdpMouse(driver)
        cx, cy = vw * 0.5, vh * 0.5
        for _ in range(rng.randint(3, 5)):
            cx = rng.uniform(vw * 0.25, vw * 0.75)
            cy = rng.uniform(vh * 0.30, vh * 0.70)
            mouse.move_to(cx, cy, dt=rng.uniform(0.012, 0.025))
            time.sleep(rng.uniform(0.05, 0.18))
    except Exception:
        pass


def _supports_cdp(driver):
    return hasattr(driver, "execute_cdp_cmd")


# ---------------------------------------------------------------- 拖拽

def drag_aliyun_slider(driver, geo=None, log=print, seed=None):
    """把阿里云滑块拖到最右边。返回 True 表示动作已发出（不代表服务端通过）"""
    geo = geo or get_slider_geometry(driver)
    if not geo:
        log("  - 未读取到滑块几何信息")
        return False

    distance = geo.get("distance") or 0
    if distance < 10:
        log(f"  - 滑块可拖动距离异常: {distance}")
        return False

    rng = random.Random(seed) if seed is not None else random
    start_x = geo["startX"]
    start_y = geo["startY"]
    track = build_human_track(distance, rng=rng)

    log("  - 滑块: 轨道 {:.0f}px, 滑块 {:.0f}px, 需移动 {:.0f}px, 轨迹 {} 步".format(
        geo.get("trackW", 0), geo.get("sliderW", 0), distance, len(track)))

    if not _supports_cdp(driver):
        return _drag_by_action_chains(driver, start_x, start_y, track, log)

    try:
        _warm_up_mouse(driver, rng)
        mouse = CdpMouse(driver)
        mouse.move_to(start_x + rng.uniform(-2, 2), start_y + rng.uniform(-2, 2),
                      dt=rng.uniform(0.012, 0.02))
        time.sleep(rng.uniform(0.03, 0.10))
        mouse.press(start_x, start_y)

        for step in track:
            if step["pause"]:
                time.sleep(step["pause"])
                mouse.clock += step["pause"]
            mouse.move(start_x + step["x"], start_y + step["y"], dt=step["dt"])

        mouse.release(start_x + track[-1]["x"] if track else start_x, start_y)
        log("  - 已松手")
        return True
    except Exception as e:
        log(f"  - CDP 拖拽失败({e})，改用 ActionChains")
        return _drag_by_action_chains(driver, start_x, start_y, track, log)


def _drag_by_action_chains(driver, start_x, start_y, track, log):
    """兜底：把整条轨迹编成一次 W3C actions 提交（事件同样 isTrusted）"""
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        actions = ActionChains(driver)
        actions.move_to_element_with_offset(
            driver.find_element("css selector", ALIYUN_SLIDER), 0, 0)
        actions.click_and_hold()
        prev_x = prev_y = 0.0
        for step in track:
            actions.move_by_offset(int(round(step["x"] - prev_x)),
                                   int(round(step["y"] - prev_y)))
            prev_x, prev_y = step["x"], step["y"]
            actions.pause(max(0.01, step["dt"] + step["pause"]))
        actions.release()
        actions.perform()
        log("  - ActionChains 拖拽已提交")
        return True
    except Exception as e:
        log(f"  - ActionChains 拖拽失败: {e}")
        return False


# ---------------------------------------------------------------- 旧版易盾

def drag_legacy_slider(driver, log=print, seed=None):
    """旧版易盾滑块（.btn_slide / .nc_scale）——保留兜底，防止哪天又切回去"""
    rng = random.Random(seed) if seed is not None else random
    try:
        slider = driver.find_element("css selector", LEGACY_SLIDER)
        track = driver.find_element("css selector", LEGACY_TRACK)
        distance = track.size["width"] - slider.size["width"] - 10
        if distance < 10:
            log(f"  - 旧版滑块距离异常: {distance}")
            return False
        log(f"  - 旧版滑块需移动 {distance}px")
        actions = ActionChains = __import__(
            "selenium.webdriver.common.action_chains", fromlist=["ActionChains"]).ActionChains(driver)
        actions.click_and_hold(slider).perform()
        time.sleep(rng.uniform(0.2, 0.4))
        moved = 0.0
        while moved < distance - 6:
            step = rng.uniform(6, 18)
            step = min(step, distance - moved)
            actions = __import__("selenium.webdriver.common.action_chains",
                                 fromlist=["ActionChains"]).ActionChains(driver)
            actions.move_by_offset(int(step), rng.randint(-1, 1)).perform()
            moved += step
            time.sleep(rng.uniform(0.01, 0.03))
        actions = __import__("selenium.webdriver.common.action_chains",
                             fromlist=["ActionChains"]).ActionChains(driver)
        actions.release().perform()
        return True
    except Exception as e:
        log(f"  - 旧版滑块拖拽失败: {e}")
        return False


# ---------------------------------------------------------------- 统一入口

def wait_for_captcha(driver, timeout=12.0, poll=0.35):
    """等待验证弹层出现。返回 ('aliyun'|'nc'|None, geo)"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        geo = get_slider_geometry(driver)
        if geo and (geo.get("distance") or 0) > 10:
            return "aliyun", geo
        if legacy_slider_present(driver):
            return "nc", None
        time.sleep(poll)
    return None, None


def _captcha_passed(driver, start_url):
    st = read_status(driver)
    if not st:
        return False
    if st.get("url") and st["url"] != start_url and "passport.jlc.com/login" not in st["url"]:
        return True
    if not st.get("wrapVisible") and not st.get("popupVisible"):
        return True
    return False


def _captcha_failed_hint(driver):
    st = read_status(driver)
    if not st:
        return False
    text = (st.get("text") or "").lower()
    return any(w in text for w in _FAIL_WORDS)


def solve_captcha(driver, log=print, kind=None, max_attempts=4, verify_timeout=7.0):
    """
    处理登录页的安全验证。返回 'passed' / 'failed' / 'none'。

    每拖一次就去观察弹层是否消失；失败（提示重试/滑块回弹）则重新读几何再来一遍，
    阿里云每次失败后轨道宽度和起点都可能变化，所以必须重新采样。
    """
    if kind is None:
        kind, _ = wait_for_captcha(driver, timeout=1.0)

    if kind is None:
        return "none"

    if kind == "nc":
        for attempt in range(1, max_attempts + 1):
            log(f"  - 旧版滑块第 {attempt} 次尝试")
            if drag_legacy_slider(driver, log=log):
                time.sleep(1.5)
                if "passport.jlc.com/login" not in (driver.current_url or ""):
                    return "passed"
        return "failed"

    start_url = driver.current_url
    for attempt in range(1, max_attempts + 1):
        log(f"  - 阿里云滑块第 {attempt}/{max_attempts} 次尝试")
        geo = get_slider_geometry(driver)
        if not geo:
            # 弹层没了：要么已经过了，要么被关掉
            if _captcha_passed(driver, start_url):
                return "passed"
            time.sleep(0.8)
            continue

        ok = drag_aliyun_slider(driver, geo=geo, log=log)
        if not ok:
            time.sleep(0.6)
            continue

        # 观察结果
        deadline = time.time() + verify_timeout
        passed = False
        while time.time() < deadline:
            if _captcha_passed(driver, start_url):
                passed = True
                break
            if _captcha_failed_hint(driver):
                break
            time.sleep(0.25)

        if passed:
            log("  - 安全验证通过")
            return "passed"

        prog = read_progress(driver)
        if prog:
            log("  - 本次未通过（滑块 offset {:.0f}/{:.0f}px）".format(
                prog.get("offset") or 0, prog.get("maxOffset") or 0))
        else:
            log("  - 本次未通过")

        # 等它重置
        time.sleep(random.uniform(0.8, 1.4))

    return "failed"
