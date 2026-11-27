"""
微观指标跟踪 — 本地服务器
端口 4323, 提供静态文件 + API: POST /api/update 触发全流程更新
"""
import http.server
import subprocess
import sys
import os
import json
import threading
import time
import traceback
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(BASE_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
PYTHON = sys.executable
DATA_DIR = os.path.join(BASE_DIR, "data")
UPDATE_LOG = os.path.join(DATA_DIR, "update_log.json")
SERVER_LOG = os.path.join(DATA_DIR, "server.log")

# Ensure data dir
os.makedirs(DATA_DIR, exist_ok=True)


def log(msg):
    """Print and append to data/server.log so failures stay diagnosable
    after the console window is gone."""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(SERVER_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

STATUS = {"running": False, "step": None, "last_update": None, "last_result": None}

if os.path.exists(UPDATE_LOG):
    try:
        with open(UPDATE_LOG, "r", encoding="utf-8") as f:
            saved = json.load(f)
            STATUS["last_update"] = saved.get("last_update")
            STATUS["last_result"] = saved.get("last_result")
    except:
        pass


def _run_step(name, script, timeout):
    """Run one pipeline step, streaming its output to data/pipeline.log.

    Output goes to a file instead of a pipe: a killed child on Windows can
    leave grandchildren holding the pipe open, which makes subprocess.run's
    post-timeout communicate() block forever and the UI stay at "更新中".
    """
    script_path = os.path.join(SRC_DIR, script)
    pipeline_log = os.path.join(DATA_DIR, "pipeline.log")
    log(f"[UPDATE] Running {name}: {script_path} (timeout {timeout}s)")

    with open(pipeline_log, "a", encoding="utf-8", errors="replace") as out:
        out.write(f"\n{'='*60}\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {name} 开始\n")
        out.flush()
        proc = subprocess.Popen(
            [PYTHON, script_path],
            stdout=out, stderr=subprocess.STDOUT,
            cwd=BASE_DIR, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        try:
            code = proc.wait(timeout=timeout)
            ok = code == 0
            out.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {name} 结束 退出码={code}\n")
        except subprocess.TimeoutExpired:
            # 杀掉整个进程树，否则遗留进程会继续占用文件和网络
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
            proc.wait(timeout=30)
            out.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {name} 超时 {timeout}s，已终止\n")
            out.flush()
            return False, f"Timeout after {timeout}s"

    try:
        with open(pipeline_log, "r", encoding="utf-8", errors="replace") as f:
            tail = f.read()[-800:]
    except Exception:
        tail = ""
    return ok, tail


def run_update():
    STATUS["running"] = True
    STATUS["step"] = "开始"
    try:
        # 采集步骤涉及 70+ 个接口/5000+ 只股票，耗时远大于分析步骤，单独给足超时
        steps = [
            ("采集数据", "fetch_data.py", 1800),
            ("行业树", "fetch_sw_tree.py", 900),
            ("ETF资金", "fetch_nfgj.py", 900),
            ("分析引擎", "analyze.py", 600),
            ("构建页面", "build_dashboard.py", 120),
        ]
        results = []
        for name, script, timeout in steps:
            STATUS["step"] = name
            ok, output = _run_step(name, script, timeout)
            results.append({"step": name, "ok": ok, "output": output})
            log(f"[UPDATE] {name}: {'OK' if ok else 'FAILED'}")
            if not ok:
                break

        success = all(r["ok"] for r in results)
        STATUS["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        STATUS["last_result"] = {"success": success, "steps": results}

        with open(UPDATE_LOG, "w", encoding="utf-8") as f:
            json.dump({"last_update": STATUS["last_update"],
                       "last_result": STATUS["last_result"]}, f, ensure_ascii=False, indent=2)
        log(f"[UPDATE] Pipeline complete: {'SUCCESS' if success else 'FAILED'}")
        return success, results
    except Exception as e:
        log("[UPDATE] 未捕获异常:\n" + traceback.format_exc())
        STATUS["last_result"] = {"success": False, "error": str(e)}
        return False, [{"step": "异常", "ok": False, "output": str(e)}]
    finally:
        STATUS["running"] = False
        STATUS["step"] = None


def _realtime_loop():
    """盘中每 5 分钟采一次实时快照（供「当日」视图与分时采样）。"""
    time.sleep(3)
    while True:
        try:
            import fetch_rt
            if fetch_rt.market_open():
                s = fetch_rt.snapshot()
                log(f"[RT] snapshot {s['time']} alerts={len(s.get('alerts') or [])}")
        except Exception as e:
            log(f"[RT] 快照失败: {type(e).__name__} {e}")
        time.sleep(300)


NFGJ_CSV = os.path.join(DATA_DIR, "nfgj_hs300.csv")
RECHECK_TIMES = [(20, 30), (21, 30), (22, 30), (23, 30)]


def _last_nfgj_date():
    """ETF 资金跟踪里最新一条份额日期（YYYYMMDD）。"""
    try:
        with open(NFGJ_CSV, "r", encoding="utf-8-sig") as f:
            lines = f.read().strip().splitlines()
        return lines[-1].split(",")[0] if len(lines) > 1 else None
    except Exception:
        return None


def _evening_recheck_loop():
    """交易所当晚才披露当日 ETF 份额：若当天缺失，在固定时点自动补采。"""
    done = set()
    time.sleep(20)
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y%m%d")
            if now.weekday() < 5:
                for hh, mm in RECHECK_TIMES:
                    key = (today, hh, mm)
                    if now.hour * 60 + now.minute >= hh * 60 + mm and key not in done:
                        last = _last_nfgj_date() or ""
                        if last >= today:
                            done.add(key)
                            log(f"[RECHECK] {hh:02d}:{mm:02d} 已有当日份额（{last}）")
                            continue
                        if STATUS["running"]:
                            # 有更新在跑：不标记完成，下一分钟再试
                            continue
                        done.add(key)
                        log(f"[RECHECK] {hh:02d}:{mm:02d} 当日份额缺失（最新 {last}），自动补采")
                        STATUS["running"] = True
                        try:
                            for name, script, to in (("ETF资金", "fetch_nfgj.py", 900),
                                                     ("分析引擎", "analyze.py", 600),
                                                     ("构建页面", "build_dashboard.py", 120)):
                                STATUS["step"] = name
                                ok, _ = _run_step(name, script, to)
                                log(f"[RECHECK] {name}: {'OK' if ok else 'FAILED'}")
                                if not ok:
                                    break
                        finally:
                            STATUS["running"] = False
                            STATUS["step"] = None
                        if _last_nfgj_date() == today:
                            log("[RECHECK] 已补到当日份额")
        except Exception as e:
            log(f"[RECHECK] 异常 {type(e).__name__}: {e}")
        time.sleep(60)


MARGIN_CSV = os.path.join(DATA_DIR, "margin_balance.csv")
MARGIN_TIMES = [(21, 10), (22, 10), (23, 10)]


def _last_margin_date():
    try:
        with open(MARGIN_CSV, "r", encoding="utf-8-sig") as f:
            lines = f.read().strip().splitlines()
        return lines[-1].split(",")[0].replace("-", "") if len(lines) > 1 else None
    except Exception:
        return None


def _margin_recheck_loop():
    """交易所通常 21:00 后才披露当日两融：当晚按 21:10/22:10/23:10 补采一次。"""
    done = set()
    time.sleep(30)
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y%m%d")
            if now.weekday() < 5:
                for hh, mm in MARGIN_TIMES:
                    key = (today, hh, mm)
                    if now.hour * 60 + now.minute >= hh * 60 + mm and key not in done:
                        if (_last_margin_date() or "") >= today:
                            done.add(key)
                            log(f"[MARGIN] {hh:02d}:{mm:02d} 已有当日两融")
                            continue
                        if STATUS["running"]:
                            continue
                        done.add(key)
                        log(f"[MARGIN] {hh:02d}:{mm:02d} 当日两融缺失，自动补采")
                        STATUS["running"] = True
                        try:
                            STATUS["step"] = "融资余额"
                            ok, _ = _run_step("融资余额", "sync_margin.py", 600)
                            log(f"[MARGIN] 融资余额: {'OK' if ok else 'FAILED'}")
                        finally:
                            STATUS["running"] = False
                            STATUS["step"] = None
        except Exception as e:
            log(f"[MARGIN] 异常 {type(e).__name__}: {e}")
        time.sleep(60)


def _intraday_loop():
    """盘中每 15 分钟轻量同步（指数/宽度/拥挤度），15:05 触发一次收盘定版更新。"""
    time.sleep(15)
    last_sync = 0.0
    closed_done = None
    closed_checks = set()
    while True:
        try:
            now = datetime.now()
            hm = now.hour * 60 + now.minute
            today = now.strftime("%Y%m%d")
            trading = now.weekday() < 5
            in_session = trading and ((9 * 60 + 35 <= hm <= 11 * 60 + 30)
                                      or (13 * 60 <= hm <= 14 * 60 + 58))
            if in_session and not STATUS["running"] and time.time() - last_sync >= 900:
                last_sync = time.time()
                STATUS["running"] = True
                STATUS["step"] = "盘中同步"
                try:
                    ok, _ = _run_step("盘中同步", "sync_intraday.py", 420)
                    log(f"[INTRADAY] {'OK' if ok else 'FAILED'}")
                finally:
                    STATUS["running"] = False
                    STATUS["step"] = None
            if (trading and 15 * 60 + 5 <= hm <= 15 * 60 + 40
                    and closed_done != today and not STATUS["running"]):
                closed_done = today
                log("[CLOSE] 收盘定版更新")
                run_update()
            # 收盘后再校验数次，避免日线接口当晚仍停留在盘中值
            for hh, mm in ((15, 25), (15, 45), (16, 5)):
                key = (today, hh, mm)
                if (trading and hh * 60 + mm <= hm <= hh * 60 + mm + 20
                        and key not in closed_checks
                        and not STATUS["running"]):
                    closed_checks.add(key)
                    log(f"[CLOSE] {hh:02d}:{mm:02d} 收盘数据校验")
                    STATUS["running"] = True
                    STATUS["step"] = "收盘校验"
                    try:
                        ok, _ = _run_step("收盘校验", "sync_intraday.py", 420)
                        log(f"[CLOSE] 校验 {'OK' if ok else 'FAILED'}")
                    finally:
                        STATUS["running"] = False
                        STATUS["step"] = None
        except Exception as e:
            log(f"[INTRADAY] 异常 {type(e).__name__}: {e}")
        time.sleep(60)


def _morning_catchup_loop():
    """次日 08:40 兜底：昨晚没到的估值/份额/两融/市值，开盘前补齐。"""
    time.sleep(40)
    done = set()
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y%m%d")
            if now.weekday() < 5 and (8, 40) not in done and now.hour * 60 + now.minute >= 8 * 60 + 40:
                done.add((8, 40))
                # 昨日数据是否已齐（以上一交易日为基准）
                missing = []
                for f, name in (("valuation_000300.csv", "估值"), ("nfgj_hs300.csv", "ETF份额"),
                                ("margin_balance.csv", "两融"), ("cap_structure.csv", "市值")):
                    p = os.path.join(DATA_DIR, f)
                    if not os.path.exists(p):
                        missing.append(name)
                        continue
                    try:
                        with open(p, "r", encoding="utf-8-sig") as fh:
                            lines = fh.read().strip().splitlines()
                        last = lines[-1].split(",")[0].replace("-", "") if len(lines) > 1 else ""
                        if last < today:
                            missing.append(f"{name}({last})")
                    except Exception:
                        missing.append(name)
                if missing and not STATUS["running"]:
                    log(f"[MORNING] 数据缺失 {missing}，自动补采")
                    run_update()
                else:
                    log("[MORNING] 数据齐备，跳过补采")
        except Exception as e:
            log(f"[MORNING] 异常 {type(e).__name__}: {e}")
        time.sleep(60)


def start_realtime_thread():
    t = threading.Thread(target=_realtime_loop, daemon=True)
    t.start()


class APIHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_POST(self):
        if self.path == "/api/update":
            if STATUS["running"]:
                self.send_json(409, {"ok": False, "message": "更新正在运行中，请稍后再试"})
                return
            print("[API] Starting update thread...")
            t = threading.Thread(target=run_update, daemon=True)
            t.start()
            self.send_json(200, {"ok": True, "message": "更新已启动"})
        # CORS preflight: GET-like fallback for OPTIONS not handled
        elif self.path == "/api/update" and self.command == "OPTIONS":
            self.do_OPTIONS()
        else:
            self.send_json(404, {"ok": False, "message": "Not Found"})

    def do_GET(self):
        parsed = self.path.split("?")[0]
        if parsed == "/api/status":
            self.send_json(200, {
                "ok": True,
                "running": STATUS["running"],
                "step": STATUS.get("step"),
                "last_update": STATUS["last_update"],
                "last_result": STATUS["last_result"],
            })
        elif parsed == "/api/realtime":
            try:
                import fetch_rt
                self.send_json(200, fetch_rt.load_snapshot(max_age=60))
            except Exception as e:
                self.send_json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        elif parsed in ("/", "/dashboard.html", "/index.html"):
            self.path = "/dashboard.html"
            self._serve_file("text/html; charset=utf-8")
        elif parsed.startswith("/data/") or parsed.startswith("/static/"):
            self._serve_file(None)  # auto detect
        else:
            self._serve_file(None)

    def _serve_file(self, mime):
        """Serve a static file with proper MIME type.
        Never blocks — writes response synchronously."""
        file_path = self.translate_path(self.path)
        if not os.path.isfile(file_path):
            self.send_error(404, "Not Found")
            return
        try:
            with open(file_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            if mime:
                self.send_header("Content-Type", mime)
            elif file_path.endswith(".json"):
                self.send_header("Content-Type", "application/json; charset=utf-8")
            elif file_path.endswith(".js"):
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
            elif file_path.endswith(".css"):
                self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(500, str(e))

    def send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")


def main():
    port = 4323
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), APIHandler)
    log(f"服务器启动 http://localhost:{port} (python={PYTHON})")
    start_realtime_thread()
    threading.Thread(target=_intraday_loop, daemon=True).start()
    threading.Thread(target=_margin_recheck_loop, daemon=True).start()
    threading.Thread(target=_morning_catchup_loop, daemon=True).start()
    threading.Thread(target=_evening_recheck_loop, daemon=True).start()
    print(f"\n{'='*60}")
    print(f"  微观指标跟踪服务器")
    print(f"  http://localhost:{port}")
    print(f"  POST /api/update -> 触发全流程更新")
    print(f"  GET  /api/status  -> 查看更新状态")
    print(f"  按 Ctrl+C 停止")
    print(f"{'='*60}\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("服务器已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
