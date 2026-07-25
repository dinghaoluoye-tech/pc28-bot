#!/usr/bin/env python3
"""
游刃有余双冷方案 · Telegram 自动推送机器人
- 历史记录持久化到 /data 目录
- 滚动显示最近10个三期计划
- 单期命中率 = 历史总命中比例
- 可配置延迟发布（环境变量 DELAY_SECONDS）
"""

import os
import json
import asyncio
import logging
from pathlib import Path
from typing import Optional

import httpx
from telegram import Update, Bot
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackContext
)

# ==================== 配置 ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TARGET_CHAT_ID = os.environ.get("TARGET_CHAT_ID", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))   # ★ 改为 /data 持久化
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
DELAY_SECONDS = int(os.environ.get("DELAY_SECONDS", "0"))
API_URL = "https://dp28-engine.vercel.app/api/pc28"
MAX_WINDOW = 11
MAX_HISTORY = 500
COMBO_ORDER = ["小单", "小双", "大单", "大双"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("pc28-bot")

DATA_DIR.mkdir(parents=True, exist_ok=True)

# ==================== 核心逻辑 ====================

def get_combo(sum_val: int) -> str:
    if sum_val >= 14 and sum_val % 2 == 1: return "大单"
    if sum_val >= 14 and sum_val % 2 == 0: return "大双"
    if sum_val < 14 and sum_val % 2 == 1: return "小单"
    return "小双"


def count_window(win: list) -> dict:
    cnt = {"小单": 0, "小双": 0, "大单": 0, "大双": 0}
    for item in win:
        cnt[item["combo"]] += 1
    return cnt


def detect_morph(win: list) -> dict:
    if len(win) < 5: return {"triggered": False}
    single_run = 0
    for item in win:
        if "单" in item["combo"]: single_run += 1
        else: break
    if single_run >= 4: return {"triggered": True, "type": "连续出单", "strength": "extreme" if single_run >= 5 else "strong", "detail": f"近{single_run}期全是单"}
    double_run = 0
    for item in win:
        if "双" in item["combo"]: double_run += 1
        else: break
    if double_run >= 4: return {"triggered": True, "type": "连续出双", "strength": "extreme" if double_run >= 5 else "strong", "detail": f"近{double_run}期全是双"}
    r5 = win[:5]
    dsxd = sum(1 for i in r5 if i["combo"] in ("大双", "小单"))
    if dsxd == 5: return {"triggered": True, "type": "大双小单交替", "strength": "strong", "detail": "近5期全部为大双+小单"}
    ddxs = sum(1 for i in r5 if i["combo"] in ("大单", "小双"))
    if ddxs == 5: return {"triggered": True, "type": "大单小双交替", "strength": "strong", "detail": "近5期全部为大单+小双"}
    big_run = 0
    for item in win:
        if item["sum"] >= 14: big_run += 1
        else: break
    if big_run >= 4: return {"triggered": True, "type": "连续出大", "strength": "extreme" if big_run >= 5 else "strong", "detail": f"近{big_run}期全部≥14"}
    small_run = 0
    for item in win:
        if item["sum"] < 14: small_run += 1
        else: break
    if small_run >= 4: return {"triggered": True, "type": "连续出小", "strength": "extreme" if small_run >= 5 else "strong", "detail": f"近{small_run}期全部<14"}
    od_seq = ["单" if "单" in i["combo"] else "双" for i in r5]
    if all(od_seq[i] != od_seq[i-1] for i in range(1, len(od_seq))) and len(od_seq) == 5:
        return {"triggered": True, "type": "单双跳", "strength": "strong", "detail": "近5期单双交替"}
    bs_seq = ["大" if i["sum"] >= 14 else "小" for i in r5]
    if all(bs_seq[i] != bs_seq[i-1] for i in range(1, len(bs_seq))) and len(bs_seq) == 5:
        return {"triggered": True, "type": "大小跳", "strength": "strong", "detail": "近5期大小交替"}
    return {"triggered": False}


def get_recommendation(win: list, state: dict) -> dict:
    cnt = count_window(win)
    morph = detect_morph(win)
    a = state.get("a", {"period": 1, "rec": ""})
    b = state.get("b", {"period": 1, "rec": "", "isHot": False, "hotCombo": ""})
    if morph["triggered"]:
        mapping = {
            "连续出单": ("大单", "小单"), "连续出双": ("大双", "小双"),
            "大双小单交替": ("大双", "小单"), "大单小双交替": ("大单", "小双"),
            "连续出大": ("大双", "大单"), "连续出小": ("小双", "小单"),
            "单双跳": ("大单", "小双"), "大小跳": ("大单", "小双"),
        }
        a_rec, b_rec = mapping.get(morph["type"], ("大双", "小双"))
        return {"a": a_rec, "b": b_rec, "aPeriod": 1, "bPeriod": 1, "morph": morph, "cnt": cnt, "isMorph": True, "needNewA": True, "needNewB": True, "newBisHot": False}
    need_new_a = not a["rec"] or a["period"] == 1
    need_new_b = not b["rec"] or b["period"] == 1
    new_a = a["rec"]; new_b = b["rec"]
    new_b_is_hot = b.get("isHot", False)
    if need_new_a:
        sorted_asc = sorted(cnt.keys(), key=lambda k: cnt[k])
        coldest = sorted_asc[0]
        tie_group = [k for k in cnt if cnt[k] == cnt[coldest]]
        tie_group.sort(key=lambda k: COMBO_ORDER.index(k))
        new_a = tie_group[0]
    if need_new_b:
        remaining = [k for k in cnt if k != new_a]
        remaining.sort(key=lambda k: cnt[k])
        b_candidate = remaining[0]
        new_b_is_hot = False
        max_cnt = max(cnt.values()) if cnt else 0
        if max_cnt >= 3:
            hot_candidates = [k for k in cnt if cnt[k] == max_cnt]
            hot_candidates.sort(key=lambda k: COMBO_ORDER.index(k))
            hot_combo = hot_candidates[0]
            hot_miss = 0
            for item in win:
                if item["combo"] == hot_combo: break
                hot_miss += 1
            if hot_miss < 2 and hot_combo != new_a:
                b_candidate = hot_combo
                new_b_is_hot = True
        new_b = b_candidate
    return {"a": new_a, "b": new_b, "aPeriod": a["period"], "bPeriod": b["period"], "morph": morph, "cnt": cnt, "isMorph": False, "needNewA": need_new_a, "needNewB": need_new_b, "newBisHot": new_b_is_hot}


def update_state(latest_result: Optional[str], win: list, state: dict) -> dict:
    a = state.get("a", {"period": 1, "rec": ""})
    b = state.get("b", {"period": 1, "rec": "", "isHot": False, "hotCombo": ""})
    if latest_result:
        hit_a = (latest_result == a["rec"]); hit_b = (latest_result == b["rec"])
        if hit_a: a["period"] = 1; a["rec"] = ""
        else:
            a["period"] += 1
            if a["period"] > 3: a["period"] = 1; a["rec"] = ""
        if hit_b: b["period"] = 1; b["rec"] = ""; b["isHot"] = False; b["hotCombo"] = ""
        else:
            b["period"] += 1
            if b.get("isHot") and b.get("hotCombo"):
                hot_miss = 0
                for item in win:
                    if item["combo"] == b["hotCombo"]: break
                    hot_miss += 1
                if hot_miss >= 2: b["period"] = 1; b["rec"] = ""; b["isHot"] = False; b["hotCombo"] = ""
            if b["period"] > 3: b["period"] = 1; b["rec"] = ""; b["isHot"] = False; b["hotCombo"] = ""
    state["a"] = a; state["b"] = b
    return state


def apply_recommendation(result: dict, state: dict) -> dict:
    if result["isMorph"]:
        state["a"]["rec"] = result["a"]; state["a"]["period"] = 1
        state["b"]["rec"] = result["b"]; state["b"]["period"] = 1
        state["b"]["isHot"] = False; state["b"]["hotCombo"] = ""
    else:
        if result.get("needNewA"): state["a"]["rec"] = result["a"]; state["a"]["period"] = 1
        if result.get("needNewB"):
            state["b"]["rec"] = result["b"]; state["b"]["period"] = 1
            state["b"]["isHot"] = result.get("newBisHot", False)
            state["b"]["hotCombo"] = result["b"] if result.get("newBisHot") else ""
    return state


# ==================== 持久化 ====================

def load_json(filename: str, default=None):
    if default is None: default = {}
    filepath = DATA_DIR / filename
    try:
        if filepath.exists(): return json.loads(filepath.read_text(encoding="utf-8"))
    except Exception as e: logger.warning(f"加载 {filename} 失败: {e}")
    return default


def save_json(filename: str, data):
    filepath = DATA_DIR / filename
    try: filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e: logger.error(f"保存 {filename} 失败: {e}")


# ==================== 全局状态 ====================
yinyu_state = load_json("state.json", {"a": {"period": 1, "rec": ""}, "b": {"period": 1, "rec": "", "isHot": False, "hotCombo": ""}})
last_period = load_json("last_period.json", {"period": ""})["period"]
api_data = []
history = load_json("history.json", [])
prev_state = None
subscribers = set(load_json("subscribers.json", []))


def save_all_state():
    save_json("state.json", yinyu_state)
    save_json("last_period.json", {"period": last_period})
    save_json("history.json", history)
    save_json("subscribers.json", list(subscribers))


def fill_history_gaps():
    if len(api_data) < 5: return
    all_periods = list(reversed(api_data))
    for i in range(len(all_periods) - 4):
        target = all_periods[i]
        if any(h["period"] == target["period"] for h in history): continue
        future = all_periods[i+1:]
        if len(future) < 4: continue
        win = future[:MAX_WINDOW-1]
        if len(win) < 4: continue
        temp_state = json.loads(json.dumps(yinyu_state))
        rec = get_recommendation(win, temp_state)
        hit_a = (target["combo"] == rec["a"]); hit_b = (target["combo"] == rec["b"])
        record = {
            "period": target["period"], "actual": target["combo"],
            "nums": target["nums"], "sum": target["sum"],
            "predA": rec["a"], "predB": rec["b"],
            "hitA": hit_a, "hitB": hit_b,
            "mode": rec["morph"]["type"] if rec["morph"]["triggered"] else "正常"
        }
        history.append(record)
    seen = set()
    new_history = []
    for h in history:
        if h["period"] not in seen:
            seen.add(h["period"])
            new_history.append(h)
    new_history.sort(key=lambda x: int(x["period"]), reverse=True)
    history.clear()
    history.extend(new_history[:MAX_HISTORY])


def calc_plan_stats(hist):
    if len(hist) < 1: return [], 0, 0
    sorted_hist = sorted(hist, key=lambda x: int(x["period"]))
    plans = []
    i = 0
    while i < len(sorted_hist):
        start_period = sorted_hist[i]["period"]
        hit = False; end_idx = i
        for j in range(3):
            idx = i + j
            if idx >= len(sorted_hist): break
            if sorted_hist[idx]["hitA"] or sorted_hist[idx]["hitB"]:
                end_idx = idx; hit = True; break
            end_idx = idx
        if hit:
            end_period = sorted_hist[end_idx]["period"]
            plans.append({"range": f"{start_period}～{end_period}", "success": True})
            i = end_idx + 1
        else:
            if i + 2 < len(sorted_hist):
                end_period = sorted_hist[i+2]["period"]
            else:
                end_period = sorted_hist[-1]["period"]
            plans.append({"range": f"{start_period}～{end_period}", "success": False})
            i += 3
    recent_plans = plans[-10:] if len(plans) >= 10 else plans
    total_periods = len(hist)
    hit_periods = sum(1 for h in hist if h["hitA"] or h["hitB"])
    return recent_plans, total_periods, hit_periods


# ==================== 数据获取与分析 ====================

async def fetch_api_data() -> list:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(API_URL)
            data = resp.json()
            if data.get("success") and data.get("data"):
                result = []
                for item in data["data"]:
                    n1 = int(item.get("openCode1", 0)); n2 = int(item.get("openCode2", 0)); n3 = int(item.get("openCode3", 0))
                    s = n1 + n2 + n3
                    result.append({"period": item.get("section", ""), "nums": [n1, n2, n3], "sum": s, "combo": get_combo(s)})
                return result
    except Exception as e: logger.error(f"API请求失败: {e}")
    return []


async def check_and_push(bot: Bot):
    global api_data, last_period, yinyu_state, prev_state, history

    new_data = await fetch_api_data()
    if not new_data: return

    latest_period = new_data[0]["period"]
    existing_periods = {item["period"] for item in api_data}
    fresh_items = [item for item in new_data if item["period"] not in existing_periods]
    if fresh_items:
        api_data = fresh_items + api_data
        if len(api_data) > MAX_WINDOW: api_data = api_data[:MAX_WINDOW]

    if latest_period == last_period: return

    logger.info(f"新期号: {latest_period}")
    if len(api_data) < 5: last_period = latest_period; save_all_state(); return

    win = api_data[:min(MAX_WINDOW, len(api_data))]
    latest_combo = api_data[0]["combo"]

    prev_state = json.loads(json.dumps(yinyu_state))
    yinyu_state = update_state(latest_combo, win, yinyu_state)
    result = get_recommendation(win, yinyu_state)
    yinyu_state = apply_recommendation(result, yinyu_state)

    if prev_state and prev_state.get("a", {}).get("rec") and prev_state.get("b", {}).get("rec"):
        actual = api_data[0]["combo"]
        hit_a = (actual == prev_state["a"]["rec"]); hit_b = (actual == prev_state["b"]["rec"])
        last_win = api_data[1:min(MAX_WINDOW+1, len(api_data))]
        last_morph = detect_morph(last_win)
        record = {
            "period": api_data[0]["period"], "actual": actual,
            "nums": api_data[0]["nums"], "sum": api_data[0]["sum"],
            "predA": prev_state["a"]["rec"], "predB": prev_state["b"]["rec"],
            "hitA": hit_a, "hitB": hit_b,
            "mode": last_morph.get("type", "正常") if last_morph["triggered"] else "正常"
        }
        history.insert(0, record)
        if len(history) > MAX_HISTORY: history = history[:MAX_HISTORY]

    fill_history_gaps()

    recent_plans, total_periods, hit_periods = calc_plan_stats(history)
    if total_periods > 0:
        single_rate = hit_periods / total_periods * 100
        plan_info = f"💡 单期命中率：{single_rate:.0f}% ({hit_periods}/{total_periods})\n"
    else:
        plan_info = "💡 单期命中率：暂无数据\n"

    plan_lines = "\n".join(f"{p['range']} {'✅' if p['success'] else '❌'}" for p in recent_plans) + "\n"

    display_b = result["b"]
    a_display = f"{result['a']} 第{result['aPeriod']}期"
    b_display = f"{display_b} 第{result['bPeriod']}期"

    curr_item = api_data[0]
    curr_period = curr_item["period"]
    nums_str = "+".join(str(n) for n in curr_item["nums"])
    sum_val = curr_item["sum"]
    combo_str = curr_item["combo"]

    review_lines = ""
    if prev_state and prev_state.get("a", {}).get("rec"):
        actual_combo = api_data[0]["combo"]
        hit_a_emoji = "✅" if actual_combo == prev_state["a"]["rec"] else "❌"
        hit_b_emoji = "✅" if actual_combo == prev_state["b"]["rec"] else "❌"
        review_lines = (f"📊 <b>上期推荐回顾</b>\n🔵 A线：{prev_state['a']['rec']} {hit_a_emoji}\n🔴 B线：{prev_state['b']['rec']} {hit_b_emoji}\n")

    valid_end = str(int(curr_period) + 2)

    message = (
        f"🎯 <b>第 {curr_period} 期 开奖结果</b>\n"
        f"号码：{nums_str} = <b>{sum_val}</b>\n"
        f"组合：【{combo_str}】\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{review_lines}"
        f"━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>本期推荐</b>\n"
        f"🔵 A线：{a_display}\n"
        f"🔴 B线：{b_display}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⏰ 有效期：{curr_period}～{valid_end}\n"
        f"{plan_info}"
        f"━━━━━━━━━━━━━━━━\n"
        f"{plan_lines}"
    )

    if DELAY_SECONDS > 0:
        await asyncio.sleep(DELAY_SECONDS)

    if not subscribers:
        logger.warning("没有订阅群组，跳过推送")
    else:
        for chat_id in list(subscribers):
            try:
                await bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.HTML)
                logger.info(f"已推送到 {chat_id}")
            except Exception as e:
                logger.error(f"推送到 {chat_id} 失败: {e}")
                if "Forbidden" in str(e) or "not found" in str(e) or "kicked" in str(e).lower():
                    subscribers.discard(chat_id)

    last_period = latest_period
    save_all_state()


