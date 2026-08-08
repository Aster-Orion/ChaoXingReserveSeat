import json
import time
import random
import argparse
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

from utils import reserve, get_user_credentials

get_current_time = lambda action: (
    time.strftime("%H:%M:%S", time.localtime(time.time() + 8 * 3600))
    if action
    else time.strftime("%H:%M:%S", time.localtime(time.time()))
)
get_current_dayofweek = lambda action: (
    time.strftime("%A", time.localtime(time.time() + 8 * 3600))
    if action
    else time.strftime("%A", time.localtime(time.time()))
)

SLEEPTIME = 0.3

TARGET_TIME = os.getenv("TARGET_TIME", "18:40:00")
ENDTIME = os.getenv("ENDTIME", "19:54:20")


ENABLE_SLIDER = False
MAX_ATTEMPT = 6
RESERVE_NEXT_DAY = True
MAX_WORKERS = 1  # 最大并行线程数，可根据需要调整

WARMUP_SECONDS = int(os.getenv("WARMUP_SECONDS", "5"))

def hms_to_seconds(hms: str) -> int:
    """把 08:00:00 转成当天秒数。"""
    hour, minute, second = map(int, hms.split(":"))
    return hour * 3600 + minute * 60 + second


def wait_until_prepare(action: bool) -> None:
    """等待到目标时间前 WARMUP_SECONDS 秒，再开始登录。"""
    current_time = get_current_time(action)
    current_seconds = hms_to_seconds(current_time)
    target_seconds = hms_to_seconds(TARGET_TIME)
    wait_seconds = target_seconds - current_seconds

    if wait_seconds > WARMUP_SECONDS:
        logging.info(
            f"距离目标时间 {TARGET_TIME} 还有 {wait_seconds} 秒，等待中..."
        )
        time.sleep(wait_seconds - WARMUP_SECONDS)
        logging.info(
            f"提前 {WARMUP_SECONDS} 秒开始登录..."
        )
    elif wait_seconds > 0:
        logging.info(
            f"距离 {TARGET_TIME} 不足 {WARMUP_SECONDS} 秒，立即登录..."
        )
    else:
        logging.info(
            f"当前时间已经超过 {TARGET_TIME}，立即执行..."
        )

def prepare_all(users, usernames, passwords, action):
    """在目标时间前完成登录；同一账号建议只保留一个配置。"""
    current_dayofweek = get_current_dayofweek(action)
    prepared = [None] * len(users)

    username_list = (
        [item.strip() for item in usernames.split(",")]
        if action and usernames
        else []
    )
    password_list = (
        [item.strip() for item in passwords.split(",")]
        if action and passwords
        else []
    )

    def login_one(index):
        user = users[index]
        username = user.get("username", "")
        password = user.get("password", "")
        times = user.get("times", user.get("time", []))
        roomid = user.get("roomid")
        seatid = user.get("seatid")
        daysofweek = user.get("daysofweek", [])

        if isinstance(seatid, str):
            seatid = [seatid]

        if action:
            if index >= len(username_list) or index >= len(password_list):
                raise ValueError(
                    f"第{index + 1}个配置缺少对应的 USERNAMES/PASSWORDS"
                )
            username = username_list[index]
            password = password_list[index]

        if current_dayofweek not in daysofweek:
            return index, None

        logging.info(
            f"[prepare] ({index + 1}/{len(users)}) 登录: "
            f"user={username}, times={times}, seatid={seatid}, roomid={roomid}"
        )

        client = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )

        client.get_login_status()
        login_success, login_message = client.login(username, password)
        if not login_success:
            logging.error(
                f"[prepare] 用户 {username} 登录失败：{login_message}"
            )
            return index, None

        # 清除登录接口遗留的 Host、Content-Type、X-Requested-With 等请求头。
        # Host 由 requests 根据 URL 自动生成，不手动设置。
        client.reset_office_headers()

        # 保留“热启动”，但只访问 office 首页，不提前访问 token 页面。
        client.warmup_office_home()

        return index, {
            "s": client,
            "times": times,
            "roomid": roomid,
            "seatid": seatid,
            "action": action,
            "username": username,
        }

    workers = min(MAX_WORKERS, max(1, len(users)))
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="login",
    ) as executor:
        future_map = {
            executor.submit(login_one, index): index
            for index in range(len(users))
        }
        for future in as_completed(future_map):
            index = future_map[future]
            try:
                result_index, result = future.result()
                prepared[result_index] = result
            except Exception as error:
                logging.exception(
                    f"[prepare] 线程异常 index={index}: {error}"
                )

    return prepared


