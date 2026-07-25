#!/usr/bin/env python3
import os, json, asyncio, logging
import httpx
from telegram import Update, Bot
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackContext

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
DELAY_SECONDS = int(os.environ.get("DELAY_SECONDS", "0"))
API_URL = "https://dp28-engine.vercel.app/api/pc28"
MAX_WINDOW = 11
COMBO_ORDER = ["小单","小双","大单","大双"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("pc28-bot")

def get_combo(s): return "大单" if s>=14 and s%2==1 else "大双" if s>=14 and s%2==0 else "小单" if s<14 and s%2==1 else "小双"
def count_window(win): 
    cnt = {"小单":0,"小双":0,"大单":0,"大双":0}
    for i in win: cnt[i["combo"]]+=1
    return cnt

def detect_morph(win):
    if len(win)<5: return {"triggered":False}
    for name,check in [("单","连续出单"),("双","连续出双")]:
        run=0
        for i in win:
            if name in i["combo"]: run+=1
            else: break
        if run>=4: return {"triggered":True,"type":check,"detail":f"近{run}期全是{name}"}
    r5=win[:5]
    if sum(1 for i in r5 if i["combo"] in ("大双","小单"))==5: return {"triggered":True,"type":"大双小单交替","detail":"近5期全部为大双+小单"}
    if sum(1 for i in r5 if i["combo"] in ("大单","小双"))==5: return {"triggered":True,"type":"大单小双交替","detail":"近5期全部为大单+小双"}
    big_run=0; small_run=0
    for i in win:
        if i["sum"]>=14: big_run+=1
        else: break
    if big_run>=4: return {"triggered":True,"type":"连续出大","detail":f"近{big_run}期全部≥14"}
    for i in win:
        if i["sum"]<14: small_run+=1
        else: break
    if small_run>=4: return {"triggered":True,"type":"连续出小","detail":f"近{small_run}期全部<14"}
    od_seq=["单" if "单" in i["combo"] else "双" for i in r5]
    if all(od_seq[i]!=od_seq[i-1] for i in range(1,5)): return {"triggered":True,"type":"单双跳","detail":"近5期单双交替"}
    bs_seq=["大" if i["sum"]>=14 else "小" for i in r5]
    if all(bs_seq[i]!=bs_seq[i-1] for i in range(1,5)): return {"triggered":True,"type":"大小跳","detail":"近5期大小交替"}
    return {"triggered":False}

def get_recommendation(win, state):
    cnt=count_window(win)
    morph=detect_morph(win)
    a=state.get("a",{"period":1,"rec":""})
    b=state.get("b",{"period":1,"rec":""})
    if morph["triggered"]:
        m={"连续出单":("大单","小单"),"连续出双":("大双","小双"),"大双小单交替":("大双","小单"),"大单小双交替":("大单","小双"),"连续出大":("大双","大单"),"连续出小":("小双","小单"),"单双跳":("大单","小双"),"大小跳":("大单","小双")}
        a_rec,b_rec=m.get(morph["type"],("大双","小双"))
        return {"a":a_rec,"b":b_rec,"aPeriod":1,"bPeriod":1,"cnt":cnt,"isMorph":True}
    need_new_a=not a["rec"] or a["period"]==1
    need_new_b=not b["rec"] or b["period"]==1
    new_a=a["rec"]; new_b=b["rec"]
    if need_new_a:
        asc=sorted(cnt.keys(),key=lambda k:cnt[k])
        tie=[k for k in cnt if cnt[k]==cnt[asc[0]]]
        tie.sort(key=lambda k:COMBO_ORDER.index(k))
        new_a=tie[0]
    if need_new_b:
        remaining=[k for k in cnt if k!=new_a]
        remaining.sort(key=lambda k:cnt[k])
        new_b=remaining[0]
        max_cnt=max(cnt.values())
        if max_cnt>=3:
            hot=[k for k in cnt if cnt[k]==max_cnt]
            hot.sort(key=lambda k:COMBO_ORDER.index(k))
            hot_combo=hot[0]
            hot_miss=0
            for item in win:
                if item["combo"]==hot_combo: break
                hot_miss+=1
            if hot_miss<2 and hot_combo!=new_a: new_b=hot_combo
    return {"a":new_a,"b":new_b,"aPeriod":a["period"],"bPeriod":b["period"],"cnt":cnt}

api_data = []
history = []   # {"period":..., "hitA":bool, "hitB":bool}
yinyu_state = {"a":{"period":1,"rec":""},"b":{"period":1,"rec":""}}
last_period = ""
subscribers = set()

def calc_plan_stats(hist):
    if len(hist)<1: return [],0,0
    sorted_hist=sorted(hist,key=lambda x:int(x["period"]))
    plans=[]
    i=0
    while i<len(sorted_hist):
        start=sorted_hist[i]["period"]
        hit=False; end_idx=i
        for j in range(3):
            idx=i+j
            if idx>=len(sorted_hist): break
            if sorted_hist[idx]["hitA"] or sorted_hist[idx]["hitB"]:
                end_idx=idx; hit=True; break
            end_idx=idx
        if hit:
            plans.append({"range":f"{start}～{sorted_hist[end_idx]['period']}","success":True})
            i=end_idx+1
        else:
            if i+2<len(sorted_hist): end=sorted_hist[i+2]["period"]
            else: end=sorted_hist[-1]["period"]
            plans.append({"range":f"{start}～{end}","success":False})
            i+=3
    recent=plans[-10:] if len(plans)>=10 else plans
    total=len(hist)
    hits=sum(1 for h in hist if h["hitA"] or h["hitB"])
    return recent,total,hits

async def fetch_api_data():
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp=await client.get(API_URL)
            data=resp.json()
            if data.get("success") and data.get("data"):
                result=[]
                for item in data["data"]:
                    n1=int(item.get("openCode1",0)); n2=int(item.get("openCode2",0)); n3=int(item.get("openCode3",0))
                    s=n1+n2+n3
                    result.append({"period":item.get("section",""),"nums":[n1,n2,n3],"sum":s,"combo":get_combo(s)})
                return result
    except Exception as e: logger.error(f"API错误: {e}")
    return []

async def startup_fill_history():
    global api_data, history
    logger.info("正在启动补全历史...")
    data = await fetch_api_data()
    if data:
        api_data = data[:MAX_WINDOW]
        win_fill = api_data[1:]
        temp_state = {"a":{"period":1,"rec":""},"b":{"period":1,"rec":""}}
        for item in api_data:
            if any(h["period"]==item["period"] for h in history): continue
            rec = get_recommendation(win_fill, temp_state)
            hit_a = (item["combo"]==rec["a"]); hit_b = (item["combo"]==rec["b"])
            history.append({"period":item["period"],"hitA":hit_a,"hitB":hit_b})
        history.sort(key=lambda x:int(x["period"]), reverse=True)
        logger.info(f"补全完成，历史记录 {len(history)} 期")

async def check_and_push(bot: Bot):
    global api_data, last_period, yinyu_state, history
    new_data=await fetch_api_data()
    if not new_data: return
    latest_period=new_data[0]["period"]
    existing={item["period"] for item in api_data}
    fresh=[item for item in new_data if item["period"] not in existing]
    if fresh:
        api_data=fresh+api_data
        if len(api_data)>MAX_WINDOW: api_data=api_data[:MAX_WINDOW]

    # 每次推送前补全历史（不足30条时）
    if len(history) < 30 and len(api_data) >= 5:
        win_fill = api_data[1:]
        temp_state = {"a":{"period":1,"rec":""},"b":{"period":1,"rec":""}}
        for item in api_data:
            if any(h["period"]==item["period"] for h in history): continue
            rec = get_recommendation(win_fill, temp_state)
            hit_a = (item["combo"]==rec["a"]); hit_b = (item["combo"]==rec["b"])
            history.append({"period":item["period"],"hitA":hit_a,"hitB":hit_b})
        seen=set(); new_hist=[]
        for h in history:
            if h["period"] not in seen:
                seen.add(h["period"]); new_hist.append(h)
        new_hist.sort(key=lambda x:int(x["period"]), reverse=True)
        history = new_hist[:500]

    if latest_period==last_period: return
    logger.info(f"新期号: {latest_period}")
    if len(api_data)<5: last_period=latest_period; return

    win=api_data[:min(MAX_WINDOW,len(api_data))]
    prev_state=json.loads(json.dumps(yinyu_state))
    yinyu_state=update_state(api_data[0]["combo"],win,yinyu_state)
    result=get_recommendation(win,yinyu_state)
    yinyu_state=apply_recommendation(result,yinyu_state)

    if prev_state["a"]["rec"] and prev_state["b"]["rec"]:
        actual=api_data[0]["combo"]
        hit_a=(actual==prev_state["a"]["rec"]); hit_b=(actual==prev_state["b"]["rec"])
        history.insert(0, {"period":api_data[0]["period"],"hitA":hit_a,"hitB":hit_b})
        if len(history)>500: history=history[:500]

    recent_plans,total_periods,hit_periods=calc_plan_stats(history)
    plan_info = f"💡 单期命中率：{hit_periods/total_periods*100:.0f}% ({hit_periods}/{total_periods})\n" if total_periods else "暂无数据\n"
    plan_lines = "\n".join(f"{p['range']} {'✅' if p['success'] else '❌'}" for p in recent_plans) + "\n"

    a_display=f"{result['a']} 第{result['aPeriod']}期"
    b_display=f"{result['b']} 第{result['bPeriod']}期"
    curr=api_data[0]
    nums_str="+".join(str(n) for n in curr["nums"])
    combo_str=curr["combo"]

    review_lines=""
    if prev_state["a"]["rec"]:
        ha="✅" if curr["combo"]==prev_state["a"]["rec"] else "❌"
        hb="✅" if curr["combo"]==prev_state["b"]["rec"] else "❌"
        review_lines=f"📊 <b>上期推荐回顾</b>\n🔵 A线：{prev_state['a']['rec']} {ha}\n🔴 B线：{prev_state['b']['rec']} {hb}\n"

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
        f"⏰ 有效期：{curr['period']}～{int(curr['period'])+2}\n"
        f"{plan_info}"
        f"━━━━━━━━━━━━━━━━\n"
        f"{plan_lines}"
    )

    if DELAY_SECONDS>0: await asyncio.sleep(DELAY_SECONDS)

    for chat_id in list(subscribers):
        try:
            await bot.send_message(chat_id=chat_id,text=message,parse_mode=ParseMode.HTML)
            logger.info(f"已推送到 {chat_id}")
        except Exception as e:
            logger.error(f"推送失败 {chat_id}: {e}")

    last_period=latest_period

