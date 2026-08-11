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
ENDTIME = "08:01:00"
ENABLE_SLIDER = False
MAX_ATTEMPT = 5
RESERVE_NEXT_DAY = True
MAX_WORKERS = 10  # 最大并行线程数（仅用于 prepare_all 登录阶段）


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
        # 预热：提前请求 token 页面，让服务器/CDN 缓存"热起来"
        # 高峰期优化：实际尝试提取一次 token，确保 session 完全就绪
        warmup_headers = {
            "Referer": "https://office.chaoxing.com/",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Host": "office.chaoxing.com",
        }
        for seat in seatid:
            warmup_url = s.url.format(roomid, seat)
            # 第一阶段：快速 GET 预热 CDN
            try:
                s.requests.get(
                    url=warmup_url,
                    headers=warmup_headers,
                    timeout=5,
                    verify=False,
                )
            except Exception:
                pass
            # 第二阶段：尝试真实 token 提取，验证 session 有效性
            try:
                token, _ = s._get_page_token(warmup_url, require_value=False)
                if token:
                    logging.info(
                        f"[prepare] {username} seat={seat} 预热token获取成功, "
                        f"len={len(token)}"
                    )
                else:
                    logging.warning(
                        f"[prepare] {username} seat={seat} 预热token为空，"
                        f"将在正式提交时重试"
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


def submit_all(prepared, slot_success):
    """串行提交预约，逐个用户依次提交，避免并行导致 303 会话冲突

    每个时间段作为一次独立的请求发送，一个请求只发送一个时间段。
    slot_success: 列表，每项是一个 set，记录该用户已成功的时间段索引。
    """
    # 收集当前轮需要提交的项（还有未完成时间段的用户）
    pending = [
        (i, item)
        for i, item in enumerate(prepared)
        if item is not None
        and len(slot_success[i]) < len(item["times"])
    ]
    if not pending:
        return slot_success

    total_pending = len(pending)
    for pos, (index, item) in enumerate(pending):
        s = item["s"]
        times = item["times"]
        roomid = item["roomid"]
        seatid = item["seatid"]
        action = item["action"]
        username = item.get("username", f"user{index}")

        for ti, time_slot in enumerate(times):
            if ti in slot_success[index]:
                continue  # 该时间段已成功，跳过

            logging.info(
                f"[submit_all] 用户 {username} 时间段 "
                f"{time_slot[0]}~{time_slot[1]} "
                f"({pos+1}/{total_pending}人, "
                f"时段{ti+1}/{len(times)})"
            )

            slot_done = False
            for seat in seatid:
                url = s.url.format(roomid, seat)
                # 每个 seat 最多 3 次尝试，每次重新获取 token 避免 303 超时
                for attempt in range(1, 4):
                    # 每次尝试前（除首次外）刷新 session，确保 cookie 新鲜
                    if attempt > 1:
                        logging.info(
                            f"[submit_all] {username} seat={seat} "
                            f"第{attempt}次尝试前刷新session..."
                        )
                        try:
                            s.get_login_status()
                        except Exception:
                            pass
                        time.sleep(random.uniform(0.3, 0.8))

                    token, value = s._get_page_token(url, require_value=True)
                    if not token:
                        logging.warning(
                            f"[submit_all] {username} seat={seat} token为空，跳过"
                        )
                        break
                    success, msg = s.get_submit(
                        s.submit_url,
                        times=time_slot,
                        token=token,
                        roomid=roomid,
                        seatid=seat,
                        captcha="",
                        action=action,
                        value=value,
                    )
                    if success:
                        slot_success[index].add(ti)
                        slot_done = True
                        logging.info(
                            f"[submit_all] ✅ {username} 时间段 "
                            f"{time_slot[0]}~{time_slot[1]} 预约成功!"
                        )
                        break
                    # 失败处理
                    if attempt < 3:
                        retry_delay = random.uniform(0.3, 0.8)
                        if "303" in (msg or ""):
                            logging.info(
                                f"[submit_all] {username} seat={seat} "
                                f"第{attempt}次失败(303超时)，"
                                f"刷新session并等待{retry_delay:.1f}s..."
                            )
                            try:
                                s.get_login_status()
                            except Exception:
                                pass
                        else:
                            logging.info(
                                f"[submit_all] {username} seat={seat} "
                                f"第{attempt}次失败，刷新token重试..."
                            )
                        time.sleep(retry_delay)

                if slot_done:
                    break  # 该时间段已成功，跳出 seat 循环

            # 时间段间短暂间隔
            if not slot_done and ti < len(times) - 1:
                time.sleep(random.uniform(0.3, 0.6))

        # 用户间增加间隔，进一步降低 303 概率
        remaining = total_pending - pos - 1
        if remaining > 0:
            interval = random.uniform(0.5, 1.5)
            logging.debug(
                f"[submit_all] 用户间间隔 {interval:.1f}s, 剩余 {remaining} 人"
            )
            time.sleep(interval)

    return slot_success


def main(users, action=False):
    current_time = get_current_time(action)
    logging.info(f"[main] 开始时间 {current_time}, 模式={'GitHub Action' if action else '本地'}")
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    current_dayofweek = get_current_dayofweek(action)
    # 统计有效用户数和总时间段数
    active_users = sum(
        1 for u in users if current_dayofweek in u.get("daysofweek", [])
    )
    total_slots = sum(
        len(u.get("times", [])) for u in users
        if current_dayofweek in u.get("daysofweek", [])
    )
    # 每个用户用一个 set 记录已成功的时间段索引
    slot_success = [set() for _ in range(len(users))]
    logging.info(
        f"[main] 今日待预约 {active_users} 人, 共 {total_slots} 个时间段"
    )

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
        slot_success = submit_all(prepared, slot_success)
        done_slots = sum(len(s) for s in slot_success)
        current_time = get_current_time(action)
        logging.info(f"[main] 第{attempt_times}轮 {current_time}, "
                     f"已完成 {done_slots}/{total_slots} 个时间段")
        if done_slots == total_slots:
            logging.info(f"[main] 🎉 全部预约成功！共 {attempt_times} 轮")
            return
        if current_time >= ENDTIME:
            logging.warning(f"[main] ⚠️ 已到截止时间 {ENDTIME}，"
                           f"尚有 {total_slots - done_slots} 个时间段未成功")
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
