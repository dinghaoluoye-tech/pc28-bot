#!/usr/bin/env python3
"""
最终诊断版：启动后强制补全历史，订阅持久化，并输出调试日志
"""
import os, json, asyncio, logging
from pathlib import Path
import httpx
from telegram import Update, Bot
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackContext

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TARGET_CHAT_ID = os.environ.get("TARGET_CHAT_ID", "")
DATA_DIR = Path("/data")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
DELAY_SECONDS = int(os.environ.get("DELAY_SECONDS", "0"))
API_URL = "https://dp28-engine.vercel.app/api/pc28"
MAX_WINDOW = 11
MAX_HISTORY = 500
COMBO_ORDER = ["小单", "小双", "大单", "大双"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("pc28-bot")
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ==================== 工具函数 ====================
def get_combo(s):
    return "大单" if s >= 14 and s % 2 == 1 else "大双" if s >= 14 and s % 2 == 0 else "小单" if s < 14 and s % 2 == 1 else "小双"

def count_window(win):
    cnt = {"小单": 0, "小双": 0, "大单": 0, "大双": 0}
    for i in win: cnt[i["combo"]] += 1
    return cnt

def detect_morph(win):
    if len(win) < 5: return {"triggered": False}
    single_run = 0
    for i in win:
        if "单" in i["combo"]: single_run += 1
        else: break
    if single_run >= 4: return {"triggered": True, "type": "连续出单", "strength": "extreme" if single_run >= 5 else "strong", "detail": f"近{single_run}期全是单"}
    double_run = 0
    for i in win:
        if "双" in i["combo"]: double_run += 1
        else: break
    if double_run >= 4: return {"triggered": True, "type": "连续出双", "strength": "extreme" if double_run >= 5 else "strong", "detail": f"近{double_run}期全是双"}
    r5 = win[:5]
    dsxd = sum(1 for i in r5 if i["combo"] in ("大双", "小单"))
    if dsxd == 5: return {"triggered": True, "type": "大双小单交替", "strength": "strong", "detail": "近5期全部为大双+小单"}
    ddxs = sum(1 for i in r5 if i["combo"] in ("大单", "小双"))
    if ddxs == 5: return {"triggered": True, "type": "大单小双交替", "strength": "strong", "detail": "近5期全部为大单+小双"}
    big_run = 0
    for i in win:
        if i["sum"] >= 14: big_run += 1
        else: break
    if big_run >= 4: return {"triggered": True, "type": "连续出大", "strength": "extreme" if big_run >= 5 else "strong", "detail": f"近{big_run}期全部≥14"}
    small_run = 0
    for i in win:
        if i["sum"] < 14: small_run += 1
        else: break
    if small_run >= 4: return {"triggered": True, "type": "连续出小", "strength": "extreme" if small_run >= 5 else "strong", "detail": f"近{small_run}期全部<14"}
    od_seq = ["单" if "单" in i["combo"] else "双" for i in r5]
    if all(od_seq[i] != od_seq[i-1] for i in range(1, 5)) and len(od_seq) == 5:
        return {"triggered": True, "type": "单双跳", "strength": "strong", "detail": "近5期单双交替"}
    bs_seq = ["大" if i["sum"] >= 14 else "小" for i in r5]
    if all(bs_seq[i] != bs_seq[i-1] for i in range(1, 5)) and len(bs_seq) == 5:
        return {"triggered": True, "type": "大小跳", "strength": "strong", "detail": "近5期大小交替"}
    return {"triggered": False}

def get_recommendation(win, state):
    cnt = count_window(win)
    morph = detect_morph(win)
    a = state.get("a", {"period": 1, "rec": ""})
    b = state.get("b", {"period": 1, "rec": "", "isHot": False, "hotCombo": ""})
    if morph["triggered"]:
        mapping = {"连续出单": ("大单", "小单"), "连续出双": ("大双", "小双"), "大双小单交替": ("大双", "小单"), "大单小双交替": ("大单", "小双"),
                   "连续出大": ("大双", "大单"), "连续出小": ("小双", "小单"), "单双跳": ("大单", "小双"), "大小跳": ("大单", "小双")}
        a_rec, b_rec = mapping.get(morph["type"], ("大双", "小双"))
        return {"a": a_rec, "b": b_rec, "aPeriod": 1, "bPeriod": 1, "cnt": cnt, "isMorph": True, "needNewA": True, "needNewB": True}
    need_new_a = not a["rec"] or a["period"] == 1
    need_new_b = not b["rec"] or b["period"] == 1
    new_a = a["rec"]; new_b = b["rec"]
    if need_new_a:
        asc = sorted(cnt.keys(), key=lambda k: cnt[k])
        coldest = asc[0]
        tie = [k for k in cnt if cnt[k] == cnt[coldest]]
        tie.sort(key=lambda k: COMBO_ORDER.index(k))
        new_a = tie[0]
    if need_new_b:
        remaining = [k for k in cnt if k != new_a]
        remaining.sort(key=lambda k: cnt[k])
        b_candidate = remaining[0]
        max_cnt = max(cnt.values())
        if max_cnt >= 3:
            hot = [k for k in cnt if cnt[k] == max_cnt]
            hot.sort(key=lambda k: COMBO_ORDER.index(k))
            hot_combo = hot[0]
            hot_miss = 0
            for item in win:
                if item["combo"] == hot_combo: break
                hot_miss += 1
            if hot_miss < 2 and hot_combo != new_a:
                b_candidate = hot_combo
        new_b = b_candidate
    return {"a": new_a, "b": new_b, "aPeriod": a["period"], "bPeriod": b["period"], "cnt": cnt, "isMorph": False, "needNewA": need_new_a, "needNewB": need_new_b}

def update_state(latest, win, state):
    a = state["a"]; b = state["b"]
    if latest:
        if latest == a["rec"]: a["period"] = 1; a["rec"] = ""
        else:
            a["period"] += 1
            if a["period"] > 3: a["period"] = 1; a["rec"] = ""
        if latest == b["rec"]: b["period"] = 1; b["rec"] = ""; b["isHot"] = False; b["hotCombo"] = ""
        else:
            b["period"] += 1
            if b.get("isHot") and b.get("hotCombo"):
                miss = 0
                for item in win:
                    if item["combo"] == b["hotCombo"]: break
                    miss += 1
                if miss >= 2: b["period"] = 1; b["rec"] = ""; b["isHot"] = False; b["hotCombo"] = ""
            if b["period"] > 3: b["period"] = 1; b["rec"] = ""; b["isHot"] = False; b["hotCombo"] = ""
    return state

def apply_recommendation(result, state):
    if result["isMorph"]:
        state["a"]["rec"] = result["a"]; state["a"]["period"] = 1
        state["b"]["rec"] = result["b"]; state["b"]["period"] = 1
    else:
        if result.get("needNewA"): state["a"]["rec"] = result["a"]; state["a"]["period"] = 1
        if result.get("needNewB"): state["b"]["rec"] = result["b"]; state["b"]["period"] = 1
    return state

def load_json(filename, default=None):
    if default is None: default = {}
    filepath = DATA_DIR / filename
    try:
        if filepath.exists(): return json.loads(filepath.read_text(encoding="utf-8"))
    except: pass
    return default

def save_json(filename, data):
    filepath = DATA_DIR / filename
    try: filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e: logger.error(f"保存 {filename} 失败: {e}")

# ==================== 全局状态 ====================
yinyu_state = load_json("state.json", {"a": {"period": 1, "rec": ""}, "b": {"period": 1, "rec": "", "isHot": False, "hotCombo": ""}})
last_period = load_json("last_period.json", {"period": ""})["period"]
api_data = []
history = load_json("history.json", [])
subscribers = set(load_json("subscribers.json", []))

def save_all_state():
    save_json("state.json", yinyu_state)
    save_json("last_period.json", {"period": last_period})
    save_json("history.json", history)
    save_json("subscribers.json", list(subscribers))

def calc_plan_stats(hist):
    if len(hist) < 1: return [], 0, 0
    sorted_hist = sorted(hist, key=lambda x: int(x["period"]))
    plans = []
    i = 0
    while i < len(sorted_hist):
        start = sorted_hist[i]["period"]
        hit = False; end_idx = i
        for j in range(3):
            idx = i + j
            if idx >= len(sorted_hist): break
            if sorted_hist[idx]["hitA"] or sorted_hist[idx]["hitB"]:
                end_idx = idx; hit = True; break
            end_idx = idx
        if hit:
            plans.append({"range": f"{start}～{sorted_hist[end_idx]['period']}", "success": True})
            i = end_idx + 1
        else:
            if i + 2 < len(sorted_hist): end = sorted_hist[i+2]["period"]
            else: end = sorted_hist[-1]["period"]
            plans.append({"range": f"{start}～{end}", "success": False})
            i += 3
    recent = plans[-10:] if len(plans) >= 10 else plans
    total = len(hist)
    hits = sum(1 for h in hist if h["hitA"] or h["hitB"])
    return recent, total, hits

async def fetch_api_data():
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
    except Exception as e: logger.error(f"API错误: {e}")
    return []

async def initial_history_fill():
    """启动时主动补全历史记录，基于API最新数据"""
    global api_data, history
    if not api_data:
        data = await fetch_api_data()
        if data:
            api_data = data[:MAX_WINDOW]
    if len(api_data) < 5:
        logger.info("历史补全：API数据不足，跳过")
        return
    win_fill = api_data[1:MAX_WINDOW]  # 用于模拟推荐窗口
    new_records = 0
    for item in api_data:
        if any(h["period"] == item["period"] for h in history): continue
        temp_state = json.loads(json.dumps(yinyu_state))
        rec = get_recommendation(win_fill, temp_state)
        hit_a = (item["combo"] == rec["a"]); hit_b = (item["combo"] == rec["b"])
        record = {"period": item["period"], "actual": item["combo"], "nums": item["nums"], "sum": item["sum"],
                  "predA": rec["a"], "predB": rec["b"], "hitA": hit_a, "hitB": hit_b, "mode": "补全"}
        history.insert(0, record)
        new_records += 1
    if new_records > 0:
        # 去重排序
        seen = set(); new_hist = []
        for h in history:
            if h["period"] not in seen:
                seen.add(h["period"]); new_hist.append(h)
        new_hist.sort(key=lambda x: int(x["period"]), reverse=True)
        history = new_hist[:MAX_HISTORY]
        save_json("history.json", history)
        logger.info(f"历史补全完成，新增 {new_records} 条记录，当前总记录 {len(history)} 期")
    else:
        logger.info("历史补全：无新记录")

# ==================== 主循环 ====================
async def check_and_push(bot: Bot):
    global api_data, last_period, yinyu_state, history, prev_state
    new_data = await fetch_api_data()
    if not new_data: return

    latest_period = new_data[0]["period"]
    existing = {item["period"] for item in api_data}
    fresh = [item for item in new_data if item["period"] not in existing]
    if fresh:
        api_data = fresh + api_data
        if len(api_data) > MAX_WINDOW: api_data = api_data[:MAX_WINDOW]

    # 每次有新数据时，也尝试补全历史（保证不遗漏）
    if len(api_data) >= 5:
        win_fill = api_data[1:MAX_WINDOW]
        for item in api_data:
            if item["period"] == last_period: continue
            if any(h["period"] == item["period"] for h in history): continue
            temp_state = json.loads(json.dumps(yinyu_state))
            rec = get_recommendation(win_fill, temp_state)
            hit_a = (item["combo"] == rec["a"]); hit_b = (item["combo"] == rec["b"])
            record = {"period": item["period"], "actual": item["combo"], "nums": item["nums"], "sum": item["sum"],
                      "predA": rec["a"], "predB": rec["b"], "hitA": hit_a, "hitB": hit_b, "mode": "补全"}
            history.insert(0, record)
        seen = set(); new_hist = []
        for h in history:
            if h["period"] not in seen:
                seen.add(h["period"]); new_hist.append(h)
        new_hist.sort(key=lambda x: int(x["period"]), reverse=True)
        history = new_hist[:MAX_HISTORY]

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
        record = {"period": api_data[0]["period"], "actual": actual, "nums": api_data[0]["nums"], "sum": api_data[0]["sum"],
                  "predA": prev_state["a"]["rec"], "predB": prev_state["b"]["rec"], "hitA": hit_a, "hitB": hit_b, "mode": "正常"}
        history.insert(0, record)
        if len(history) > MAX_HISTORY: history = history[:MAX_HISTORY]

    recent_plans, total_periods, hit_periods = calc_plan_stats(history)
    if total_periods > 0:
        rate = hit_periods / total_periods * 100
        plan_info = f"💡 单期命中率：{rate:.0f}% ({hit_periods}/{total_periods})\n"
    else:
        plan_info = "💡 单期命中率：暂无数据\n"

    plan_lines = "\n".join(f"{p['range']} {'✅' if p['success'] else '❌'}" for p in recent_plans) + "\n"

    a_display = f"{result['a']} 第{result['aPeriod']}期"
    b_display = f"{result['b']} 第{result['bPeriod']}期"
    curr = api_data[0]
    nums_str = "+".join(str(n) for n in curr["nums"])
    combo_str = curr["combo"]

    review_lines = ""
    if prev_state and prev_state.get("a", {}).get("rec"):
        hit_a_emoji = "✅" if curr["combo"] == prev_state["a"]["rec"] else "❌"
        hit_b_emoji = "✅" if curr["combo"] == prev_state["b"]["rec"] else "❌"
        review_lines = f"📊 <b>上期推荐回顾</b>\n🔵 A线：{prev_state['a']['rec']} {hit_a_emoji}\n🔴 B线：{prev_state['b']['rec']} {hit_b_emoji}\n"

    valid_end = str(int(curr["period"]) + 2)

    message = (
        f"🎯 <b>第 {curr['period']} 期 开奖结果</b>\n"
        f"号码：{nums_str} = <b>{curr['sum']}</b>\n"
        f"组合：【{combo_str}】\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{review_lines}"
        f"━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>本期推荐</b>\n"
        f"🔵 A线：{a_display}\n"
        f"🔴 B线：{b_display}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⏰ 有效期：{curr['period']}～{valid_end}\n"
        f"{plan_info}"
        f"━━━━━━━━━━━━━━━━\n"
        f"{plan_lines}"
    )

    if DELAY_SECONDS > 0: await asyncio.sleep(DELAY_SECONDS)

    if not subscribers:
        logger.warning("没有订阅群组，跳过推送")
    else:
        for chat_id in list(subscribers):
            try:
                await bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.HTML)
                logger.info(f"已推送到 {chat_id}")
            except Exception as e:
                logger.error(f"推送失败 {chat_id}: {e}")
                if "Forbidden" in str(e): subscribers.discard(chat_id)

    last_period = latest_period
    save_all_state()