def update_state(latest,win,state):
    a=state["a"]; b=state["b"]
    if latest:
        if latest==a["rec"]: a["period"]=1; a["rec"]=""
        else:
            a["period"]+=1
            if a["period"]>3: a["period"]=1; a["rec"]=""
        if latest==b["rec"]: b["period"]=1; b["rec"]=""
        else:
            b["period"]+=1
            if b["period"]>3: b["period"]=1; b["rec"]=""
    return state

def apply_recommendation(result,state):
    if result.get("isMorph"):
        state["a"]["rec"]=result["a"]; state["a"]["period"]=1
        state["b"]["rec"]=result["b"]; state["b"]["period"]=1
    else:
        if result.get("needNewA"): state["a"]["rec"]=result["a"]; state["a"]["period"]=1
        if result.get("needNewB"): state["b"]["rec"]=result["b"]; state["b"]["period"]=1
    return state

async def cmd_start(update,context): await update.message.reply_text("🎯 游刃有余双冷方案\n命令：/subscribe /unsubscribe /status /stats /history /help",parse_mode=ParseMode.HTML)
async def cmd_subscribe(update,context):
    chat_id=update.effective_chat.id
    if chat_id in subscribers: await update.message.reply_text("✅ 本群已订阅")
    else: subscribers.add(chat_id); await update.message.reply_text("✅ 订阅成功！")