def submit_all(prepared, success_list):
    """使用同一个登录 Session，依次提交尚未完成的时间段。"""
    pending = [
        (index, item)
        for index, item in enumerate(prepared)
        if item is not None and not all(success_list[index])
    ]
    if not pending:
        return success_list

    def submit_one(index, item):
        client = item["s"]
        periods = item["times"]
        roomid = item["roomid"]
        seatids = item["seatid"]
        action = item["action"]
        username = item.get("username", f"user{index}")

        # 必须继承上一轮结果，不能把已成功的时间段重新置为 False。
        period_results = list(success_list[index])

        for seat in seatids:
            url = client.url.format(roomid, seat)

            for period_index, period in enumerate(periods):
                if period_results[period_index]:
                    continue

                if not isinstance(period, list) or len(period) != 2:
                    logging.error(
                        f"[submit] 非法时间段配置：{period!r}，"
                        "应为 [\"08:00\", \"10:00\"]"
                    )
                    continue

                logging.info(
                    f"[submit] {username} seat={seat} "
                    f"开始预约时间段 {period[0]}-{period[1]}"
                )

                for attempt in range(1, MAX_ATTEMPT + 1):
                    if get_current_time(action) >= ENDTIME:
                        logging.warning(
                            f"[submit] 已到截止时间 {ENDTIME}，停止当前任务"
                        )
                        return index, period_results

                    token, value = client._get_page_token(
                        url,
                        require_value=False,
                        deadline_hms=ENDTIME,
                    )

                    if not token:
                        logging.warning(
                            f"[submit] {username} seat={seat} "
                            f"{period[0]}-{period[1]} 第{attempt}次 "
                            "token获取失败"
                        )
                        if attempt < MAX_ATTEMPT:
                            time.sleep(0.15 * attempt)
                        continue

                    if not value:
                        logging.error(
                            f"[submit] {username} seat={seat} "
                            f"{period[0]}-{period[1]} 页面没有algorithm，"
                            "当前页面安全校验方式已变化，继续提交"
                        )
                        
                    success, message = client.get_submit(
                        client.submit_url,
                        times=period,
                        token=token,
                        roomid=roomid,
                        seatid=seat,
                        captcha="",
                        action=action,
                        value=value,
                    )

                    message = message or ""
                    if success or "已有预约" in message:
                        period_results[period_index] = True
                        if not success:
                            logging.info(
                                f"[submit] {period[0]}-{period[1]} "
                                "已存在预约，按完成处理"
                            )
                        break

                    if attempt < MAX_ATTEMPT:
                        retry_delay = random.uniform(0.2, 0.45)
                        if "303" in message:
                            logging.info(
                                f"[submit] 第{attempt}次出现303，"
                                "保持当前Session，仅重新获取token"
                            )
                        else:
                            logging.info(
                                f"[submit] 第{attempt}次失败：{message}，"
                                "重新获取token"
                            )
                        time.sleep(retry_delay)

                if not period_results[period_index]:
                    logging.warning(
                        f"[submit] {username} "
                        f"{period[0]}-{period[1]} 本轮未完成"
                    )

        return index, period_results

    workers = min(MAX_WORKERS, max(1, len(pending)))
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="submit",
    ) as executor:
        future_map = {
            executor.submit(submit_one, index, item): index
            for index, item in pending
        }
        for future in as_completed(future_map):
            index = future_map[future]
            try:
                result_index, result = future.result()
                success_list[result_index] = result
            except Exception as error:
                logging.exception(
                    f"[submit_all] 线程异常 index={index}: {error}"
                )

    return success_list


def main(users, action=False):
    current_time = get_current_time(action)
    logging.info(f"[main] 开始时间 {current_time}, 模式={'GitHub Action' if action else '本地'}")
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    current_dayofweek = get_current_dayofweek(action)
    today_reservation_num = sum(
        len(user.get("times", user.get("time", [])))
        for user in users
        if current_dayofweek in user.get("daysofweek", [])
    )
    success_list = [
        [False] * len(user.get("times", user.get("time", [])))
        for user in users
    ]
    logging.info(
        f"[main] 今日待预约 {today_reservation_num} 个时间段，"
        f"配置账号数={len(users)}"
    )

    prepared = prepare_all(users, usernames, passwords, action)

    # 如果已过 08:00，跳过等待直接提交
    if current_time < TARGET_TIME:
        logging.info(
            f"[main] 预热登录完成，等待 {TARGET_TIME} 整点提交..."
        )
        while True:
            current_time = get_current_time(action)
            if current_time >= TARGET_TIME:
                break
            time.sleep(0.1)
    else:
        logging.info(
            f"[main] 登录完成，已过 {TARGET_TIME}，立即尝试提交..."
        )

    logging.info("[main] ⏰ 开始提交！")
    attempt_times = 0
    # do-while 模式：至少执行一轮，方便手动触发时验证 token 是否可获取
    while True:
        attempt_times += 1
        success_list = submit_all(prepared, success_list)
        done = sum(
            sum(item)
            for item in success_list
        )
        current_time = get_current_time(action)
        logging.info(f"[main] 第{attempt_times}轮 {current_time}, "
                     f"已完成 {done}/{today_reservation_num}, 状态={success_list}")
        
        if done == today_reservation_num:
            logging.info(f"[main] 🎉 全部预约成功！共 {attempt_times} 轮")
            return
        if current_time >= ENDTIME:
            logging.warning(f"[main] ⚠️ 已到截止时间 {ENDTIME}，"
                           f"尚有 {today_reservation_num - done} 人未成功")
            return
        time.sleep(SLEEPTIME)