# ==================== 命令处理 ====================
async def cmd_start(update, context):
    await update.message.reply_text("🎯 游刃有余双冷方案\n命令：/subscribe /unsubscribe /status /stats /history /help", parse_mode=ParseMode.HTML)

async def cmd_subscribe(update, context):
    chat_id = update.effective_chat.id
    if chat_id in subscribers:
        await update.message.reply_text("✅ 本群已订阅")
    else:
        subscribers.add(chat_id)
        save_all_state()
        await update.message.reply_text("✅ 订阅成功！")

async def cmd_unsubscribe(update, context):
    chat_id = update.effective_chat.id
    if chat_id in subscribers:
        subscribers.discard(chat_id)
        save_all_state()
        await update.message.reply_text("❌ 已取消订阅")
    else:
        await update.message.reply_text("本群尚未订阅")

async def cmd_status(update, context):
    if not api_data or len(api_data) < 5: await update.message.reply_text("⏳ 数据加载中..."); return
    win = api_data[:min(MAX_WINDOW, len(api_data))]
    result = get_recommendation(win, yinyu_state)
    cnt = result["cnt"]
    recent_plans, total_periods, hit_periods = calc_plan_stats(history)
    if total_periods > 0:
        rate = hit_periods / total_periods * 100
        plan_str = f"💡 单期命中率：{rate:.0f}% ({hit_periods}/{total_periods})"
    else: plan_str = "暂无数据"
    msg = (f"📊 当前状态\n期号：{api_data[0]['period']}\n推荐：A {result['a']} 第{result['aPeriod']}期 / B {result['b']} 第{result['bPeriod']}期\n"
           f"窗口统计：小单{cnt['小单']} 小双{cnt['小双']} 大单{cnt['大单']} 大双{cnt['大双']}\n{plan_str}\n📈 历史记录：{len(history)} 期\n📢 订阅群组：{len(subscribers)} 个")
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_stats(update, context):
    if not api_data: await update.message.reply_text("数据不足"); return
    win = api_data[:min(MAX_WINDOW, len(api_data))]
    cnt = count_window(win)
    seq_od = "-".join(["单" if "单" in i["combo"] else "双" for i in win])
    seq_bs = "-".join(["大" if i["sum"] >= 14 else "小" for i in win])
    msg = f"📊 窗口统计（{len(win)}期）\n小单{cnt['小单']} 小双{cnt['小双']} 大单{cnt['大单']} 大双{cnt['大双']}\n单双序列：{seq_od}\n大小序列：{seq_bs}"
    await update.message.reply_text(msg)

