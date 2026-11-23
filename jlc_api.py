# -*- coding: utf-8 -*-
"""
嘉立创签到接口客户端（纯 requests，不依赖浏览器）。

设计原则：
- 浏览器只负责登录、拿凭证；
- 积分（oshwhub.com）和金豆（m.jlc.com）签到全部走这里；
- 凭证失效时抛 AuthExpired，由调用方决定是否重新登录。

两个平台用的凭证不同：
- 开源平台积分：Cookie（关键是 oshwhub_session）
- 嘉立创金豆：请求头 X-JLC-AccessToken（mobile 端 token，不含 Cookie 也能用）
"""

import time

import requests

# 开源平台用桌面 UA；之前实测无头 Chrome 的 HeadlessChrome UA 会被瑞数 WAF 返回 418
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 金豆接口沿用 App 内嵌 H5 的 UA（m.jlc.com 对移动端最宽松）
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "Html5Plus/1.0 (Immersed/20) JlcMobileApp"
)


class AuthExpired(Exception):
    """凭证失效，需要重新登录"""


class ApiError(Exception):
    """接口调用失败（网络、WAF、业务错误等），不一定需要重新登录"""


def _reason_of(resp):
    """把 401 响应体里的 msg 提取出来，日志里能看到服务端真实原因"""
    try:
        payload = resp.json()
    except ValueError:
        text = (resp.text or "").strip().replace("\n", " ")
        return text[:80] or "无响应体"
    if isinstance(payload, dict):
        for key in ("msg", "message", "error", "errorMsg"):
            value = payload.get(key)
            if value:
                return str(value)[:80]
    return "未登录或凭证失效"


class _BaseClient(object):
    def __init__(self, index=0, logger=None, timeout=15, retries=2):
        self.index = index
        self.timeout = timeout
        self.retries = retries
        self._logger = logger
        self.session = requests.Session()

    def log(self, msg):
        if self._logger:
            self._logger(msg)
        else:
            print(msg, flush=True)

    def _send(self, method, url, **kwargs):
        """带轻量重试的请求，返回 (status_code, payload)"""
        last_error = None
        for attempt in range(self.retries):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as e:
                last_error = "请求异常: %s" % str(e).splitlines()[0][:160]
                if attempt + 1 < self.retries:
                    time.sleep(1.0 + attempt)
                    continue
                raise ApiError(last_error)

            if resp.status_code == 401:
                raise AuthExpired("接口返回 401（%s）" % _reason_of(resp))
            if resp.status_code == 418:
                raise ApiError("418 被 WAF 拦截（请求头指纹被判定为自动化）")
            if resp.status_code >= 500:
                last_error = "HTTP %s" % resp.status_code
                if attempt + 1 < self.retries:
                    time.sleep(1.0 + attempt)
                    continue
                raise ApiError(last_error)
            if resp.status_code != 200:
                raise ApiError("HTTP %s: %s" % (resp.status_code, resp.text[:120]))

            try:
                payload = resp.json()
            except ValueError:
                raise ApiError("返回内容不是 JSON: %s" % resp.text[:120])

            code = payload.get("code")
            if code in (401, 403):
                raise AuthExpired("接口返回 %s，凭证失效" % code)
            return payload

        raise ApiError(last_error or "未知请求错误")