async def cmd_unsubscribe(update,context):
    chat_id=update.effective_chat.id
    if chat_id in subscribers: subscribers.discard(chat_id); await update.message.reply_text("❌ 已取消订阅")
    else: await update.message.reply_text("本群尚未订阅")
async def cmd_status(update,context):
    if not api_data: await update.message.reply_text("⏳ 数据加载中..."); return
    win=api_data[:min(MAX_WINDOW,len(api_data))]
    result=get_recommendation(win,yinyu_state)
    cnt=result["cnt"]
    recent_plans,total_periods,hit_periods=calc_plan_stats(history)
    plan_str = f"💡 单期命中率：{hit_periods/total_periods*100:.0f}% ({hit_periods}/{total_periods})" if total_periods else "暂无"
    msg = (f"📊 当前状态\n期号：{api_data[0]['period']}\n推荐：A {result['a']} 第{result['aPeriod']}期 / B {result['b']} 第{result['bPeriod']}期\n"
           f"窗口统计：小单{cnt['小单']} 小双{cnt['小双']} 大单{cnt['大单']} 大双{cnt['大双']}\n{plan_str}\n📈 历史记录：{len(history)} 期")
    await update.message.reply_text(msg,parse_mode=ParseMode.HTML)
async def cmd_stats(update,context):
    if not api_data: await update.message.reply_text("数据不足"); return
    win=api_data[:min(MAX_WINDOW,len(api_data))]
    cnt=count_window(win)
    seq_od="-".join(["单" if "单" in i["combo"] else "双" for i in win])
    seq_bs="-".join(["大" if i["sum"]>=14 else "小" for i in win])
    msg = f"📊 窗口统计（{len(win)}期）\n小单{cnt['小单']} 小双{cnt['小双']} 大单{cnt['大单']} 大双{cnt['大双']}\n单双序列：{seq_od}\n大小序列：{seq_bs}"
    await update.message.reply_text(msg)
async def cmd_history(update,context):
    if not history: await update.message.reply_text("暂无记录"); return
    lines=["📜 最近10期对错："]
    for r in history[:10]:
        ha="✅" if r["hitA"] else "❌"; hb="✅" if r["hitB"] else "❌"
        lines.append(f"{r['period']} A{ha} B{hb}")
    await update.message.reply_text("\n".join(lines))
async def cmd_help(update,context): await update.message.reply_text("/subscribe /unsubscribe /status /stats /history /help")
async def cmd_chatid(update,context): await update.message.reply_text(f"Chat ID: <code>{update.effective_chat.id}</code>",parse_mode=ParseMode.HTML)

async def polling_job(context): await check_and_push(context.bot)
async def error_handler(update,context): logger.error(f"错误: {context.error}")

def main():
    if not BOT_TOKEN: logger.error("❌ 未设置 BOT_TOKEN"); return
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
    # 启动后立即补全历史
    loop = asyncio.get_event_loop()
    loop.create_task(startup_fill_history())
    logger.info("🚀 Polling模式启动"); app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
