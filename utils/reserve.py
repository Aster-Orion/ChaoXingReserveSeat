import json
import random
import requests
import re
import time
import logging
import datetime
from utils import (
    AES_Encrypt,
    generate_captcha_key,
    enc,
    verify_param,
)
from urllib3.exceptions import InsecureRequestWarning
import os


def get_date(day_offset: int = 0):
    tz_beijing = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(tz_beijing).date()
    offset_day = today + datetime.timedelta(days=day_offset)
    tomorrow = offset_day.strftime("%Y-%m-%d")
    return tomorrow


class reserve:
    def __init__(
        self,
        sleep_time=0.2,
        max_attempt=50,
        enable_slider=False,
        reserve_next_day=False,
    ):
        self.login_page = (
            "https://passport2.chaoxing.com/mlogin?loginType=1&newversion=true&fid="
        )
        self.url = (
            "https://office.chaoxing.com/front/third/apps/seat/code?id={}&seatNum={}"
        )
        self.submit_url = "https://office.chaoxing.com/data/apps/seat/submit"
        self.seat_url = "https://office.chaoxing.com/data/apps/seat/getusedtimes"
        self.login_url = "https://passport2.chaoxing.com/fanyalogin"
        self.token = ""
        self.success_times = 0
        self.fail_dict = []
        self.submit_msg = []
        self.requests = requests.session()
        self.token_pattern = re.compile("token = '(.*?)'")
        self.headers = {
            "Referer": "https://office.chaoxing.com/",
            "Host": "captcha.chaoxing.com",
            "Pragma": "no-cache",
            "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        }
        self.login_headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "accept-encoding": "gzip, deflate, br, zstd",
            "cache-control": "no-cache",
            "Connection": "keep-alive",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 10_3_1 like Mac OS X) AppleWebKit/603.1.3 (KHTML, like Gecko) Version/10.0 Mobile/14E304 Safari/602.1 wechatdevtools/1.05.2109131 MicroMessenger/8.0.5 Language/zh_CN webview/16364215743155638",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Host": "passport2.chaoxing.com",
        }

        self.sleep_time = sleep_time
        self.max_attempt = max_attempt
        self.enable_slider = enable_slider
        self.reserve_next_day = reserve_next_day
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    def reset_office_headers(self):
        """登录完成后切换为普通网页请求头，避免残留登录接口请求头。"""
        self.requests.headers.clear()
        self.requests.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": "https://office.chaoxing.com/",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Connection": "keep-alive",
            }
        )

    def warmup_office_home(self):
        """只预热 office 域名连接；不提前访问会产生 token 的预约页面。"""
        try:
            response = self.requests.get(
                "https://office.chaoxing.com/",
                timeout=3,
                verify=False,
            )
            logging.info(
                f"[prepare] office首页热启动 HTTP={response.status_code}, "
                f"cookie数量={len(self.requests.cookies)}"
            )
        except requests.RequestException as error:
            logging.warning(
                f"[prepare] office首页热启动失败，不影响正式提交：{error}"
            )

    def _get_page_token(
        self,
        url,
        require_value=False,
        deadline_hms=None,
    ):
        """获取页面中的 token 与 algorithm；采用短重试并保存诊断页面。"""

        def extract_named_value(html_text, names):
            for name in names:
                escaped_name = re.escape(name)
                patterns = (
                    rf"""<input\b
                         (?=[^>]*(?:id|name)\s*=\s*["']{escaped_name}["'])
                         [^>]*\bvalue\s*=\s*["']([^"']+)["']""",
                    rf"""(?:["']?{escaped_name}["']?)
                         \s*[:=]\s*["']([^"']+)["']""",
                )
                for pattern in patterns:
                    match = re.search(
                        pattern,
                        html_text,
                        flags=re.IGNORECASE | re.VERBOSE,
                    )
                    if match:
                        return match.group(1)
            return ""

        def deadline_reached():
            if not deadline_hms:
                return False
            tz_beijing = datetime.timezone(datetime.timedelta(hours=8))
            now_hms = datetime.datetime.now(tz_beijing).strftime("%H:%M:%S")
            return now_hms >= deadline_hms

        fetch_headers = {
            "Referer": "https://office.chaoxing.com/",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

        # 外层还会继续尝试，因此这里必须短重试，不能阻塞几分钟。
        max_retries = 3
        base_delay = 0.15

        for attempt in range(1, max_retries + 1):
            if deadline_reached():
                logging.warning(
                    f"[token] 已到截止时间 {deadline_hms}，停止获取token"
                )
                return "", ""

            try:
                logging.warning(
                    "[submit-debug] headers=%s",
                    self.requests.headers
                )
                
                logging.warning(
                    "[submit-debug] cookies=%s",
                    self.requests.cookies.get_dict()
                )
                
                logging.warning(
                    "[submit-debug] params=%s",
                    parm
                )
                response = self.requests.get(
                    url=url,
                    headers=fetch_headers,
                    timeout=8,
                    verify=False,
                    allow_redirects=True,
                )
            except requests.RequestException as error:
                logging.warning(
                    f"[token] 第{attempt}次请求异常：{error}"
                )
                if attempt < max_retries:
                    time.sleep(base_delay * attempt)
                continue

            html = response.text
            html_len = len(html)
            content_type = response.headers.get("Content-Type", "")

            if response.status_code != 200:
                logging.warning(
                    f"[token] 第{attempt}次 HTTP={response.status_code}, "
                    f"final_url={response.url}"
                )
                if attempt < max_retries:
                    time.sleep(base_delay * attempt)
                continue

            token = extract_named_value(
                html,
                ("submit_enc", "submitEnc"),
            )
            value = ""
            if require_value:
                value = extract_named_value(
                    html,
                    ("algorithm",),
                )

            if token:
                sign_mode = "dynamic" if value else "legacy"
                logging.info(
                    f"[token] 第{attempt}次成功, "
                    f"token_len={len(token)}, value_len={len(value)}, "
                    f"sign_mode={sign_mode}, url={response.url}"
                )
                return token, value

            script_sources = re.findall(
                r"""<script[^>]+src=["']([^"']+)["']""",
                html,
                flags=re.IGNORECASE,
            )
            diagnostic_keywords = {
                keyword: keyword.lower() in html.lower()
                for keyword in (
                    "submit_enc",
                    "algorithm",
                    "fetch(",
                    "axios",
                    "captcha",
                    "验证码",
                    "登录",
                    "异常",
                )
            }

            logging.warning(
                f"[token] 第{attempt}次未找到有效token/value, "
                f"HTTP={response.status_code}, len={html_len}, "
                f"content_type={content_type}, final_url={response.url}, "
                f"cookies={len(self.requests.cookies)}, "
                f"scripts={script_sources[:8]}, "
                f"keywords={diagnostic_keywords}"
            )

            if attempt == 1:
                debug_directory = os.path.join(
                    os.getcwd(),
                    "debug_pages",
                )
                os.makedirs(debug_directory, exist_ok=True)
                timestamp = time.time_ns()
                html_path = os.path.join(
                    debug_directory,
                    f"token_fail_{timestamp}.html",
                )
                metadata_path = os.path.join(
                    debug_directory,
                    f"token_fail_{timestamp}.json",
                )

                try:
                    with open(html_path, "w", encoding="utf-8") as file:
                        file.write(html)
                    with open(
                        metadata_path,
                        "w",
                        encoding="utf-8",
                    ) as file:
                        json.dump(
                            {
                                "status_code": response.status_code,
                                "final_url": response.url,
                                "content_type": content_type,
                                "html_length": html_len,
                                "history": [
                                    item.status_code
                                    for item in response.history
                                ],
                                "scripts": script_sources,
                                "keywords": diagnostic_keywords,
                                "cookies": self.requests.cookies.get_dict(),
                            },
                            file,
                            ensure_ascii=False,
                            indent=2,
                        )
                    logging.warning(
                        f"[token] 诊断文件已保存：{html_path}"
                    )
                except OSError as error:
                    logging.warning(
                        f"[token] 保存诊断文件失败：{error}"
                    )

            if attempt < max_retries:
                time.sleep(base_delay * attempt)

        logging.error(
            f"[token] 连续{max_retries}次未获取到token, url={url}"
        )
        return "", ""

    def get_login_status(self):
        logging.info("[login] 获取登录页 Cookie...")
        self.requests.headers = self.login_headers
        resp = self.requests.get(url=self.login_page, verify=False)
        logging.info(f"[login] 登录页 HTTP {resp.status_code}, cookie数量={len(self.requests.cookies)}")

    def login(self, username, password):
        enc_username = AES_Encrypt(username)
        enc_password = AES_Encrypt(password)
        parm = {
            "fid": -1,
            "uname": enc_username,
            "password": enc_password,
            "refer": "http%3A%2F%2Foffice.chaoxing.com%2Ffront%2Fthird%2Fapps%2Fseat%2Fcode%3Fid%3D4219%26seatNum%3D380",
            "t": True,
        }
        resp = self.requests.post(url=self.login_url, params=parm, verify=False)
        obj = resp.json()
        if obj.get("status"):
            logging.info(f"[login] 用户 {username} 登录成功")
            return (True, "")
        else:
            msg = obj.get("msg2", obj.get("msg", "未知错误"))
            logging.warning(f"[login] 用户 {username} 登录失败: {msg}")
            return (False, msg)

    def roomid(self, encode):
        url = f"https://office.chaoxing.com/data/apps/seat/room/list?cpage=1&pageSize=100&firstLevelName=&secondLevelName=&thirdLevelName=&deptIdEnc={encode}"
        json_data = self.requests.get(url=url).content.decode("utf-8")
        ori_data = json.loads(json_data)
        for i in ori_data["data"]["seatRoomList"]:
            info = f'{i["firstLevelName"]}-{i["secondLevelName"]}-{i["thirdLevelName"]} id为：{i["id"]}'
            print(info)

    def resolve_captcha(self):
        logging.info(f"Start to resolve captcha token")
        captcha_token, bg, tp = self.get_slide_captcha_data()
        logging.info(f"Successfully get prepared captcha_token {captcha_token}")
        logging.info(f"Captcha Image URL-small {tp}, URL-big {bg}")
        x = self.x_distance(bg, tp)
        logging.info(f"Successfully calculate the captcha distance {x}")
        params = {
            "callback": "jQuery33109180509737430778_1716381333117",
            "captchaId": "42sxgHoTPTKbt0uZxPJ7ssOvtXr3ZgZ1",
            "type": "slide",
            "token": captcha_token,
            "textClickArr": json.dumps([{"x": x}]),
            "coordinate": json.dumps([]),
            "runEnv": "10",
            "version": "1.1.18",
            "_": int(time.time() * 1000),
        }
        response = self.requests.get(
            f"https://captcha.chaoxing.com/captcha/check/verification/result",
            params=params,
            headers=self.headers,
        )
        text = response.text.replace(
            "jQuery33109180509737430778_1716381333117(", ""
        ).replace(")", "")
        data = json.loads(text)
        logging.info(f"Successfully resolve the captcha token {data}")
        try:
            validate_val = json.loads(data["extraData"])["validate"]
            return validate_val
        except KeyError as e:
            logging.info("Can't load validate value. Maybe server return mistake.")
            return ""

    def get_slide_captcha_data(self):
        url = "https://captcha.chaoxing.com/captcha/get/verification/image"
        timestamp = int(time.time() * 1000)
        capture_key, token = generate_captcha_key(timestamp)
        referer = f"https://office.chaoxing.com/front/third/apps/seat/code?id=3993&seatNum=0199"
        params = {
            "callback": f"jQuery33107685004390294206_1716461324846",
            "captchaId": "42sxgHoTPTKbt0uZxPJ7ssOvtXr3ZgZ1",
            "type": "slide",
            "version": "1.1.18",
            "captchaKey": capture_key,
            "token": token,
            "referer": referer,
            "_": timestamp,
            "d": "a",
            "b": "a",
        }
        response = self.requests.get(url=url, params=params, headers=self.headers)
        content = response.text
        data = content.replace(
            "jQuery33107685004390294206_1716461324846(", ")"
        ).replace(")", "")
        data = json.loads(data)
        captcha_token = data["token"]
        bg = data["imageVerificationVo"]["shadeImage"]
        tp = data["imageVerificationVo"]["cutoutImage"]
        return captcha_token, bg, tp

    def x_distance(self, bg, tp):
        import numpy as np
        import cv2

        def cut_slide(slide):
            slider_array = np.frombuffer(slide, np.uint8)
            slider_image = cv2.imdecode(slider_array, cv2.IMREAD_UNCHANGED)
            slider_part = slider_image[:, :, :3]
            mask = slider_image[:, :, 3]
            mask[mask != 0] = 255
            x, y, w, h = cv2.boundingRect(mask)
            cropped_image = slider_part[y : y + h, x : x + w]
            return cropped_image

        c_captcha_headers = {
            "Referer": "https://office.chaoxing.com/",
            "Host": "captcha-b.chaoxing.com",
            "Pragma": "no-cache",
            "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        }
        bgc, tpc = self.requests.get(bg, headers=c_captcha_headers), self.requests.get(
            tp, headers=c_captcha_headers
        )
        bg, tp = bgc.content, tpc.content
        bg_img = cv2.imdecode(np.frombuffer(bg, np.uint8), cv2.IMREAD_COLOR)
        tp_img = cut_slide(tp)
        bg_edge = cv2.Canny(bg_img, 100, 200)
        tp_edge = cv2.Canny(tp_img, 100, 200)
        bg_pic = cv2.cvtColor(bg_edge, cv2.COLOR_GRAY2RGB)
        tp_pic = cv2.cvtColor(tp_edge, cv2.COLOR_GRAY2RGB)
        res = cv2.matchTemplate(bg_pic, tp_pic, cv2.TM_CCOEFF_NORMED)
        _, _, _, max_loc = cv2.minMaxLoc(res)
        tl = max_loc
        return tl[0]

    def submit(self, times, roomid, seatid, action):
        for seat in seatid:
            suc = False
            remaining = self.max_attempt
            while not suc and remaining > 0:
                token, value = self._get_page_token(
                    self.url.format(roomid, seat), require_value=True
                )
                if not token:
                    logging.warning(f"[submit] seat={seat} token为空，等待重试...")
                    time.sleep(self.sleep_time)
                    remaining -= 1
                    continue
                captcha = self.resolve_captcha() if self.enable_slider else ""
                if captcha:
                    logging.info(f"[submit] 滑块验证码: {captcha[:20]}...")
                suc, _ = self.get_submit(
                    self.submit_url,
                    times=times,
                    token=token,
                    roomid=roomid,
                    seatid=seat,
                    captcha=captcha,
                    action=action,
                    value=value,
                )
                if suc:
                    return suc
                time.sleep(self.sleep_time)
                remaining -= 1
            logging.warning(f"[submit] seat={seat} 已耗尽所有尝试次数")
        return False

    def get_submit(
        self, url, times, token, roomid, seatid, captcha="", action=False, value=""
    ):
        delta_day = 1 if self.reserve_next_day else 0
        tz_beijing = datetime.timezone(datetime.timedelta(hours=8))
        beijing_today = datetime.datetime.now(tz_beijing)
        day = beijing_today.date() + datetime.timedelta(days=delta_day)
        parm = {
            "roomId": roomid,
            "startTime": times[0],
            "endTime": times[1],
            "day": str(day),
            "seatNum": seatid,
            "captcha": captcha,
            "token": token,
            "type": "1",
            "verifyData": "1",
        }
        logging.info(f"[submit] 请求参数 roomId={roomid} seatNum={seatid} "
                     f"day={day} {times[0]}~{times[1]}")
        if value:
            parm["enc"] = verify_param(parm, value)
            logging.info("[submit] 签名模式=dynamic")
        else:
            logging.warning(
                "[submit] 未发现algorithm，尝试原提交参数"
            )
        
        parm["enc"] = verify_param(parm, value)
        logging.info("[submit] 签名模式=dynamic(algorithm)")
        resp = self.requests.post(url=url, params=parm, verify=True)
        html = resp.content.decode("utf-8")
        try:
            result = json.loads(html)
        except json.JSONDecodeError:
            logging.error(f"[submit] 响应非JSON, HTTP={resp.status_code}, 内容={html[:200]}")
            return False, ""
        self.submit_msg.append(f"{times[0]}~{times[1]}: {result}")
        success = result.get("success", False)
        msg = result.get("msg", "")
        if success:
            logging.info(f"[submit] ✅ 预约成功! {result}")
        else:
            logging.warning(f"[submit] ❌ 预约失败: {result}")
        return success, msg
