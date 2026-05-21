import os, json, time as _time, socket as _socket, logging
from datetime import datetime, timedelta

# 端口锁：防止重复启动，bind失败时agentmain会直接崩溃退出
# reload时mod.__dict__保留_lock，跳过重复绑定
try: _lock
except NameError:
    _lock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    _lock.bind(('127.0.0.1', 45762)); _lock.listen(1)

INTERVAL = 30
ONCE = False

_dir = os.path.dirname(os.path.abspath(__file__))
TASKS = os.path.join(_dir, '../sche_tasks')
DONE = os.path.join(_dir, '../sche_tasks/done')
_LOG = os.path.join(_dir, '../sche_tasks/scheduler.log')

# --- 日志 ---
_logger = logging.getLogger('scheduler')
if not _logger.handlers:
    _logger.setLevel(logging.INFO)
    _fh = logging.FileHandler(_LOG, encoding='utf-8')
    _fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M'))
    _logger.addHandler(_fh)

# 默认最大延迟窗口（小时），超过此时间不触发
DEFAULT_MAX_DELAY = 6
_l4_t = 0 # last L4 archive time

# --- 空闲自动继续机制 ---
# check()被调用=agent空闲(reflect线程在agent执行任务时阻塞等待done)
# 所以只需追踪连续空闲时间，超时返回"继续"
IDLE_TIMEOUT = 60 # 空闲多少秒后触发"继续"(从90→60,更快恢复)
IDLE_COOLDOWN = 30 # 触发一次后的冷却时间(秒)(从60→30,更频繁恢复)
IDLE_MAX_CONTINUOUS = 20 # 最大连续触发次数(从5→20,避免过早进长冷却)
IDLE_LONG_COOLDOWN = 60 # 连续触发过多后的长冷却(秒)(从300→60,长冷却也快速恢复)
IDLE_DRAIN_BACKOFF_BASE = 300 # drain超时后退避基数(秒),指数递增
_idle_since = None       # 开始空闲的时间戳
_idle_last_trigger = 0   # 上次触发"继续"的时间戳
_idle_count = 0          # 连续触发次数
_drain_timeout_count = 0 # 连续drain超时次数

# --- 开机首次检测机制 ---
_boot_check_done = False  # 是否已执行过开机检测
_boot_check_max_delay = 24  # 开机时放宽max_delay到24小时(覆盖missed任务)

def _boot_check():
    """开机首次调用check()时，检测所有missed的定时任务和TODO待办"""
    global _boot_check_done
    if _boot_check_done:
        return None
    _boot_check_done = True
    _logger.info('[BOOT CHECK] 首次开机检测开始')

    # 1) 检查是否有missed定时任务(max_delay放宽到24h)
    if not os.path.isdir(TASKS):
        _logger.info('[BOOT CHECK] sche_tasks目录不存在，跳过')
        return None

    now = datetime.now()
    os.makedirs(DONE, exist_ok=True)
    done_files = set(os.listdir(DONE))
    for f in sorted(os.listdir(TASKS)):
        if not f.endswith('.json'): continue
        tid = f[:-5]
        try:
            with open(os.path.join(TASKS, f), encoding='utf-8') as fp:
                task = json.loads(fp.read())
        except Exception:
            continue
        if not task.get('enabled', False): continue
        if tid == 'auto_continue': continue
        repeat = task.get('repeat', 'daily')
        sched = task.get('schedule', '00:00')
        try:
            h, m = map(int, sched.split(':'))
        except Exception:
            continue
        sched_minutes = h * 60 + m
        now_minutes = now.hour * 60 + now.minute
        # 开机时放宽max_delay到24小时
        max_delay = _boot_check_max_delay
        if (now_minutes - sched_minutes) > max_delay * 60:
            continue
        if repeat == 'weekday' and now.weekday() >= 5:
            continue
        # 检查冷却(开机时用较短冷却)
        last = _last_run(tid, done_files)
        cooldown = _parse_cooldown(repeat)
        # 开机首次检测：冷却减半
        if last and (now - last) < cooldown / 2:
            continue
        _logger.info(f'[BOOT CHECK] TRIGGER missed task: {tid}')
        ts = now.strftime('%Y-%m-%d_%H%M')
        rpt = os.path.join(DONE, f'{ts}_{tid}.md')
        prompt = task.get('prompt', '')
        return (f'[定时任务-开机补触发] {tid}\n'
                f'[报告路径] {rpt}\n\n'
                f'先读 scheduled_task_sop 了解执行流程，然后执行以下任务：\n\n'
                f'{prompt}\n\n')

    # 2) 检查TODO.txt是否有未完成任务
    try:
        from memory.autonomous_operation_sop.helper import get_todo
        todos = get_todo()
        if todos:
            todo_text = '\n'.join(f'- {t}' for t in todos[:10])
            _logger.info(f'[BOOT CHECK] 发现{len(todos)}个TODO待办')
            return (f'[开机检测-待办任务] 发现{len(todos)}个未完成任务\n\n'
                    f'{todo_text}\n\n'
                    f'请检查以上待办，按优先级逐个处理。')
    except Exception as e:
        _logger.debug(f'[BOOT CHECK] TODO check skipped: {e}')

    _logger.info('[BOOT CHECK] 无missed任务，无待办')
    return None