# ==================== 命令处理 ====================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎯 <b>游刃有余双冷方案</b>\n命令：/subscribe /unsubscribe /status /stats /history /help", parse_mode=ParseMode.HTML)

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in subscribers:
        await update.message.reply_text("✅ 本群已订阅")
    else:
        subscribers.add(chat_id); save_all_state()
        await update.message.reply_text("✅ 订阅成功！")

async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in subscribers:
        subscribers.discard(chat_id); save_all_state()
        await update.message.reply_text("❌ 已取消订阅")
    else:
        await update.message.reply_text("本群尚未订阅")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not api_data or len(api_data) < 5:
        await update.message.reply_text("⏳ 数据加载中..."); return
    win = api_data[:min(MAX_WINDOW, len(api_data))]
    result = get_recommendation(win, yinyu_state)
    cnt = result["cnt"]
    recent_plans, total_periods, hit_periods = calc_plan_stats(history)
    if total_periods > 0:
        single_rate = hit_periods / total_periods * 100
        plan_str = f"💡 单期命中率：{single_rate:.0f}% ({hit_periods}/{total_periods})"
    else:
        plan_str = "暂无数据"
    msg = (
        f"📊 <b>当前状态</b>\n期号：{api_data[0]['period']}\n"
        f"推荐：A {result['a']} 第{result['aPeriod']}期 / B {result['b']} 第{result['bPeriod']}期\n"
        f"窗口统计：小单{cnt['小单']} 小双{cnt['小双']} 大单{cnt['大单']} 大双{cnt['大双']}\n"
        f"{plan_str}\n📢 订阅群组：{len(subscribers)} 个"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not api_data or len(api_data) < 5: await update.message.reply_text("数据不足"); return
    win = api_data[:min(MAX_WINDOW, len(api_data))]
    cnt = count_window(win)
    seq_od = "-".join(["单" if "单" in i["combo"] else "双" for i in win])
    seq_bs = "-".join(["大" if i["sum"] >= 14 else "小" for i in win])
    msg = (f"📊 窗口统计（{len(win)}期）\n小单{cnt['小单']} 小双{cnt['小双']} 大单{cnt['大单']} 大双{cnt['大双']}\n单双序列：{seq_od}\n大小序列：{seq_bs}")
    await update.message.reply_text(msg)

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not history: await update.message.reply_text("暂无记录"); return
    lines = ["📜 最近10期对错："]
    for r in history[:10]:
        ha = "✅" if r["hitA"] else "❌"; hb = "✅" if r["hitB"] else "❌"
        nums_str = "+".join(str(n) for n in r.get("nums", []))
        lines.append(f"{r['period']} {nums_str}={r.get('sum','?')} {r['actual']} A{ha} B{hb}")
    await update.message.reply_text("\n".join(lines))

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/subscribe /unsubscribe /status /stats /history /help")

async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"Chat ID: <code>{chat_id}</code>", parse_mode=ParseMode.HTML)


async def polling_job(context: CallbackContext):
    await check_and_push(context.bot)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"错误: {context.error}")


def main():
    if not BOT_TOKEN: logger.error("❌ 未设置 BOT_TOKEN"); return
    global subscribers
    if TARGET_CHAT_ID:
        for cid in TARGET_CHAT_ID.split(","):
            cid = cid.strip()
            if cid:
                try: subscribers.add(int(cid))
                except ValueError: subscribers.add(cid)
    save_all_state()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_error_handler(error_handler)
    app.job_queue.run_repeating(polling_job, interval=POLL_INTERVAL, first=3)
    if WEBHOOK_URL:
        app.run_webhook(listen="0.0.0.0", port=int(os.environ.get("PORT", "8080")), url_path=BOT_TOKEN, webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    else:
        logger.info("🚀 Polling模式启动"); app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
