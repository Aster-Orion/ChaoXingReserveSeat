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

SLEEPTIME = 1.0
ENDTIME = "15:30:00"
ENABLE_SLIDER = False
MAX_ATTEMPT = 5
RESERVE_NEXT_DAY = True
MAX_WORKERS = 10  # 最大并行线程数，可根据需要调整


def prepare_all(users, usernames, passwords, action):
    """并行提前登录，多个用户同时进行，大幅缩短登录等待时间"""
    current_dayofweek = get_current_dayofweek(action)
    prepared = [None] * len(users)

    def login_one(index):
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
            return index, None
        logging.info(f"[prepare] ({index+1}/{len(users)}) 并行登录: user={username}, "
                     f"times={times}, seatid={seatid}, roomid={roomid}")
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        s.login(username, password)
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        # 预热：提前请求一次 token 页面，让服务器/CDN缓存"热起来"
        # 使用快速单次请求，避免重试逻辑阻塞准备阶段
        warmup_headers = {
            "Referer": "https://office.chaoxing.com/",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Host": "office.chaoxing.com",
        }
        for seat in seatid:
            try:
                s.requests.get(
                    url=s.url.format(roomid, seat),
                    headers=warmup_headers,
                    timeout=5,
                    verify=False,
                )
            except Exception:
                pass  # 预热失败不影响主流程
        return index, {
            "s": s,
            "times": times,
            "roomid": roomid,
            "seatid": seatid,
            "action": action,
            "username": username,
        }

    workers = min(MAX_WORKERS, len(users))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="login") as executor:
        futures = [executor.submit(login_one, i) for i in range(len(users))]
        for future in as_completed(futures):
            try:
                idx, result = future.result()
                prepared[idx] = result
            except Exception as e:
                logging.error(f"[prepare] 线程异常 index={idx}: {e}")

    return prepared


def submit_all(prepared, success_list):
    """并行提交预约，多个用户同时抢座，互不阻塞"""
    # 收集当前轮需要提交的项（未成功且有效）
    pending = [
        (i, item)
        for i, item in enumerate(prepared)
        if item is not None and not success_list[i]
    ]
    if not pending:
        return success_list

    def submit_one(index, item):
        # 随机抖动 100~800ms，同用户多时间段时增大错峰，降低303概率
        jitter = random.uniform(0.1, 0.8)
        time.sleep(jitter)

        s = item["s"]
        times = item["times"]
        roomid = item["roomid"]
        seatid = item["seatid"]
        action = item["action"]
        username = item.get("username", f"user{index}")
        for seat in seatid:
            url = s.url.format(roomid, seat)
            # 每个 seat 最多 3 次尝试，每次重新获取 token 避免 303 超时
            for attempt in range(1, 4):
                token, value = s._get_page_token(url, require_value=True)
                if not token:
                    logging.warning(f"[submit_all] {username} seat={seat} token为空，跳过")
                    break
                success, msg = s.get_submit(
                    s.submit_url,
                    times=times,
                    token=token,
                    roomid=roomid,
                    seatid=seat,
                    captcha="",
                    action=action,
                    value=value,
                )
                if success:
                    return index, True
                # 失败处理：加大抖动延迟，连续失败时刷新 session
                if attempt < 3:
                    retry_delay = random.uniform(0.2, 0.6)
                    # 303 超时连续出现时刷新 session cookie
                    if "303" in (msg or ""):
                        logging.info(
                            f"[submit_all] {username} seat={seat} 第{attempt}次失败(303超时)，"
                            f"刷新session并等待{retry_delay:.1f}s..."
                        )
                        try:
                            s.get_login_status()
                        except Exception:
                            pass
                    else:
                        logging.info(
                            f"[submit_all] {username} seat={seat} 第{attempt}次失败，"
                            f"刷新token重试..."
                        )
                    time.sleep(retry_delay)
        return index, False

    workers = min(MAX_WORKERS, len(pending))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="submit") as executor:
        futures = {executor.submit(submit_one, i, item): i for i, item in pending}
        for future in as_completed(futures):
            try:
                idx, result = future.result()
                if result:
                    success_list[idx] = True
            except Exception as e:
                logging.error(f"[submit_all] 线程异常 index={futures[future]}: {e}")

    return success_list


def main(users, action=False):
    current_time = get_current_time(action)
    logging.info(f"[main] 开始时间 {current_time}, 模式={'GitHub Action' if action else '本地'}")
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    current_dayofweek = get_current_dayofweek(action)
    today_reservation_num = sum(
        1 for d in users if current_dayofweek in d.get("daysofweek")
    )
    success_list = [False] * len(users)
    logging.info(f"[main] 今日待预约 {today_reservation_num}/{len(users)} 人")

    prepared = prepare_all(users, usernames, passwords, action)

    # 如果已过 08:00，跳过等待直接提交
    if current_time < "08:00:00":
        logging.info("[main] 预热登录完成，等待 08:00:00 整点提交...")
        while True:
            current_time = get_current_time(action)
            if current_time >= "08:00:00":
                break
            time.sleep(0.1)
    else:
        logging.info("[main] 预热登录完成，已过 08:00，立即尝试提交...")

    logging.info("[main] ⏰ 开始提交！")
    attempt_times = 0
    # do-while 模式：至少执行一轮，方便手动触发时验证 token 是否可获取
    while True:
        attempt_times += 1
        success_list = submit_all(prepared, success_list)
        done = sum(success_list)
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


if __name__ == "__main__":
    beijing_now = time.time() + 8 * 3600
    beijing_struct = time.gmtime(beijing_now)
    target_seconds = 8 * 3600
    current_seconds = beijing_struct.tm_hour * 3600 + beijing_struct.tm_min * 60 + beijing_struct.tm_sec
    wait = target_seconds - current_seconds

    # 🟢 终极优化：更改提前唤醒时间为 3 秒，避免过度空转与 Token 提前老化
    if wait > 3:
        logging.info(f"距离北京时间 08:00:00 还有 {wait} 秒，等待中...")
        time.sleep(wait - 3)
        logging.info("提前 3 秒开始预热登录...")
    elif wait > 0:
        logging.info(f"距离08:00不足 3 秒，立即预热登录...")
    else:
        logging.info("已过北京时间 08:00:00，立即执行")

    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument(
        "-m",
        "--method",
        default="reserve",
        choices=["reserve", "debug", "room"],
        help="for debug",
    )
    parser.add_argument(
        "-a",
        "--action",
        action="store_true",
        help="use --action to enable in github action",
    )
    args = parser.parse_args()
    func_dict = {"reserve": main, "debug": debug, "room": get_roomid}
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
    func_dict[args.method](usersdata, args.action)