class OshwhubClient(_BaseClient):
    """开源平台（oshwhub.com）积分签到"""

    BASE = "https://oshwhub.com"

    def __init__(self, cookies, index=0, logger=None, cookie_header="", ua=""):
        super(OshwhubClient, self).__init__(index=index, logger=logger)
        self.cookies = dict(cookies or {})
        self.session.headers.update({
            "User-Agent": ua or DESKTOP_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://oshwhub.com/sign_in",
            "Origin": "https://oshwhub.com",
            # 站内接口是 XHR 调用，老脚本都带这个头；缺了会被当成非站内请求
            "X-Requested-With": "XMLHttpRequest",
        })
        if cookie_header:
            # 直接用浏览器发出去的那串 Cookie：既能带全 WAF 会话 Cookie，
            # 也不会因为 cookiejar 的域名/路径匹配规则漏发。
            self.session.headers["Cookie"] = cookie_header
        else:
            for name, value in self.cookies.items():
                self.session.cookies.set(name, value, domain="oshwhub.com")

    def _api(self, method, path):
        url = self.BASE + path
        payload = self._send(method, url)
        if not payload.get("success"):
            msg = payload.get("msg") or payload.get("message") or "success=false"
            raise ApiError(str(msg))
        return payload.get("result")

    def get_user(self):
        """用户信息：nickname / points / customerCode"""
        result = self._api("GET", "/api/users")
        if not isinstance(result, dict):
            raise ApiError("用户信息格式异常")
        return result

    def get_sign_profile(self):
        """签到信息：total_point / isTodaySignIn / goodGiftStatus"""
        result = self._api("GET", "/api/users/getSignInProfile")
        if not isinstance(result, dict):
            raise ApiError("签到信息格式异常")
        return result

    def sign_in(self):
        """
        提交签到。返回 True=本次签到成功，False=今天已经签到过。
        接口约定：success=true 且 result=true/false。
        """
        result = self._api("POST", "/api/users/signIn")
        return bool(result)

    # ---- 高层流程 ----

    def run(self):
        """
        积分签到完整流程，返回：
        {
          'status': '签到成功' / '已签到过' / '签到失败',
          'success': bool,          # 是否算签到完成（含已签到）
          'initial_points': int,
          'final_points': int,
          'points_reward': int,
          'nickname': str,
          'customer_code': str,
          'message': str,
        }
        """
        user = self.get_user()
        nickname = user.get("nickname") or ""
        customer_code = user.get("customerCode") or ""
        initial = _to_number(user.get("points"))

        profile = self.get_sign_profile()
        already = bool(profile.get("isTodaySignIn"))

        if not already:
            signed = self.sign_in()

            if not signed:
                # 接口返回 result=false：今天其实已经签过了（并发/前端状态滞后）
                final = _to_number(self.get_user().get("points"))
                return {
                    "status": "已签到过",
                    "success": True,
                    "initial_points": initial,
                    "final_points": final,
                    "points_reward": final - initial,
                    "nickname": nickname,
                    "customer_code": customer_code,
                    "message": "今天已签到过",
                }

            # 签到后积分可能有短暂延迟，最多再看两次
            final = initial
            for wait in (0, 1.5, 2.5):
                if wait:
                    time.sleep(wait)
                final = _to_number(self.get_user().get("points"))
                if final != initial:
                    break
            return {
                "status": "签到成功",
                "success": True,
                "initial_points": initial,
                "final_points": final,
                "points_reward": final - initial,
                "nickname": nickname,
                "customer_code": customer_code,
                "message": "签到成功",
            }

        # 已签到，只刷新一次积分
        final = _to_number(self.get_user().get("points"))
        return {
            "status": "已签到过",
            "success": True,
            "initial_points": initial,
            "final_points": final,
            "points_reward": final - initial,
            "nickname": nickname,
            "customer_code": customer_code,
            "message": "今天已签到过",
        }


