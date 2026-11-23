# -*- coding: utf-8 -*-
"""
本地调试脚本：验证嘉立创登录页「阿里云滑块」能否被自动拖过。

用法（在项目根目录，用虚拟环境里的解释器跑）：

    .venv/Scripts/python.exe test_captcha.py --account 你的账号 --password 你的密码
    .venv/Scripts/python.exe test_captcha.py --account 你的账号 --password 你的密码 --browser edge
    .venv/Scripts/python.exe test_captcha.py --fake            # 用假账号触发验证码，只看滑块
    .venv/Scripts/python.exe test_captcha.py --fake --headless  # 无头模式对比

说明：
- 默认用 Chrome，找不到就自动退回 Edge；
- 会把每一步的截图存到 debug/ 目录，方便判断是「滑块没拖到」还是「被判定机器」；
- --fake 只用来观察验证码弹层，账号密码是假的，最后一定登录失败，属正常。
"""

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import captcha_solver as cs

LOGIN_URL = ("https://passport.jlc.com/window/login?appId=JLC_PORTAL_PC"
             "&redirectUrl=https%3A%2F%2Fwww.oshwhub.com%2F")

DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")


def log(msg):
    print("[{}] {}".format(time.strftime("%H:%M:%S"), msg), flush=True)


def shot(driver, name):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        path = os.path.join(DEBUG_DIR, name + ".png")
        driver.save_screenshot(path)
        log("截图: {}".format(path))
    except Exception as e:
        log("截图失败: {}".format(e))


def build_driver(browser, headless):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.edge.options import Options as EdgeOptions

    order = [browser] if browser else ["chrome", "edge"]
    last_err = None
    for name in order:
        try:
            if name == "edge":
                opts = EdgeOptions()
                if headless:
                    opts.add_argument("--headless=new")
            else:
                opts = ChromeOptions()
                if headless:
                    opts.add_argument("--headless=new")

            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-gpu")
            opts.add_argument("--window-size=1482,991")
            opts.add_argument("--lang=zh-CN")
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            opts.add_experimental_option("useAutomationExtension", False)
            opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
            # 注意：不要加 --blink-settings=imagesEnabled=false，否则滑块背景图不渲染

            driver = (webdriver.Edge(options=opts) if name == "edge"
                      else webdriver.Chrome(options=opts))
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            })
            try:
                import stealth
                stealth.apply(driver)
            except Exception as e:
                log("反检测脚本注入失败: {}".format(e))
            log("已启动浏览器: {} {}".format(name, driver.capabilities.get("browserVersion")))
            return driver
        except Exception as e:
            last_err = e
            log("启动 {} 失败: {}".format(name, e))
    raise RuntimeError("没有可用浏览器: {}".format(last_err))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--browser", default="", choices=["", "chrome", "edge"])
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--fake", action="store_true", help="用假账号，只测滑块")
    ap.add_argument("--keep", action="store_true", help="结束后不关浏览器")
    args = ap.parse_args()

    account = args.account
    password = args.password
    if args.fake or not account:
        account = "13800138000"
        password = "FakePass_{}".format(random.randint(1000, 9999))
        log("使用假账号测试滑块：{}".format(account))

    driver = build_driver(args.browser, args.headless)
    wait = None
    try:
        driver.get(LOGIN_URL)
        time.sleep(3)
        shot(driver, "01_login_page")

        # 切到账号登录
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        wait = WebDriverWait(driver, 20)
        try:
            wait.until(EC.element_to_be_clickable(
                (By.XPATH, '//button[contains(text(),"账号登录")]'))).click()
            time.sleep(1)
        except Exception as e:
            log("账号登录 Tab: {}".format(e))

        user_input = wait.until(EC.presence_of_element_located(
            (By.XPATH, '//input[@placeholder="请输入手机号码 / 客户编号 / 邮箱"]')))
        user_input.clear()
        user_input.send_keys(account)

        pwd_input = wait.until(EC.presence_of_element_located((By.XPATH, '//input[@type="password"]')))
        pwd_input.clear()
        pwd_input.send_keys(password)
        log("已填写账号密码")

        driver.find_element(By.CSS_SELECTOR, "button.submit").click()
        log("已点击登录，等待安全验证...")
        time.sleep(1.5)
        shot(driver, "02_after_submit")

        kind, geo = cs.wait_for_captcha(driver, timeout=12)
        if not kind:
            st = cs.read_status(driver)
            log("没有出现验证码，当前 URL: {}".format(driver.current_url))
            log("页面状态: {}".format(st))
            shot(driver, "03_no_captcha")
            return 0

        log("检测到验证类型: {}".format(kind))
        if geo:
            log("滑块几何: 轨道 {:.0f}px / 滑块 {:.0f}px / 需移动 {:.0f}px".format(
                geo.get("trackW", 0), geo.get("sliderW", 0), geo.get("distance", 0)))
        shot(driver, "03_captcha")

        t0 = time.time()
        result = cs.solve_captcha(driver, log=log, kind=kind, max_attempts=4)
        log("结果: {}（耗时 {:.1f}s）".format(result, time.time() - t0))
        time.sleep(2)
        shot(driver, "04_after_solve")

        st = cs.read_status(driver)
        log("结束状态: {}".format(st))
        log("当前 URL: {}".format(driver.current_url))

        # 顺带看看后续页面有没有密码错误提示
        try:
            body = driver.find_element(By.TAG_NAME, "body").text
            for kw in ("密码不正确", "密码错误", "登录失败", "验证失败"):
                if kw in body:
                    log("页面提示: {}".format(kw))
        except Exception:
            pass

        return 0 if result == "passed" else 1
    finally:
        if not args.keep:
            try:
                driver.quit()
            except Exception:
                pass
        else:
            log("已保留浏览器窗口，可手动检查")


if __name__ == "__main__":
    sys.exit(main())