def debug(users, action=False):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    logging.info(f" Debug Mode start! , action {'on' if action else 'off'}")
    if action:
        usernames, passwords = get_user_credentials(action)
    current_dayofweek = get_current_dayofweek(action)

    def debug_one(index):
        user = users[index]
        username, password, times, roomid, seatid, daysofweek = user.values()
        if type(seatid) == str:
            seatid = [seatid]
        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            return False
        logging.info(f"----------- {username} -- {times} -- {seatid} try -----------")
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        s.login(username, password)
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        return s.submit(times, roomid, seatid, action)

    workers = min(MAX_WORKERS, len(users))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="debug") as executor:
        futures = [executor.submit(debug_one, i) for i in range(len(users))]
        for future in as_completed(futures):
            try:
                if future.result():
                    logging.info("[debug] 🎉 预约成功！")
                    # 取消剩余任务（已在执行的会继续运行完，但不影响结果）
                    for f in futures:
                        f.cancel()
                    return
            except Exception as e:
                logging.error(f"[debug] 线程异常: {e}")


def get_roomid(args1, args2):
    username = input("请输入用户名：")
    password = input("请输入密码：")
    s = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
    )
    s.get_login_status()
    s.login(username=username, password=password)
    s.requests.headers.update({"Host": "office.chaoxing.com"})
    encode = input("请输入deptldEnc：")
    s.roomid(encode)

def token_test(users, action=False):
    """
    只测试：
    1. 获取 Cookie
    2. 登录
    3. 获取预约页面
    4. 解析 token

    不会发送预约提交请求。
    """
    usernames = None
    passwords = None

    if action:
        usernames, passwords = get_user_credentials(action)
        username_list = [item.strip() for item in usernames.split(",")]
        password_list = [item.strip() for item in passwords.split(",")]

    if not users:
        logging.error("[token_test] config.json 中没有预约配置")
        return

    # 测试时只使用第一条配置，避免同一个账号并发登录
    user = users[0]
    username, password, times, roomid, seatid, daysofweek = user.values()

    if action:
        username = username_list[0]
        password = password_list[0]

    seats = [seatid] if isinstance(seatid, str) else seatid

    logging.info(
        f"[token_test] 开始测试 user={username}, "
        f"roomid={roomid}, seats={seats}"
    )

    client = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
    )

    client.get_login_status()
    login_success, login_message = client.login(username, password)

    if not login_success:
        logging.error(
            f"[token_test] 登录失败：{login_message}"
        )
        return

    client.requests.headers.update(
        {"Host": "office.chaoxing.com"}
    )

    for seat in seats:
        url = client.url.format(roomid, seat)

        token, value = client._get_page_token(
            url,
            require_value=True,
        )

        logging.info(
            f"[token_test] seat={seat}, "
            f"token_len={len(token)}, "
            f"value_len={len(value)}"
        )

        if token:
            logging.info("[token_test] ✅ token 获取成功，不执行预约")
        else:
            logging.error(
                "[token_test] ❌ token 获取失败，请下载 debug HTML"
            )

    logging.info("[token_test] 测试结束，没有发送预约请求")
if __name__ == "__main__":
    config_path = os.path.join(
        os.path.dirname(__file__),
        "config.json",
    )

    parser = argparse.ArgumentParser(
        prog="Chao Xing seat auto reserve"
    )

    parser.add_argument(
        "-u",
        "--user",
        default=config_path,
        help="user config file",
    )

    parser.add_argument(
        "-m",
        "--method",
        default="reserve",
        choices=["reserve", "debug", "room", "token"],
        help="reserve=正式预约，token=只测试token",
    )

    parser.add_argument(
        "-a",
        "--action",
        action="store_true",
        help="use --action to enable in github action",
    )

    args = parser.parse_args()

    with open(
        args.user,
        "r",
        encoding="utf-8",
    ) as data:
        usersdata = json.load(data)["reserve"]

    func_dict = {
        "reserve": main,
        "debug": debug,
        "room": get_roomid,
        "token": token_test,
    }

    # 只有正式预约模式才等待目标时间
    # token 测试模式会立即运行
    if args.method == "reserve":
        wait_until_prepare(args.action)

    func_dict[args.method](
        usersdata,
        args.action,
    )
