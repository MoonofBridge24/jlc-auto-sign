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

import calendar
import datetime
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

    # 积分可能落在这几个字段上，不同接口/版本不一样
    POINT_KEYS = ("total_point", "points", "totalPoint", "point")

    def read_points(self, *sources):
        """
        从若干 (标签, dict) 里找积分，返回 (值, 来源)；都读不到返回 (None, "")。

        优先 getSignInProfile.total_point（官方示例里这个字段稳定存在），
        再退回 /api/users 的 points。之前只读 users.points，接口没这个字段时就一直是 0。
        """
        for label, data in sources:
            if not isinstance(data, dict):
                continue
            for key in self.POINT_KEYS:
                if data.get(key) is not None:
                    return _to_number(data.get(key)), "%s.%s" % (label, key)
        return None, ""

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

    # ---- 签到好礼（七日好礼 / 月度好礼）----

    def gift_info(self):
        """可领取的好礼：{sevenDays: {uuid, name}, monthEnd: {uuid, name}}"""
        result = self._api("GET", "/api/gift/goodGift")
        return result if isinstance(result, dict) else {}

    def claim_good_gift(self, gift_uuid):
        """
        领取好礼，返回接口的 result 码：0=领取失败，1=优惠券，2=积分。
        （前端「点击领取」走的就是这个 GET，见 _next 包里 /api/gift/goodGift/{uuid}）
        """
        result = self._api("GET", "/api/gift/goodGift/%s" % gift_uuid)
        if isinstance(result, bool):
            return 2 if result else 0
        return _to_number(result)

    # (key, 名称, goodGiftStatus 里的已领标记, 所需的连续签到天数来源)
    GIFT_KINDS = (
        ("seven", "七日好礼", "sevenGoodGiftRecord", "week_signIn_days"),
        ("month", "月度好礼", "monthGoodGiftRecord", "month_signIn_days"),
    )

    def claim_good_gifts(self, profile=None):
        """
        自动领取签到好礼。

        前端条件（packages/next 里的 signIn 组件）：
        - 七日好礼：`week_signIn_days >= 7` 且 `goodGiftStatus.sevenGoodGiftRecord == 0`；
        - 月度好礼：`month_signIn_days >= 当月天数` 且 `monthGoodGiftRecord == 0`。
        这里按同一条件判断，真正能不能领仍由服务端决定。

        返回 [{'name': '七日好礼', 'status': 'ok'/'fail', 'message': '领取积分'}]
        """
        if not isinstance(profile, dict):
            try:
                profile = self.get_sign_profile()
            except AuthExpired:
                raise
            except ApiError as e:
                self.log("好礼: 读取签到信息失败，跳过领取: %s" % e)
                return []

        gift_status = profile.get("goodGiftStatus") or {}
        need_days = {"seven": 7, "month": _days_in_month()}

        pending = []
        for key, name, record_key, days_key in self.GIFT_KINDS:
            if gift_status.get(record_key):
                # 已经领过，不重复请求
                continue
            days = _to_number(profile.get(days_key))
            if days < need_days[key]:
                continue
            pending.append((key, name))

        if not pending:
            return []

        try:
            info = self.gift_info()
        except AuthExpired:
            raise
        except ApiError as e:
            self.log("好礼: 获取可领取礼包失败: %s" % e)
            return [{"name": name, "status": "fail", "message": str(e)}
                    for _, name in pending]

        gifts = []
        for key, name in pending:
            node = info.get("sevenDays" if key == "seven" else "monthEnd") or {}
            uuid = node.get("uuid") or ""
            if not uuid:
                self.log("好礼: %s 当前没有可领取的礼包" % name)
                continue
            try:
                code = self.claim_good_gift(uuid)
            except AuthExpired:
                raise
            except ApiError as e:
                gifts.append({"name": name, "status": "fail", "message": str(e)})
                self.log("好礼: %s 领取失败: %s" % (name, e))
                continue

            if code == 2:
                gifts.append({"name": name, "status": "ok", "message": "领取积分"})
                self.log("好礼: %s 领取成功（积分）" % name)
            elif code == 1:
                gifts.append({"name": name, "status": "ok", "message": "领取优惠券"})
                self.log("好礼: %s 领取成功（优惠券）" % name)
            else:
                gifts.append({"name": name, "status": "fail",
                              "message": "接口返回 %s" % code})
                self.log("好礼: %s 领取失败（接口返回 %s）" % (name, code))
        return gifts

    # ---- 高层流程 ----

    def _points_now(self, default=0):
        """重新读一次积分：先签到信息，失败或没这个字段再看用户信息"""
        try:
            profile = self.get_sign_profile()
        except AuthExpired:
            raise
        except ApiError:
            profile = None

        value, _ = self.read_points(("getSignInProfile", profile))
        if value is None:
            try:
                value, _ = self.read_points(("users", self.get_user()))
            except AuthExpired:
                raise
            except ApiError:
                value = None
        return default if value is None else value

    def _sign_block_reasons(self, profile, user):
        """
        签到接口返回 false 时的可能原因，给用户可操作的方向。
        开源平台要求账号先设置昵称（系统自动生成的 eda_xxx 不算）。
        """
        reasons = []
        if str((user or {}).get("nickname") or "").startswith("eda_"):
            reasons.append("未设置昵称")
        if _to_number((profile or {}).get("userProjectCount")) == 0:
            reasons.append("还没有开源项目")
        return reasons or ["账号状态不满足签到条件"]

    def run(self):
        """
        积分签到完整流程，返回：
        {
          'status': '签到成功' / '已签到过' / '签到未生效' / '签到失败',
          'success': bool,          # 是否算签到完成（含已签到）
          'initial_points': int,
          'final_points': int,
          'points_reward': int,
          'nickname': str,
          'customer_code': str,
          'gifts': [{'name', 'status', 'message'}],   # 签到好礼领取结果
          'message': str,
        }
        """
        user = self.get_user()
        nickname = user.get("nickname") or ""
        customer_code = user.get("customerCode") or ""
        profile = self.get_sign_profile()
        already = bool(profile.get("isTodaySignIn"))

        # 积分以签到信息里的 total_point 为准，/api/users 的 points 作兜底
        initial, _ = self.read_points(
            ("getSignInProfile", profile), ("users", user))
        if initial is None:
            initial = 0
            self.log("积分: 接口里没有积分字段（users=%s；profile=%s）" % (
                "/".join(sorted(user)[:10]) or "空",
                "/".join(sorted(profile)[:10]) or "空"))

        status = "已签到过"
        message = "今天已签到过"
        if not already:
            if self.sign_in():
                status = "签到成功"
                message = "签到成功"
            else:
                # result=false 有两种可能：并发下今天其实已经签过，或签到被服务端拒绝。
                # 必须复核 profile——仍显示未签到就是真失败，绝不能当成「已签到过」
                # （之前就是这样把失败报成已签到，用户看到的是「已签到过，积分 0 → 0」）。
                try:
                    check = self.get_sign_profile()
                except (ApiError, AuthExpired):
                    check = None

                if check is not None and check.get("isTodaySignIn"):
                    status = "已签到过"
                    message = "今天已签到过（签到接口返回 false，复核已签到）"
                else:
                    view = check or profile
                    reasons = self._sign_block_reasons(view, user)
                    status = "签到未生效"
                    message = "签到接口返回 false，疑似%s" % "、".join(reasons)
                    self.log(
                        "积分: 签到未生效 —— result=false；isTodaySignIn=%s，周签到=%s，月签到=%s，"
                        "项目数=%s，昵称系统自动生成=%s" % (
                            view.get("isTodaySignIn"), view.get("week_signIn_days"),
                            view.get("month_signIn_days"), view.get("userProjectCount"),
                            "是" if str(nickname).startswith("eda_") else "否"))
                    return {
                        "status": status,
                        "success": False,
                        # 账号条件不满足，重试和重登都没意义，只会再撞一次限流
                        "non_retryable": True,
                        "initial_points": initial,
                        "final_points": initial,
                        "points_reward": 0,
                        "nickname": nickname,
                        "customer_code": customer_code,
                        "gifts": [],
                        "message": message,
                    }
        # sign_in() 返回 false 但复核发现已签到：按已签到处理

        # 好礼：签到后再读一次签到信息，拿最新的连续签到天数和领取状态
        gifts = []
        try:
            fresh = self.get_sign_profile()
        except AuthExpired:
            raise
        except ApiError as e:
            fresh = None
            self.log("好礼: 刷新签到信息失败，跳过领取: %s" % e)
        if fresh is not None:
            gifts = self.claim_good_gifts(fresh)

        # 积分到账可能有延迟；领过好礼时多等一会儿
        gift_claimed = any(g.get("status") == "ok" for g in gifts)
        rounds = (0, 1.5, 2.5) if (status == "签到成功" or gift_claimed) else (0,)
        final = initial
        for wait in rounds:
            if wait:
                time.sleep(wait)
            final = self._points_now(initial)
            if final != initial:
                break

        return {
            "status": status,
            "success": True,
            "initial_points": initial,
            "final_points": final,
            "points_reward": final - initial,
            "nickname": nickname,
            "customer_code": customer_code,
            "gifts": gifts,
            "message": message,
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


def _days_in_month():
    """当月天数：月度好礼要求签满整月"""
    today = datetime.date.today()
    return calendar.monthrange(today.year, today.month)[1]


def _to_number(value):
    """把接口里的数字转成 int；兼容 "1,240"、空串、None 这类脏数据"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        text = str(value).strip().replace(",", "").replace("，", "")
        if not text:
            return 0
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _fmt(value):
    try:
        return "{:g}".format(float(value))
    except (TypeError, ValueError):
        return str(value)