async def cmd_history(update, context):
    if not history: await update.message.reply_text("暂无记录"); return
    lines = ["📜 最近10期对错："]
    for r in history[:10]:
        ha = "✅" if r["hitA"] else "❌"; hb = "✅" if r["hitB"] else "❌"
        nums_str = "+".join(str(n) for n in r.get("nums", []))
        lines.append(f"{r['period']} {nums_str}={r.get('sum', '?')} {r['actual']} A{ha} B{hb}")
    await update.message.reply_text("\n".join(lines))

async def cmd_help(update, context): await update.message.reply_text("/subscribe /unsubscribe /status /stats /history /help")
async def cmd_chatid(update, context): await update.message.reply_text(f"Chat ID: <code>{update.effective_chat.id}</code>", parse_mode=ParseMode.HTML)

async def polling_job(context): await check_and_push(context.bot)

async def error_handler(update, context): logger.error(f"错误: {context.error}")

def main():
    if not BOT_TOKEN: logger.error("❌ 未设置 BOT_TOKEN"); return
    global subscribers
    if TARGET_CHAT_ID:
        for cid in TARGET_CHAT_ID.split(","):
            cid = cid.strip()
            if cid:
                try: subscribers.add(int(cid))
                except: subscribers.add(cid)

    # 启动时加载已有的订阅列表（已从 json 加载）
    save_all_state()

    # 使用 asyncio 事件循环初始化后运行补全
    loop = asyncio.get_event_loop()
    loop.run_until_complete(initial_history_fill())

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
    logger.info("🚀 Polling模式启动")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