def _check_idle_continue():
    """检测agent空闲是否超时，超时则返回'继续'prompt"""
    global _idle_since, _idle_last_trigger, _idle_count, _drain_timeout_count
    now = _time.time()

    # drain超时退避：连续超时后增加空闲等待时间，避免死循环
    if _drain_timeout_count > 0:
        backoff = IDLE_DRAIN_BACKOFF_BASE * (2 ** min(_drain_timeout_count - 1, 2))
        backoff = min(backoff, 1800)  # 绝对上限30分钟
        if now - _idle_last_trigger < backoff:
            # [FIX] 不再直接return None——仍需设置idle_since以保持空闲计时运行
            if _idle_since is None:
                _idle_since = now
            _logger.info(f'DRAIN BACKOFF: count={_drain_timeout_count}, backoff={backoff}s, remaining={backoff - (now - _idle_last_trigger):.0f}s')
            return None
        _logger.info(f'DRAIN BACKOFF expired: count={_drain_timeout_count}, was backoff={backoff}s')

    # 检查是否通过auto_continue.json启用
    ac_path = os.path.join(TASKS, 'auto_continue.json')
    try:
        with open(ac_path, encoding='utf-8') as fp:
            ac = json.loads(fp.read())
        if not ac.get('enabled', False):
            _idle_since = None
            _idle_count = 0
            return None
    except (FileNotFoundError, json.JSONDecodeError):
        _idle_since = None
        _idle_count = 0
        return None

    # 记录空闲开始时间
    if _idle_since is None:
        _idle_since = now

    idle_duration = now - _idle_since

    # 检查冷却
    since_last_trigger = now - _idle_last_trigger
    if _idle_count >= IDLE_MAX_CONTINUOUS:
        cooldown = IDLE_LONG_COOLDOWN
    else:
        cooldown = IDLE_COOLDOWN

    if since_last_trigger < cooldown:
        return None

    # 空闲超时，触发"继续"
    if idle_duration >= IDLE_TIMEOUT:
        _idle_last_trigger = now
        _idle_count += 1
        _idle_since = now  # 重置空闲计时
        _logger.info(f'IDLE AUTO-CONTINUE #{_idle_count} (idle={idle_duration:.0f}s, '
                     f'cooldown={cooldown}s)')
    return '[USER_CMD]继续'

    return None

def _parse_cooldown(repeat):
    """解析repeat为冷却时间(比实际周期略短,防漂移)"""
    if repeat == 'once': return timedelta(days=999999)
    if repeat in ('daily', 'weekday'): return timedelta(hours=20)
    if repeat == 'weekly': return timedelta(days=6)
    if repeat == 'monthly': return timedelta(days=27)
    if repeat.startswith('every_'):
        try:
            parts = repeat.split('_')
            n = int(parts[1].rstrip('hdm'))
            u = parts[1][-1]
            if u == 'h': return timedelta(hours=n)
            if u == 'm': return timedelta(minutes=n)
            if u == 'd': return timedelta(days=n)
        except (ValueError, IndexError):
            pass # fall through to warning below
    _logger.warning(f'Unknown repeat type: {repeat}, fallback to 20h cooldown')
    return timedelta(hours=20)

def _last_run(tid, done_files):
    """找最近一次执行时间"""
    latest = None
    for df in done_files:
        if not df.endswith(f'_{tid}.md'): continue
        try:
            t = datetime.strptime(df[:15], '%Y-%m-%d_%H%M')
            if latest is None or t > latest: latest = t
        except: continue
    return latest