class JlcBeanClient(_BaseClient):
    """嘉立创金豆签到（m.jlc.com）"""

    BASE = "https://m.jlc.com"

    def __init__(self, access_token, index=0, logger=None, secretkey=""):
        super(JlcBeanClient, self).__init__(index=index, logger=logger)
        self.access_token = access_token
        self.session.headers.update({
            "User-Agent": MOBILE_UA,
            "x-jlc-clienttype": "WEB",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "x-jlc-accesstoken": access_token or "",
            "Referer": "https://m.jlc.com/mapp/pages/my/index",
        })
        if secretkey:
            self.session.headers["secretkey"] = secretkey

    def _api(self, path):
        url = self.BASE + path
        payload = self._send("GET", url)
        if not payload.get("success"):
            msg = str(payload.get("message") or payload.get("msg") or "success=false")
            if "登录" in msg or "未授权" in msg or "token" in msg.lower():
                raise AuthExpired(msg)
            raise ApiError(msg)
        return payload.get("data")

    def assets(self):
        """个人资产：customerCode / integralVoucher（金豆数）"""
        data = self._api("/api/appPlatform/center/assets/selectPersonalAssetsInfo")
        return data if isinstance(data, dict) else {}

    def integral(self):
        """金豆数量（旧接口，作为 assets 的兜底）"""
        data = self._api("/api/activity/front/getCustomerIntegral")
        if isinstance(data, dict):
            return _to_number(data.get("integralVoucher"))
        return 0

    def sign_config(self):
        """签到配置：haveSignIn 表示今天是否已签"""
        data = self._api("/api/activity/sign/getCurrentUserSignInConfig")
        return data if isinstance(data, dict) else {}

    def sign_in(self):
        """提交签到，返回 data（gainNum 为本次获得金豆）"""
        data = self._api("/api/activity/sign/signIn?source=3")
        return data if isinstance(data, dict) else {}

    def receive_voucher(self):
        """第七天/额外奖励领取"""
        data = self._api("/api/activity/sign/receiveVoucher")
        return data if isinstance(data, dict) else {}

    # ---- 高层流程 ----

    def run(self):
        """
        金豆签到完整流程，返回字段与 OshwhubClient.run 对齐。
        """
        assets = self.assets()
        initial = _to_number(assets.get("integralVoucher"))
        customer_code = assets.get("customerCode") or ""

        config = self.sign_config()
        already = bool(config.get("haveSignIn"))

        has_extra_reward = False
        if already:
            status = "已签到过"
            message = "今天已签到过"
        else:
            data = self.sign_in()
            gain = _to_number(data.get("gainNum"))
            day_status = data.get("status")
            try:
                positive_day = int(day_status) > 0
            except (TypeError, ValueError):
                positive_day = day_status is True

            if gain:
                status = "签到成功"
                message = "签到成功，获得%s个金豆" % _fmt(gain)
            elif positive_day:
                # 签到日没有直接给金豆，通常是第七天/整月奖励，需要再领一次
                try:
                    self.receive_voucher()
                    has_extra_reward = True
                    status = "签到成功"
                    message = "签到成功，已领取额外奖励"
                except ApiError as e:
                    status = "签到失败"
                    message = "签到接口未发放金豆，领取奖励失败: %s" % e
            else:
                # 既没金豆也不像奖励日：复核一次，可能其实今天已经签过
                if self.sign_config().get("haveSignIn"):
                    status = "已签到过"
                    message = "今天已签到过"
                else:
                    status = "签到失败"
                    message = "签到接口未发放金豆，账号状态异常"

            if status == "签到失败":
                return {
                    "status": status,
                    "success": False,
                    "initial_jindou": initial,
                    "final_jindou": initial,
                    "jindou_reward": 0,
                    "has_extra_reward": has_extra_reward,
                    "customer_code": customer_code,
                    "message": message,
                }

        # 刷新金豆数量
        final = initial
        for wait in (0, 1.5):
            if wait:
                time.sleep(wait)
            try:
                final = _to_number(self.assets().get("integralVoucher"))
            except AuthExpired:
                raise
            except ApiError:
                try:
                    final = self.integral()
                except (ApiError, AuthExpired):
                    final = initial
            if final != initial:
                break

        return {
            "status": status,
            "success": True,
            "initial_jindou": initial,
            "final_jindou": final,
            "jindou_reward": final - initial,
            "has_extra_reward": has_extra_reward,
            "customer_code": customer_code,
            "message": message,
        }


def _to_number(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _fmt(value):
    try:
        return "{:g}".format(float(value))
    except (TypeError, ValueError):
        return str(value)
