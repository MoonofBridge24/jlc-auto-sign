# -*- coding: utf-8 -*-
"""
Selenium 反自动化特征处理

实测结论（2026-11，嘉立创 passport 登录页 / 阿里云验证码 2.0）：
即使滑块轨迹做得再像人，只要页面上留着驱动的自动化痕迹，
`/api/cas/captcha/v2/check-ali-captcha` 依然会返回 checkSuccess=false（"滑块验证失败"）。

关键痕迹是 msedgedriver / chromedriver 挂在 window 上的：

    cdc_adoQpoasnfa76pfcZLmcfl_Array / _Object / _Promise / _Proxy / _Symbol ...

它们不可 delete 时会退化成「重定义为 undefined 的 getter」——只删名字不够，
必须把值也变成 undefined，否则验证码服务端仍能读到。
其余几项（navigator.webdriver、window.chrome、languages/plugins、权限查询）
属于常规环境指纹，一起补齐。

注意：这里不去劫持全局 Object.defineProperty（副作用大、收益小）。
"""

INIT_SCRIPT = r"""
(function () {
  'use strict';
  if (window.__stealth_done) return;
  window.__stealth_done = true;
  var rawDef = Object.defineProperty;

  // 1. 清掉驱动注入的 cdc_* 变量：先尝试 delete，不行就重定义成返回 undefined
  function wipeCdc() {
    var names;
    try {
      names = Object.getOwnPropertyNames(window);
    } catch (e) { return; }
    for (var i = 0; i < names.length; i++) {
      var n = names[i];
      if (!/cdc_/i.test(n)) continue;
      try { delete window[n]; } catch (e) {}
      try {
        rawDef.call(Object, window, n, {
          get: function () { return undefined; },
          set: function () {},
          configurable: true
        });
      } catch (e) {}
    }
  }
  wipeCdc();

  // 2. navigator.webdriver
  try {
    rawDef.call(Object, Navigator.prototype, 'webdriver', {
      get: function () { return undefined; }, configurable: true
    });
  } catch (e) {}

  // 3. UA 里的 HeadlessChrome 字样（--headless=new 会带上，很明显的机器特征）
  try {
    var ua = navigator.userAgent;
    if (ua && ua.indexOf('Headless') !== -1) {
      var clean = ua.replace(/Headless/g, '');
      rawDef.call(Object, navigator, 'userAgent', {
        get: function () { return clean; }, configurable: true
      });
      rawDef.call(Object, navigator, 'appVersion', {
        get: function () { return clean.replace('Mozilla/', ''); }, configurable: true
      });
    }
  } catch (e) {}

  // 4. window.chrome（驱动/无头环境下常缺失）
  if (!window.chrome) { window.chrome = {}; }
  if (!window.chrome.runtime) { window.chrome.runtime = {}; }
  if (!window.chrome.app) { window.chrome.app = { isInstalled: false }; }

  // 5. 无头模式的屏幕尺寸（--headless 下 screen 会报成 800x600，
  //    而窗口是 1920x1080，这种「1440p 窗口 + 15 年前的分辨率」本身就是特征）
  try {
    var sw = window.screen;
    var W = Math.max(window.outerWidth || 0, 1920);
    var H = Math.max(window.outerHeight || 0, 1080);
    var fix = function (obj, prop, value) {
      try {
        rawDef.call(Object, obj, prop, { get: function () { return value; }, configurable: true });
      } catch (e) {}
    };
    if (sw && sw.width < 1024) {
      fix(sw, 'width', W);
      fix(sw, 'height', H);
      fix(sw, 'availWidth', W);
      fix(sw, 'availHeight', H - 40);   // 任务栏
      fix(sw, 'availLeft', 0);
      fix(sw, 'availTop', 0);
      fix(sw, 'colorDepth', 24);
      fix(sw, 'pixelDepth', 24);
    }
  } catch (e) {}

  // 6. 语言 / plugins
  try {
    rawDef.call(Object, navigator, 'languages', {
      get: function () { return ['zh-CN', 'zh', 'en']; }, configurable: true
    });
    if (!navigator.plugins || navigator.plugins.length === 0) {
      rawDef.call(Object, navigator, 'plugins', {
        get: function () { return [1, 2, 3, 4, 5]; }, configurable: true
      });
    }
  } catch (e) {}

  // 7. 权限查询（无头环境常返回 denied，正常浏览器是 prompt）
  try {
    var perms = navigator.permissions;
    var origQuery = perms && perms.query;
    if (origQuery) {
      perms.query = function (p) {
        if (p && p.name === 'notifications') {
          return Promise.resolve({ state: Notification.permission });
        }
        return origQuery.apply(this, arguments);
      };
    }
  } catch (e) {}
})();
"""


def apply(driver):
    """注入到每个新文档（页面脚本执行之前），并对当前页立即执行一次"""
    if hasattr(driver, "execute_cdp_cmd"):
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                                   {"source": INIT_SCRIPT})
        except Exception:
            pass
    try:
        driver.execute_script(INIT_SCRIPT)
    except Exception:
        pass


def cdc_left(driver):
    """调试用：返回残留的驱动标记及其值类型"""
    try:
        return driver.execute_script(
            "var r = {};"
            "Object.getOwnPropertyNames(window).forEach(function (n) {"
            "  if (/cdc/i.test(n)) { r[n] = typeof window[n]; }"
            "});"
            "return r;")
    except Exception:
        return {}