def check():
    # L4 archive cron (silent, every 12h)
    global _l4_t
    if _time.time() - _l4_t > 43200:
        _l4_t = _time.time()
        try:
            import sys; sys.path.insert(0, os.path.join(_dir, '../memory/L4_raw_sessions'))
            from compress_session import batch_process
            raw_dir = os.path.join(_dir, '../temp/model_responses')
            r = batch_process(raw_dir, dry_run=False)
            print(f'[L4 cron] {r}')
        except Exception as e:
            _logger.error(f'L4 archive failed: {e}')

    # --- 开机首次检测(只执行一次) ---
    boot_result = _boot_check()
    if boot_result:
        return boot_result

    if not os.path.isdir(TASKS): return None

    # --- 空闲自动继续检测（优先级低于定时任务） ---
    # 先检查定时任务，如果有定时任务要触发则优先
    now = datetime.now()
    os.makedirs(DONE, exist_ok=True)
    done_files = set(os.listdir(DONE))
    for f in sorted(os.listdir(TASKS)):
        if not f.endswith('.json'): continue
        tid = f[:-5]
        try:
            with open(os.path.join(TASKS, f), encoding='utf-8') as fp:
                task = json.loads(fp.read())
        except Exception as e:
            _logger.error(f'JSON parse error for {f}: {e}')
            continue
        if not task.get('enabled', False): continue
        # 跳过auto_continue.json，它由空闲检测机制处理
        if tid == 'auto_continue': continue

        repeat = task.get('repeat', 'daily')
        sched = task.get('schedule', '00:00')
        try:
            h, m = map(int, sched.split(':'))
        except Exception as e:
            _logger.error(f'Invalid schedule format in {f}: {sched!r} ({e})')
            continue

        # weekday任务：周末跳过
        if repeat == 'weekday' and now.weekday() >= 5: continue

        # 还没到schedule时间就跳过
        if now.hour < h or (now.hour == h and now.minute < m): continue

        # 执行窗口检查：超过max_delay小时则跳过
        max_delay = task.get('max_delay_hours', DEFAULT_MAX_DELAY)
        sched_minutes = h * 60 + m
        now_minutes = now.hour * 60 + now.minute
        if (now_minutes - sched_minutes) > max_delay * 60:
            _logger.info(f'SKIP {tid}: {now_minutes - sched_minutes}min past schedule, '
                         f'exceeds max_delay={max_delay}h')
            continue

        # 检查冷却
        last = _last_run(tid, done_files)
        cooldown = _parse_cooldown(repeat)
        if last and (now - last) < cooldown: continue

        # 有定时任务触发时，重置空闲计时（说明不是空闲状态）
        global _idle_since, _idle_count
        _idle_since = None
        _idle_count = 0

        # 触发
        _logger.info(f'TRIGGER {tid} (repeat={repeat}, schedule={sched}, '
                     f'last_run={last})')
        ts = now.strftime('%Y-%m-%d_%H%M')
        rpt = os.path.join(DONE, f'{ts}_{tid}.md')
        prompt = task.get('prompt', '')
        return (f'[定时任务] {tid}\n'
                f'[报告路径] {rpt}\n\n'
                f'先读 scheduled_task_sop 了解执行流程，然后执行以下任务：\n\n'
                f'{prompt}\n\n'
                f'完成后将执行报告写入 {rpt}。')

    # 没有定时任务触发，检查空闲自动继续
    idle_result = _check_idle_continue()
    if idle_result:
        return idle_result

    return None

def on_drain_timeout():
    """drain超时回调：由agentmain在drain超时后调用，增加退避计数"""
    global _drain_timeout_count, _idle_since
    _drain_timeout_count += 1
    # _idle_since = None  # [REMOVED] 死循环根因：重置空闲计时导致永远达不到IDLE_TIMEOUT阈值
    _idle_count = 0
    backoff = IDLE_DRAIN_BACKOFF_BASE * (2 ** min(_drain_timeout_count - 1, 2))
    backoff = min(backoff, 1800)  # 绝对上限30分钟
    _logger.warning(f'DRAIN TIMEOUT #{_drain_timeout_count}, backoff={backoff}s')

def reset_drain_timeout():
    """任务正常完成后重置drain超时计数"""
    global _drain_timeout_count
    if _drain_timeout_count > 0:
        _logger.info(f'DRAIN TIMEOUT count reset (was {_drain_timeout_count})')
        _drain_timeout_count = 0
