# -*- coding: utf-8 -*-
"""
本地自测脚本：走和主脚本完全相同的路径（同一个 create_driver / ensure_login_page / 滑块逻辑），
用来在本地确认「能不能进登录页、滑块能不能过」，不用等到 Actions 跑完才知道。

用法（在项目根目录，用 .venv 里的解释器）：

    # 1) 只测登录入口 + 滑块（假账号，最后一定提示密码错误，属正常）
    .venv\\Scripts\\python.exe test_captcha.py --fake

    # 2) 看得到浏览器窗口，方便肉眼观察
    .venv\\Scripts\\python.exe test_captcha.py --fake --headful

    # 3) 用真实账号跑（不写进命令行，避免留在历史记录里）
    set JLC_USERNAME=你的账号
    set JLC_PASSWORD=你的密码
    .venv\\Scripts\\python.exe test_captcha.py

    # 4) 只验证「能否从签到页进入登录页」，不填账号密码（最快，约 10 秒）
    .venv\\Scripts\\python.exe test_captcha.py --only-entry

常用参数：
    --fake        用假账号，只测到滑块
    --headful     显示浏览器窗口（等价 JLC_HEADLESS=0）
    --browser     指定 chrome / edge，默认读 JLC_BROWSER，再默认 chrome
    --only-entry  只验证登录入口，不提交登录
    --keep        结束后不关闭浏览器
"""

import argparse
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jlc

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


def split_env(name):
    raw = os.getenv(name, "") or ""
    return [x.strip() for x in re.split(r"[,\n;]+", raw) if x.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--browser", default="", choices=["", "chrome", "edge"])
    ap.add_argument("--headful", action="store_true", help="显示浏览器窗口")
    ap.add_argument("--headless", action="store_true", help="强制无头（默认沿用 JLC_HEADLESS）")
    ap.add_argument("--fake", action="store_true", help="用假账号，只测登录入口和滑块")
    ap.add_argument("--only-entry", action="store_true", help="只验证能否进入登录页")
    ap.add_argument("--keep", action="store_true", help="结束后不关浏览器")
    args = ap.parse_args()

    # 让测试脚本和主脚本用同一套浏览器配置
    if args.browser:
        jlc.BROWSER = args.browser
    if args.headful:
        jlc.HEADLESS = False
    elif args.headless:
        jlc.HEADLESS = True

    account = args.account or (split_env("JLC_USERNAME") or [""])[0]
    password = args.password or (split_env("JLC_PASSWORD") or [""])[0]
    if args.fake or not account or not password:
        if not args.only_entry:
            account = "13800138000"
            password = "FakePass_{}".format(random.randint(1000, 9999))
            log("使用假账号测试：{}（最后提示密码错误属正常）".format(account))

    log("浏览器={} 无头={}".format(jlc.BROWSER, jlc.HEADLESS))
    driver = jlc.create_driver()
    try:
        # 1. 真实入口：签到页 -> 登录页
        log("打开 https://oshwhub.com/sign_in")
        ok, driver = jlc.ensure_login_page(driver, 1)
        shot(driver, "01_login_entry")
        if not ok:
            log("结论: 未能进入登录页（当前 URL: {}）".format(driver.current_url))
            return 1
        log("已进入登录页: {}".format(driver.current_url[:90]))

        if args.only_entry:
            log("结论: 登录入口正常")
            return 0

        # 2. 填账号密码
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        wait = WebDriverWait(driver, 20)
        try:
            wait.until(EC.element_to_be_clickable(
                (By.XPATH, '//button[contains(text(),"账号登录")]'))).click()
            time.sleep(1)
            log("已切换到账号登录")
        except Exception as e:
            log("切换账号登录 Tab 失败: {}".format(str(e).splitlines()[0][:100]))

        user_input = wait.until(EC.presence_of_element_located(
            (By.XPATH, '//input[@placeholder="请输入手机号码 / 客户编号 / 邮箱"]')))
        user_input.clear()
        user_input.send_keys(account)

        pwd_input = wait.until(EC.presence_of_element_located(
            (By.XPATH, '//input[@type="password"]')))
        pwd_input.clear()
        pwd_input.send_keys(password)
        log("已填写账号密码")

        driver.find_element(By.CSS_SELECTOR, "button.submit").click()
        log("已点击登录，等待安全验证...")
        time.sleep(1.5)
        shot(driver, "02_after_submit")

        # 3. 滑块
        kind, geo = jlc.captcha_solver.wait_for_captcha(driver, timeout=12) if jlc.captcha_solver else (None, None)
        if not kind:
            st = jlc.captcha_solver.read_status(driver) if jlc.captcha_solver else None
            log("没有出现验证码，当前 URL: {}".format(driver.current_url))
            log("页面状态: {}".format(st))
            shot(driver, "03_no_captcha")
            return 1

        log("检测到验证类型: {}".format(kind))
        if geo:
            log("滑块几何: 轨道 {:.0f}px / 滑块 {:.0f}px / 需移动 {:.0f}px".format(
                geo.get("trackW", 0), geo.get("sliderW", 0), geo.get("distance", 0)))
        shot(driver, "03_captcha")

        t0 = time.time()
        result = jlc.captcha_solver.solve_captcha(driver, log=log, kind=kind, max_attempts=4)
        log("滑块结果: {}（耗时 {:.1f}s）".format(result, time.time() - t0))
        time.sleep(2)
        shot(driver, "04_after_solve")

        body = ""
        try:
            body = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            pass
        hint = next((kw for kw in ("密码不正确", "密码错误", "登录失败", "验证失败")
                     if kw in body), "")
        if hint:
            log("页面提示: {}".format(hint))
        log("当前 URL: {}".format(driver.current_url[:90]))

        # 4. 结论
        if result != "passed":
            log("结论: 滑块未通过")
            return 1
        if args.fake:
            log("结论: 滑块已通过（假账号提示「{}」属正常）".format(hint or "验证通过"))
        else:
            log("结论: 登录流程已通过验证码，当前页面提示「{}」".format(hint or "无"))
        return 0
    finally:
        if args.keep:
            log("已保留浏览器窗口，可手动检查")
        else:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
