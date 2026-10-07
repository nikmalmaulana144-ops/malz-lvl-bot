# -*- coding: utf-8 -*-
# dashboard_server.py — MALZ CODEX Web Dashboard Server
# Author: malz codex
# Embedded Async Web Server for Railway / Termux / VPS

import asyncio
import json
import os
import time
import gzip
from typing import Dict, List, Any, Optional
from aiohttp import web

# ============================================================
# PATHS
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("MALZ_DATA_DIR", os.path.join(BASE_DIR, "data"))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
TEMPLATE_PATH = os.path.join(TEMPLATE_DIR, "index.html")

os.makedirs(DATA_DIR, exist_ok=True)


def data_path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


# ============================================================
# BOT STATE
# ============================================================
class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 300
        self.total_matches = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        self.match_feed: List[Dict[str, Any]] = []
        self.match_feed_max = 60

    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            "uid": uid,
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def feed_match(self, uid: str, nickname: str, gained: int, region: str = "ID"):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "ts": time.time(),
            "uid": uid,
            "nickname": nickname,
            "gained": gained,
            "region": region,
        }
        self.match_feed.append(entry)
        if len(self.match_feed) > self.match_feed_max:
            self.match_feed.pop(0)

    def register_account(self, uid: str, nickname: str, region: str,
                         level: int, exp: int, likes: int = 0):
        uid_str = str(uid)
        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str,
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD",
                "level": level or 1,
                "initial_exp": exp,
                "current_exp": exp,
                "gained_exp": 0,
                "likes": likes or 0,
                "status": "ONLINE",
                "matches_played": 0,
                "active_matches": 0,
                "last_match_time": None,
                "last_updated": time.strftime("%H:%M:%S"),
                "history": [],
            }
        else:
            acc = self.accounts[uid_str]
            if nickname:
                acc["nickname"] = nickname
            if region:
                acc["region"] = region
            if level:
                acc["level"] = level
            acc["current_exp"] = exp
            acc["gained_exp"] = max(0, exp - acc["initial_exp"])
            acc["likes"] = likes
            acc["status"] = "ONLINE"
            acc["last_updated"] = time.strftime("%H:%M:%S")
        self.recalc_totals()

    def update_exp(self, uid: str, current_exp: int, level: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            old_exp = acc["current_exp"]
            acc["current_exp"] = current_exp
            if level is not None and level > 0:
                acc["level"] = level
            acc["gained_exp"] = max(0, current_exp - acc["initial_exp"])
            acc["last_updated"] = time.strftime("%H:%M:%S")

            hist = acc.setdefault("history", [])
            hist.append({"t": time.time(), "v": acc["gained_exp"]})
            if len(hist) > 120:
                hist.pop(0)

            diff = current_exp - old_exp
            if diff > 0:
                self.log(
                    f"Account {acc['nickname']} ({uid_str}) gained +{diff} EXP! "
                    f"Total Gained: +{acc['gained_exp']}",
                    "success", uid_str,
                )
            self.recalc_totals()

    def update_status(self, uid: str, status: str,
                      active_matches: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = status
            if active_matches is not None:
                self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match(self, uid: str):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            acc["matches_played"] += 1
            acc["last_match_time"] = time.strftime("%H:%M:%S")
            acc["last_updated"] = time.strftime("%H:%M:%S")
            self.log(
                f"Account {acc['nickname']} finished Match #{acc['matches_played']}",
                "info", uid_str,
            )
            self.feed_match(uid_str, acc["nickname"],
                            acc.get("gained_exp", 0), acc.get("region", "ID"))

    def recalc_totals(self):
        self.total_gained_exp = sum(acc.get("gained_exp", 0)
                                    for acc in self.accounts.values())


bot_state = BotState()


# ============================================================
# RESPONSE HELPERS
# ============================================================
def json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


async def read_json_file(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return default
        return json.loads(content)
    except Exception:
        return default


async def write_json_file(path: str, data: Any) -> bool:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# ============================================================
# HTTP HANDLERS
# ============================================================
async def handle_index(request: web.Request) -> web.Response:
    if os.path.exists(TEMPLATE_PATH):
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        return web.Response(
            text=content,
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-cache"},
        )
    return web.Response(text="<h1>templates/index.html not found</h1>",
                        content_type="text/html", status=500)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "uptime": int(time.time() - bot_state.start_time),
        "accounts": len(bot_state.accounts),
        "matches": bot_state.total_matches,
    })


async def handle_get_stats(request: web.Request) -> web.Response:
    accounts_data = list(bot_state.accounts.values())
    accounts_data.sort(key=lambda x: x.get("gained_exp", 0), reverse=True)

    now = time.time()
    uptime = int(now - bot_state.start_time)
    hours = max(uptime / 3600, 0.0001)
    exp_per_hour = int(bot_state.total_gained_exp / hours) if uptime > 60 else 0
    match_per_hour = int(bot_state.total_matches / hours) if uptime > 60 else 0

    return json_response({
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_gained_exp": bot_state.total_gained_exp,
        "exp_per_hour": exp_per_hour,
        "match_per_hour": match_per_hour,
        "accounts": accounts_data,
        "logs": bot_state.logs[-80:],
        "match_feed": bot_state.match_feed[-30:],
        "uptime": uptime,
        "server_time": now,
    })


async def handle_add_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return json_response({"status": "error", "error": "Invalid JSON body"}, 400)

    accounts_file = data_path("accounts.json")
    existing = await read_json_file(accounts_file, [])
    if not isinstance(existing, list):
        existing = []

    if "uid" in data and "password" in data:
        uid = str(data["uid"]).strip()
        pwd = str(data["password"]).strip()
        if not uid or not pwd:
            return json_response({"status": "error",
                                  "error": "UID dan Password wajib diisi"}, 400)
        if not uid.isdigit() or len(uid) < 5:
            return json_response({"status": "error",
                                  "error": "UID harus angka (min 5 digit)"}, 400)
        existing = [a for a in existing if str(a.get("uid")) != uid]
        existing.append({"uid": uid, "password": pwd})
    elif "token" in data:
        token = str(data["token"]).strip()
        if not token:
            return json_response({"status": "error", "error": "Token wajib diisi"}, 400)
        existing = [a for a in existing if a.get("token") != token]
        existing.append({"token": token})
    else:
        return json_response({"status": "error", "error": "Payload tidak valid"}, 400)

    if not await write_json_file(accounts_file, existing):
        return json_response({"status": "error", "error": "Gagal simpan file"}, 500)

    bot_state.log(f"New account added: {data.get('uid') or 'Token'}", "success")

    cb = bot_state.refresh_callbacks.get("on_account_added")
    if cb:
        asyncio.create_task(cb(data))

    return json_response({"status": "ok"})


async def handle_delete_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return json_response({"status": "error", "error": "Invalid JSON"}, 400)

    uid = str(data.get("uid", "")).strip()
    if not uid:
        return json_response({"status": "error", "error": "UID wajib diisi"}, 400)

    accounts_file = data_path("accounts.json")
    existing = await read_json_file(accounts_file, [])
    if isinstance(existing, list):
        existing = [a for a in existing if str(a.get("uid")) != uid]
        await write_json_file(accounts_file, existing)

    if uid in bot_state.accounts:
        del bot_state.accounts[uid]
    if uid in bot_state.account_workers:
        bot_state.account_workers[uid].cancel()
        del bot_state.account_workers[uid]

    bot_state.log(f"Account {uid} removed from rotation.", "warning", uid)
    bot_state.recalc_totals()
    return json_response({"status": "ok"})


async def handle_bulk_delete(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return json_response({"status": "error", "error": "Invalid JSON"}, 400)

    uids = data.get("uids", [])
    if not isinstance(uids, list) or not uids:
        return json_response({"status": "error", "error": "uids kosong"}, 400)

    uids_set = {str(u).strip() for u in uids}

    accounts_file = data_path("accounts.json")
    existing = await read_json_file(accounts_file, [])
    if isinstance(existing, list):
        existing = [a for a in existing if str(a.get("uid")) not in uids_set]
        await write_json_file(accounts_file, existing)

    for uid in uids_set:
        if uid in bot_state.accounts:
            del bot_state.accounts[uid]
        if uid in bot_state.account_workers:
            bot_state.account_workers[uid].cancel()
            del bot_state.account_workers[uid]

    bot_state.log(f"Bulk delete: {len(uids_set)} akun dihapus", "warning")
    bot_state.recalc_totals()
    return json_response({"status": "ok", "deleted": len(uids_set)})


async def handle_refresh_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return json_response({"status": "error", "error": "Invalid JSON"}, 400)

    uid = str(data.get("uid", "")).strip()
    if not uid:
        return json_response({"status": "error", "error": "UID wajib diisi"}, 400)

    cb = bot_state.refresh_callbacks.get("on_refresh_account")
    if cb:
        asyncio.create_task(cb(uid))
    return json_response({"status": "ok"})


async def handle_export_accounts(request: web.Request) -> web.Response:
    accounts_file = data_path("accounts.json")
    data = await read_json_file(accounts_file, [])
    return json_response({
        "status": "ok",
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(data) if isinstance(data, list) else 0,
        "accounts": data if isinstance(data, list) else [],
    })


async def handle_import_accounts(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return json_response({"status": "error", "error": "Invalid JSON"}, 400)

    incoming = data.get("accounts", [])
    if not isinstance(incoming, list):
        return json_response({"status": "error", "error": "Format akun salah"}, 400)

    mode = data.get("mode", "merge")
    accounts_file = data_path("accounts.json")

    if mode == "replace":
        merged = []
    else:
        merged = await read_json_file(accounts_file, [])
        if not isinstance(merged, list):
            merged = []

    seen = set()
    for acc in merged:
        if isinstance(acc, dict):
            key = str(acc.get("uid") or acc.get("token") or "")
            if key:
                seen.add(key)

    added = 0
    for acc in incoming:
        if not isinstance(acc, dict):
            continue
        key = str(acc.get("uid") or acc.get("token") or "")
        if not key or key in seen:
            continue
        merged.append(acc)
        seen.add(key)
        added += 1

    if not await write_json_file(accounts_file, merged):
        return json_response({"status": "error", "error": "Gagal simpan"}, 500)

    bot_state.log(f"Import: {added} akun baru ditambahkan", "success")

    cb = bot_state.refresh_callbacks.get("on_account_added")
    if cb:
        for acc in incoming:
            if isinstance(acc, dict):
                asyncio.create_task(cb(acc))

    return json_response({"status": "ok", "added": added, "total": len(merged)})


async def handle_get_account_detail(request: web.Request) -> web.Response:
    uid = request.match_info.get("uid", "").strip()
    if not uid:
        return json_response({"status": "error", "error": "UID kosong"}, 400)
    acc = bot_state.accounts.get(uid)
    if not acc:
        return json_response({"status": "error", "error": "Akun tidak ditemukan"}, 404)
    return json_response({"status": "ok", "account": acc})


async def handle_clear_logs(request: web.Request) -> web.Response:
    bot_state.logs = []
    return json_response({"status": "ok"})


# ============================================================
# MIDDLEWARE
# ============================================================
@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })
    try:
        response = await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        return json_response({"status": "error", "error": str(e)}, 500)

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


# ============================================================
# APP FACTORY
# ============================================================
def build_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/stats", handle_get_stats)
    app.router.add_get("/api/account/{uid}", handle_get_account_detail)
    app.router.add_get("/api/accounts/export", handle_export_accounts)
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/bulk-delete", handle_bulk_delete)
    app.router.add_post("/api/account/refresh", handle_refresh_account)
    app.router.add_post("/api/accounts/import", handle_import_accounts)
    app.router.add_post("/api/logs/clear", handle_clear_logs)
    return app


async def start_web_dashboard(host: str = "0.0.0.0", port: Optional[int] = None):
    if port is None:
        port = int(os.environ.get("PORT", 8080))

    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    print(f"\033[92m[+] Web Dashboard running on http://{host}:{port}\033[0m")
    print(f"\033[96m[+] Local: http://localhost:{port}\033[0m")
    print(f"\033[96m[+] Health: http://localhost:{port}/health\033[0m")
    return runner