"""
繁星电商作战台 —— 综合版 v1.0
= 选品参谋 + 电商图片工厂 + 短视频工厂 三合一
外加「一键流水线」：选品库点一下 → 自动出详情图+短视频，打包下载
全免费：本地渲染 + pollinations 免费生图 + edge-tts 免费配音
用法：python app.py（浏览器 http://localhost:7866）｜ 生成桌面软件.bat（.exe）
"""

import os, sys, json, time, uuid, shutil, asyncio, subprocess, re, threading, random
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd

# ============================================================
# 一、配置
# ============================================================
DATA_DIR = Path("./data"); OUTPUT_DIR = Path("./output"); TEMP_DIR = Path("./temp")
for d in (DATA_DIR, OUTPUT_DIR, TEMP_DIR):
    d.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "command.db"

VIDEO_W, VIDEO_H, FPS = 1080, 1920, 30
MAIN_SIZE, DETAIL_W = 800, 750
MAX_WORKERS = 2

_lock = threading.Lock()

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def today():
    return datetime.now().strftime("%Y-%m-%d")

# ============================================================
# 二、数据库（选品库 + 笔记）
# ============================================================
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

import sqlite3

def init_db():
    with _lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS candidates(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, platform TEXT, category TEXT,
            cost REAL DEFAULT 0, price REAL DEFAULT 0, source_url TEXT DEFAULT '',
            score_demand INTEGER DEFAULT 3, score_compete INTEGER DEFAULT 3,
            score_profit INTEGER DEFAULT 3, score_supply INTEGER DEFAULT 3,
            trend_tag TEXT DEFAULT '新品测试', note TEXT DEFAULT '',
            status TEXT DEFAULT '评估中', created_at TEXT);
        CREATE TABLE IF NOT EXISTS selection_notes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT, tags TEXT DEFAULT '', created_at TEXT);
        """)

def q(sql, params=()):
    with _lock, db() as c:
        return c.execute(sql, params).fetchall()

def run(sql, params=()):
    with _lock, db() as c:
        c.execute(sql, params)
        c.commit()

# ============================================================
# 三、选品模块（趋势雷达 + 榜单台 + 工作台 + 笔记）
# ============================================================
TREND_TAGS = ["上升款", "爆发款", "平稳", "衰退", "新品测试"]

def trends_compare(keywords, region="CN", days=90):
    kws = [k.strip() for k in keywords.replace("，", ",").split(",") if k.strip()]
    if not kws:
        return None, "请输入关键词"
    try:
        from pytrends.request import TrendReq
    except ImportError:
        return None, "❌ 未安装 pytrends：pip install pytrends"
    try:
        pt = TrendReq(hl="zh-CN", tz=480, timeout=30)
        pt.build_payload(kws[:5], timeframe=f"today {days}-d", geo=region)
        df = pt.interest_over_time()
        if df is None or df.empty:
            return None, "无数据（热度太低或地区不支持）"
        if "isPartial" in df.columns:
            df = df.drop(columns=["isPartial"])
        report, out = [], {}
        for kw in kws[:5]:
            if kw not in df.columns:
                continue
            s = df[kw]
            first = s.iloc[: max(3, len(s)//10)].mean() or 0.1
            last = s.iloc[-max(3, len(s)//10):].mean()
            change = (last - first) / first
            tag = "爆发款🔥" if change > 1.5 else ("上升款📈" if change > 0.2 else ("衰退款📉" if change < -0.2 else "平稳"))
            report.append(f"**{kw}**：{tag}（峰值 {s.max()}，首尾 {change:+.0%}）")
            out[kw] = [int(x) for x in s]
        return pd.DataFrame({"日期": df.index.strftime("%Y-%m-%d"), **out}), \
            "📊 趋势解读：\n" + "\n".join(report) + "\n\n涨幅>150%=爆发，>20%=上升，<-20%=衰退"
    except Exception as e:
        return None, f"❌ 获取失败：{str(e)[:150]}（需可访问谷歌的网络；没有就用榜单台）"

PLATFORM_RANKS = [
    ("淘宝/天猫", "淘宝热搜榜", "当前人气搜索词", "https://s.taobao.com"),
    ("淘宝/天猫", "天猫榜单", "官方热卖/新品/回购榜", "https://top.tmall.com"),
    ("拼多多", "拼多多商家后台榜单", "热销/新品榜", "https://mms.pinduoduo.com"),
    ("拼多多", "1688 进货排行", "批发端热卖趋势", "https://index.1688.com"),
    ("抖音", "电商罗盘", "行业/商品榜（需商家号）", "https://compass.jinritemai.com"),
    ("抖音", "巨量算数", "关键词热度", "https://trendinsight.oceanengine.com"),
    ("抖音", "蝉妈妈免费版", "商品/达人榜", "https://www.chanmama.com"),
    ("亚马逊", "Best Sellers 热卖榜", "现在卖得最好", "https://www.amazon.com/gp/bestsellers"),
    ("亚马逊", "Movers & Shakers 飙升榜", "即将卖得好", "https://www.amazon.com/gp/movers-and-shakers"),
    ("亚马逊", "New Releases 新品榜", "刚上架卖爆", "https://www.amazon.com/gp/new-releases"),
    ("亚马逊", "Most Wished 心愿榜", "潜在爆款", "https://www.amazon.com/gp/most-wished-for"),
    ("跨境", "TikTok Creative Center", "热门广告/爆款视频", "https://ads.tiktok.com/business/creativecenter"),
    ("跨境", "Google Trends", "全球趋势", "https://trends.google.com"),
    ("跨境", "AliExpress 热销", "速卖通排行", "https://www.aliexpress.com"),
]

def ranks_markdown(platform="全部"):
    out, cur = [], None
    for pf, name, desc, url in PLATFORM_RANKS:
        if platform != "全部" and pf != platform:
            continue
        if pf != cur:
            cur = pf
            out.append(f"### {pf}")
        out.append(f"- **[{name}]({url})** — {desc}")
    return "\n".join(out)

def add_candidate(name, platform, category, cost, price, sd, sc, sp, ss, trend_tag, note, source_url=""):
    if not name.strip():
        return "❌ 请填产品名称"
    run("""INSERT INTO candidates(name,platform,category,cost,price,source_url,
           score_demand,score_compete,score_profit,score_supply,trend_tag,note,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name.strip(), platform, category, float(cost or 0), float(price or 0), source_url,
         int(sd), int(sc), int(sp), int(ss), trend_tag, note, now()))
    return f"✅ 已加入选品库：《{name}》"

def delete_candidate(cid):
    run("DELETE FROM candidates WHERE id=?", (int(cid),))
    return f"已删除 {cid}"

def set_candidate_status(cid, status):
    run("UPDATE candidates SET status=? WHERE id=?", (status, int(cid)))
    return f"已设为：{status}"

def candidates_df(status="全部"):
    if status == "全部":
        rows = q("SELECT * FROM candidates ORDER BY id DESC")
    else:
        rows = q("SELECT * FROM candidates WHERE status=? ORDER BY id DESC", (status,))
    out = []
    for r in rows:
        total = round(r["score_demand"]*0.3 + r["score_profit"]*0.3 +
                      r["score_compete"]*0.2 + r["score_supply"]*0.2, 2)
        verdict = "🔥重点" if total >= 4 else ("👀观察" if total >= 3 else "❌放弃")
        out.append({"ID": r["id"], "产品": r["name"], "平台": r["platform"], "分类": r["category"],
                    "成本": r["cost"], "售价": r["price"],
                    "综合": total, "结论": verdict, "趋势": r["trend_tag"], "状态": r["status"],
                    "备注": r["note"]})
    return pd.DataFrame(out)

def candidate_names():
    return [f"{r['id']}-{r['name']}" for r in q("SELECT id,name FROM candidates ORDER BY id")]

def stats_summary():
    rows = q("SELECT status, COUNT(*) n FROM candidates GROUP BY status")
    return {"选品库总数": q("SELECT COUNT(*) n FROM candidates")[0]["n"],
            "重点跟进": sum(r["n"] for r in rows if r["status"] == "重点跟进"),
            "评估中": sum(r["n"] for r in rows if r["status"] == "评估中"),
            "已放弃": sum(r["n"] for r in rows if r["status"] == "已放弃")}

def add_note(content, tags=""):
    if not content.strip():
        return "❌ 内容为空"
    run("INSERT INTO selection_notes(content,tags,created_at) VALUES(?,?,?)",
        (content.strip(), tags.strip(), now()))
    return "✅ 已记录"

def notes_df():
    rows = q("SELECT * FROM selection_notes ORDER BY id DESC LIMIT 300")
    return pd.DataFrame([{"ID": r["id"], "内容": r["content"], "标签": r["tags"], "时间": r["created_at"]} for r in rows])

def delete_note(nid):
    run("DELETE FROM selection_notes WHERE id=?", (int(nid),))
    return f"已删除 {nid}"

def seed_demo():
    init_db()
    if not q("SELECT 1 FROM candidates LIMIT 1"):
        add_candidate("宠物铃铛脖挂", "抖音", "宠物挂饰", 4, 18.9, 5, 3, 4, 5, "上升款", "对标月销3万+")
        add_candidate("车载香薰挂饰", "拼多多", "汽车挂饰", 6, 25.9, 4, 4, 4, 4, "平稳", "季节款注意切换")
        add_candidate("磁吸手机挂绳", "亚马逊", "手机配件", 8, 12.9, 3, 2, 3, 3, "新品测试", "测款中")
    if not q("SELECT 1 FROM selection_notes LIMIT 1"):
        add_note("作战台打通：选品库确认后 → 一键出详情图+短视频", "流程")
    return "✅ 示例数据已导入"

# ============================================================
# 四、生图（免费 pollinations）+ PIL 工具
# ============================================================
import requests

def pollinations_image(prompt, out_path, w, h, seed=None):
    import urllib.parse
    seed = seed if seed is not None else random.randint(0, 99999)
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
           + f"?width={w}&height={h}&nologo=true&seed={seed}")
    for attempt in range(2):
        try:
            r = requests.get(url, timeout=90)
            r.raise_for_status()
            if len(r.content) < 5000:
                raise ValueError("内容过小")
            Path(out_path).write_bytes(r.content)
            return True
        except Exception as e:
            print(f"  免费生图失败（{attempt+1}）：{e}")
            time.sleep(2)
    return False

from PIL import Image, ImageDraw, ImageFont, ImageFilter

_font_cache = {}
FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"]

def font(size):
    if size in _font_cache:
        return _font_cache[size]
    for pth in FONT_CANDIDATES:
        if Path(pth).exists():
            try:
                _font_cache[size] = ImageFont.truetype(pth, size)
                return _font_cache[size]
            except Exception:
                pass
    _font_cache[size] = ImageFont.load_default()
    return _font_cache[size]

def hex2rgb(h):
    return tuple(int(h[i:i+2], 16) for i in (1, 3, 5))

def gradient_bg(w, h, c1, c2):
    img = Image.new("RGBA", (w, h), hex2rgb(c1) + (255,))
    d = ImageDraw.Draw(img)
    r1, g1, b1 = hex2rgb(c1); r2, g2, b2 = hex2rgb(c2)
    for i in range(h):
        t = i / max(1, h - 1)
        d.line([(0, i), (w, i)], fill=(int(r1+(r2-r1)*t), int(g1+(g2-g1)*t), int(b1+(b2-b1)*t)))
    return img

def draw_centered(d, w, y, text, fnt, fill):
    d.text(((w - d.textlength(text, font=fnt)) / 2, y), text, font=fnt, fill=fill)

PALETTES = [("#0f2027", "#2c5364"), ("#42275a", "#734b6d"), ("#134e5e", "#71b280"),
            ("#3a1c71", "#d76d77"), ("#141e30", "#243b55")]

def make_text_card(text, out_path, subtitle="", palette_seed=0):
    c1, c2 = PALETTES[palette_seed % len(PALETTES)]
    img = gradient_bg(VIDEO_W, VIDEO_H, c1, c2)
    d = ImageDraw.Draw(img)
    f_big, f_small = font(72), font(40)
    lines, cur = [], ""
    for ch in text:
        if d.textlength(cur + ch, font=f_big) > VIDEO_W - 160:
            lines.append(cur); cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    y = VIDEO_H / 2 - len(lines) * 50
    for ln in lines:
        draw_centered(d, VIDEO_W, y, ln, f_big, "white")
        y += 100
    if subtitle:
        draw_centered(d, VIDEO_W, VIDEO_H - 220, subtitle, f_small, (220, 220, 220))
    img.save(out_path)
    return True

def remove_white_bg(img, thresh=238):
    img = img.convert("RGBA")
    px = img.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            if min(r, g, b) > 215 and max(r, g, b) - min(r, g, b) < 20:
                px[x, y] = (r, g, b, 0)
    return img

def fit_cutout(cutout, max_w, max_h):
    r = min(max_w / cutout.width, max_h / cutout.height)
    return cutout.resize((int(cutout.width * r), int(cutout.height * r)), Image.LANCZOS)

def shadow_paste(bg, cutout, pos, blur=18, alpha=90):
    sh = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    mask = cutout.split()[3].point(lambda a: min(a, alpha))
    sh.paste(Image.new("RGBA", cutout.size, (0, 0, 0, 255)), (pos[0]+8, pos[1]+12), mask)
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    bg.alpha_composite(sh)
    bg.alpha_composite(cutout, pos)

def _main_copy(product, brand, points, slogan, scenes, params):
    return {"slogan": slogan, "selling_points": points, "scenes": scenes,
            "params": params, "service": ["七天无理由退换", "运费险", "正品保障", "极速发货"]}

def make_main_images(cutout, copy, product, brand, palette_seed, tmp):
    c1, c2 = PALETTES[palette_seed % len(PALETTES)]
    cutout = fit_cutout(cutout, 520, 520)
    paths = []
    img = gradient_bg(MAIN_SIZE, MAIN_SIZE, "#ffffff", "#f2f2f2")
    shadow_paste(img, cutout, ((MAIN_SIZE-cutout.width)//2, (MAIN_SIZE-cutout.height)//2 - 20))
    d = ImageDraw.Draw(img)
    d.text((30, 30), brand, font=font(34), fill=hex2rgb(c1))
    p = str(Path(tmp)/"main_1.jpg"); img.convert("RGB").save(p, quality=92); paths.append(p)

    img = gradient_bg(MAIN_SIZE, MAIN_SIZE, c1, c2)
    d = ImageDraw.Draw(img)
    draw_centered(d, MAIN_SIZE, 90, copy["scenes"][0]["title"], font(52), "white")
    draw_centered(d, MAIN_SIZE, 165, copy["scenes"][0]["desc"], font(30), (235, 235, 235))
    shadow_paste(img, cutout, ((MAIN_SIZE-cutout.width)//2, 300))
    p = str(Path(tmp)/"main_2.jpg"); img.convert("RGB").save(p, quality=92); paths.append(p)

    img = gradient_bg(MAIN_SIZE, MAIN_SIZE, "#ffffff", "#ececec")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, MAIN_SIZE, 110], fill=hex2rgb(c1))
    draw_centered(d, MAIN_SIZE, 28, "核心卖点", font(48), "white")
    y = 170
    for pt in copy["selling_points"][:3]:
        d.rounded_rectangle([50, y, MAIN_SIZE-300, y+88], radius=16, fill=hex2rgb(c2))
        d.text((80, y+22), "✓ " + pt, font=font(30), fill="white")
        y += 120
    shadow_paste(img, fit_cutout(cutout, 240, 240), (MAIN_SIZE-280, MAIN_SIZE-300))
    p = str(Path(tmp)/"main_3.jpg"); img.convert("RGB").save(p, quality=92); paths.append(p)

    img = gradient_bg(MAIN_SIZE, MAIN_SIZE, c1, c2, )
    d = ImageDraw.Draw(img)
    d.ellipse([MAIN_SIZE-230, 40, MAIN_SIZE-40, 230], fill="#ff4757")
    tw = d.textlength("限时特惠", font=font(34))
    d.text((MAIN_SIZE-135-tw/2, 105), "限时特惠", font=font(34), fill="white")
    draw_centered(d, MAIN_SIZE, 250, "新品上市", font(72), "white")
    draw_centered(d, MAIN_SIZE, 350, copy["slogan"], font(34), (255, 235, 160))
    shadow_paste(img, fit_cutout(cutout, 380, 330), ((MAIN_SIZE-380)//2, 430))
    p = str(Path(tmp)/"main_4.jpg"); img.convert("RGB").save(p, quality=92); paths.append(p)

    img = gradient_bg(MAIN_SIZE, MAIN_SIZE, "#ffffff", "#f5f5f5")
    d = ImageDraw.Draw(img)
    d.text((50, 40), "产品参数", font=font(46), fill=hex2rgb(c1))
    y = 130
    for item in copy["params"][:5]:
        d.text((60, y), str(item["k"]), font=font(26), fill=(110, 110, 110))
        d.text((300, y), str(item["v"]), font=font(26), fill=(40, 40, 40))
        y += 62
    shadow_paste(img, fit_cutout(cutout, 260, 260), (MAIN_SIZE-300, MAIN_SIZE-310))
    p = str(Path(tmp)/"main_5.jpg"); img.convert("RGB").save(p, quality=92); paths.append(p)
    return paths

def make_detail_page(cutout, copy, product, brand, palette_seed, tmp):
    c1, c2 = PALETTES[palette_seed % len(PALETTES)]
    modules, W = [], DETAIL_W
    poster = gradient_bg(W, 820, c1, c2)
    d = ImageDraw.Draw(poster)
    sc_img = str(Path(tmp)/"scene_0.jpg")
    if pollinations_image(f"{product}，电商场景海报，专业产品摄影，高清，无文字", sc_img, W, 820):
        poster = Image.open(sc_img).convert("RGBA").resize((W, 820))
        poster.alpha_composite(Image.new("RGBA", (W, 820), hex2rgb(c1) + (140,)))
        d = ImageDraw.Draw(poster)
    draw_centered(d, W, 240, brand, font(56), "white")
    draw_centered(d, W, 330, product, font(64), "white")
    draw_centered(d, W, 430, copy["slogan"], font(34), (255, 235, 160))
    modules.append(poster.convert("RGB"))

    h = 150 + len(copy["selling_points"]) * 105
    m = gradient_bg(W, h, "#ffffff", "#f7f7f7")
    d = ImageDraw.Draw(m)
    d.text((50, 45), "核心卖点", font=font(44), fill=hex2rgb(c1))
    y = 140
    for pt in copy["selling_points"]:
        d.rounded_rectangle([50, y, W-50, y+80], radius=14, fill=(245, 245, 248))
        d.text((80, y+21), "✓ " + pt, font=font(30), fill=(35, 35, 35))
        y += 105
    modules.append(m)

    m = gradient_bg(W, 700, "#ffffff", "#efefef")
    d = ImageDraw.Draw(m)
    d.text((50, 40), "细节展示", font=font(44), fill=hex2rgb(c1))
    big = fit_cutout(cutout, 560, 500)
    shadow_paste(m, big, ((W-big.width)//2, 150))
    modules.append(m)

    for i, sc in enumerate(copy["scenes"][:2]):
        sc_img = str(Path(tmp)/f"scene_{i+1}.jpg")
        m = gradient_bg(W, 640, c1, c2)
        if pollinations_image(f"{product}，{sc['title']}场景，生活方式摄影，自然光，高清，无文字", sc_img, W, 480):
            m.paste(Image.open(sc_img).convert("RGB").resize((W, 480)), (0, 0))
        d = ImageDraw.Draw(m)
        d.rectangle([0, 480, W, 640], fill=hex2rgb(c1))
        d.text((50, 500), sc["title"], font=font(40), fill="white")
        d.text((50, 560), sc["desc"], font=font(26), fill=(225, 225, 225))
        modules.append(m)

    h = 140 + len(copy["params"]) * 64 + 40
    m = gradient_bg(W, h, "#ffffff", "#fafafa")
    d = ImageDraw.Draw(m)
    d.text((50, 40), "规格参数", font=font(44), fill=hex2rgb(c1))
    y = 130
    for item in copy["params"]:
        d.text((60, y), str(item["k"]), font=font(26), fill=(120, 120, 120))
        d.text((280, y), str(item["v"]), font=font(26), fill=(40, 40, 40))
        y += 64
    modules.append(m)

    m = gradient_bg(W, 260, c1, c2)
    d = ImageDraw.Draw(m)
    draw_centered(d, W, 40, "售后保障", font(40), "white")
    for i, s in enumerate(copy["service"][:4]):
        d.text((60 + (i % 2) * (W//2 - 40), 120 + (i // 2) * 64), "★ " + s, font=font(28), fill=(240, 240, 240))
    modules.append(m)

    page = Image.new("RGB", (W, sum(m.height for m in modules)), "white")
    y = 0
    for m in modules:
        page.paste(m, (0, y)); y += m.height
    out = str(Path(tmp)/"detail_page.jpg")
    page.save(out, quality=90)
    return out

# ============================================================
# 五、短视频引擎（本地模板文案 + 免费TTS + 本地运镜）
# ============================================================
def _run_ffmpeg(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg 失败：{r.stderr[-300:]}")

def _probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0

def local_storyboard(product, category="挂饰"):
    """无AI Key的模板分镜：5段口播 + 镜头"""
    lines = [f"{product}，最近卖得太火了", "为什么这么多人回购，今天讲清楚",
             "第一，用料扎实，细节看得见", "第二，价格打到地板，源头直发", "想要的点击下方，手慢无"]
    shots = ["推轨", "特写", "快切", "环绕", "定格"]
    return {"title": product[:15], "scenes": [
        {"line": l, "image_prompt": f"{product}，{l}，电商短视频画面，竖屏", "camera": shots[i]}
        for i, l in enumerate(lines)]}

def generate_tts(text, out_path, rate="+0%"):
    try:
        import edge_tts
        async def _r():
            await edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural", rate=rate).save(out_path)
        asyncio.run(_r())
        if Path(out_path).exists() and Path(out_path).stat().st_size > 0:
            return out_path, True
    except Exception as e:
        print(f"  TTS失败，用静音：{e}")
    est = max(2.5, min(8.0, len(text) * 0.28 + 0.8))
    _run_ffmpeg(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                 "-t", f"{est:.2f}", "-q:a", "9", "-acodec", "libmp3lame", out_path])
    return out_path, False

def merge_audios(paths, out):
    if len(paths) == 1:
        shutil.copy(paths[0], out)
        return out
    lst = Path(out).with_suffix(".txt")
    lst.write_text("".join(f"file '{Path(p).resolve()}'\n" for p in paths), encoding="utf-8")
    _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", out])
    return out

def write_srt(lines, durations, srt_path):
    def fmt(s):
        h, m = int(s // 3600), int((s % 3600) // 60)
        return f"{h:02d}:{m:02d}:{s % 60:06.3f}".replace(".", ",")
    t = 0.0
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, (line, d) in enumerate(zip(lines, durations), 1):
            f.write(f"{i}\n{fmt(max(t,0))} --> {fmt(t+d)}\n{line.strip()}\n\n")
            t += d
    return srt_path

def free_motion_video(image_path, camera, duration, out_path):
    n = max(1, int(round(duration * FPS)))
    center = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    if "拉" in camera:
        zexpr, xy = f"max(1.18-0.18*on/{n},1.0)", center
    elif "环绕" in camera or "摇" in camera:
        zexpr, xy = "1.25", f"x='(iw-iw/zoom)*on/{n}':y='ih/2-(ih/zoom/2)'"
    elif "定格" in camera:
        zexpr, xy = "1.0", center
    elif "特写" in camera or "推" in camera or "轨" in camera:
        zexpr, xy = f"min(1+0.18*on/{n},1.18)", center
    else:
        zexpr, xy = f"1+0.06*on/{n}", center
    vf = (f"scale={VIDEO_W*2}:{VIDEO_H*2},"
          f"zoompan=z='{zexpr}':{xy}:d={n}:s={VIDEO_W}x{VIDEO_H}:fps={FPS}")
    _run_ffmpeg(["ffmpeg", "-y", "-i", image_path, "-vf", vf, "-t", f"{duration:.2f}",
                 "-c:v", "libx264", "-preset", "fast", out_path])
    return out_path

def _normalize_clip(clip, out, target_dur):
    norm = str(Path(out).with_name(Path(out).stem + "_tmp.mp4"))
    _run_ffmpeg(["ffmpeg", "-y", "-i", clip,
                 "-vf", f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=increase,"
                        f"crop={VIDEO_W}:{VIDEO_H},setsar=1,fps={FPS}",
                 "-c:v", "libx264", "-preset", "fast", "-an", norm])
    _run_ffmpeg(["ffmpeg", "-y", "-stream_loop", "-1", "-i", norm,
                 "-t", f"{target_dur:.2f}", "-c:v", "libx264", "-preset", "fast", "-an", out])
    Path(norm).unlink(missing_ok=True)

def _xfade_concat(clips, out, transition="fade", dur=0.5):
    inputs, filters = [], []
    durations = [_probe_duration(c) for c in clips]
    for c in clips:
        inputs += ["-i", c]
    n = len(clips)
    acc = durations[0]
    for i in range(n - 1):
        off = max(0, acc - dur)
        filters.append(f"[{i if i == 0 else 'v'+str(i)}][{i+1}]"
                       f"xfade=transition={transition}:duration={dur}:offset={off:.2f}[v{i+1}]")
        acc += durations[i + 1] - dur
    _run_ffmpeg(["ffmpeg", "-y"] + inputs + ["-filter_complex", ";".join(filters),
                 "-map", f"[v{n-1}]", "-c:v", "libx264", "-preset", "fast", out])

def compose_video(clips, durations, audio_path, srt_path, output_path, transition="fade"):
    tmp = Path(clips[0]).parent
    normalized = []
    for i, (c, d) in enumerate(zip(clips, durations)):
        norm = str(tmp / f"norm_{i}.mp4")
        _normalize_clip(c, norm, d)
        normalized.append(norm)
    concat = str(tmp / "concat.mp4")
    if len(normalized) > 1:
        _xfade_concat(normalized, concat, transition)
    else:
        shutil.copy(normalized[0], concat)
    safe_srt = str(Path(srt_path).resolve()).replace("\\", "/").replace(":", "\\:")
    vf = (f"subtitles='{safe_srt}':force_style="
          f"'FontSize=26,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=2,Alignment=2,MarginV=80'")
    _run_ffmpeg(["ffmpeg", "-y", "-i", concat, "-i", audio_path, "-vf", vf,
                 "-c:v", "libx264", "-c:a", "aac", "-shortest", output_path])
    return output_path

def make_video(product, out_dir, title=None, brand="源头工厂"):
    """给产品生成一条带货短视频，返回视频路径"""
    vid = str(uuid.uuid4())[:8]
    tmp = TEMP_DIR / vid
    tmp.mkdir(exist_ok=True)
    sb = local_storyboard(product)
    audios, durations = [], []
    for i, scene in enumerate(sb["scenes"]):
        a, _ = generate_tts(scene["line"], str(tmp / f"v{i}.mp3"))
        d = max(2.0, min(8.0, _probe_duration(a)))
        audios.append(a); durations.append(d)
    audio = merge_audios(audios, str(tmp / "voice.mp3"))
    clips = []
    for i, scene in enumerate(sb["scenes"]):
        img = str(tmp / f"img_{i}.png")
        if not pollinations_image(scene["image_prompt"] + "，无文字", img, VIDEO_W, VIDEO_H,
                                  seed=random.randint(0, 99999)):
            make_text_card(scene["line"], img, sb["title"], i)
        clips.append(free_motion_video(img, scene["camera"], durations[i], str(tmp / f"c{i}.mp4")))
    srt = write_srt([s["line"] for s in sb["scenes"]], durations, str(tmp / "sub.srt"))
    safe = re.sub(r'[\\/:*?"<>|]', "", title or sb["title"])[:30]
    out = str(Path(out_dir) / f"{safe}_短视频.mp4")
    compose_video(clips, durations, audio, srt, out)
    return out

# ============================================================
# 六、图片设计封装
# ============================================================
SCRIPT_LINES = {
    "宠物挂饰": ["精工铃铛，萌宠出街神器", "柔软织带，不勒脖更舒服", "多色可选，回头率拉满", "洗澡可戴，生锈包退"],
    "汽车挂饰": ["平安流苏，一路相伴", "手工编织，寓意满满", "不挡视线，安全牢固", "新车必备，送礼有面"],
    "手机挂饰": ["挂绳防摔，手机不再掉", "磁吸设计，一贴即用", "颜值在线，百搭出片", "合金配件，耐用不掉色"],
    "包包挂饰": ["小挂件大亮点，包包瞬间高级", "头层牛皮，越用越有味道", "手工缝制，细节拉满", "闺蜜同款，闭眼入"],
    "衣服挂饰": ["香囊挂饰，衣角生香", "刺绣工艺，国风满满", "驱蚊防虫，天然草本", "亲子同款，寓意安康"],
    "默认": ["源头工厂，品质看得见", "用料扎实，细节到位", "今日下单，明天发货", "不满意七天无理由退"],
}
SCENE_MAP = {
    "宠物挂饰": [("遛弯时刻", "小区里最靓的崽"), ("拍照打卡", "出片率翻倍")],
    "汽车挂饰": [("新车到手", "第一件事就是挂它"), ("自驾出行", "一路平安相伴")],
    "默认": [("居家日常", "融入生活每个角落"), ("送礼时刻", "心意与质感兼备")],
}

def design_product_package(image_path, product, brand, category, out_dir):
    """白底图 → 主图5张 + 详情页，返回 (主图list, 详情页path, zip_path)"""
    vid = str(uuid.uuid4())[:8]
    tmp = TEMP_DIR / ("design_" + vid)
    tmp.mkdir(exist_ok=True)
    points = SCRIPT_LINES.get(category, SCRIPT_LINES["默认"])[:5]
    scenes = [{"title": t, "desc": d} for t, d in SCENE_MAP.get(category, SCENE_MAP["默认"])]
    copy = _main_copy(product, brand, points, f"{product}，好品质看得见", scenes,
                      [{"k": "品牌", "v": brand}, {"k": "品名", "v": product},
                       {"k": "材质", "v": "优质环保材料"}, {"k": "产地", "v": "中国"}])
    raw = Image.open(image_path)
    cutout = remove_white_bg(raw)
    mains = make_main_images(cutout, copy, product, brand, hash(product) % 5, tmp)
    detail = make_detail_page(cutout, copy, product, brand, hash(product) % 5, tmp)
    pack = Path(out_dir) / f"{product}_设计包"
    if pack.exists():
        shutil.rmtree(pack)
    (pack / "橱窗主图").mkdir(parents=True)
    (pack / "详情页").mkdir()
    for i, p in enumerate(mains, 1):
        shutil.copy(p, pack / "橱窗主图" / f"主图{i}.jpg")
    shutil.copy(detail, pack / "详情页" / "详情页长图.jpg")
    zip_path = shutil.make_archive(str(pack), "zip", pack)
    return mains, detail, zip_path

# ============================================================
# 七、一键流水线：选品 → 图 → 视频 → 总zip
# ============================================================
def run_pipeline(candidate_sel, brand, product_image=None):
    """选中候选品 → 自动出 主图+详情页+短视频 → 打包zip"""
    if not candidate_sel:
        return None, "❌ 请先在选品工作台添加候选品"
    cid = int(str(candidate_sel).split("-")[0])
    c = q("SELECT * FROM candidates WHERE id=?", (cid,))
    if not c:
        return None, "❌ 候选品不存在"
    c = c[0]
    product, category = c["name"], c["category"] or "默认"
    out_dir = OUTPUT_DIR / f"作战_{product}_{today()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        # 图：有产品图用产品图，没有就用免费生图做详情页底图
        if product_image:
            img_path = str(TEMP_DIR / f"pipeline_{uuid.uuid4().hex[:6]}.png")
            shutil.copy(product_image.name if hasattr(product_image, "name") else product_image, img_path)
        else:
            img_path = str(TEMP_DIR / f"pipeline_{uuid.uuid4().hex[:6]}.png")
            if not pollinations_image(f"{product}，白底产品图，电商主图风格，单个产品，高清", img_path, 800, 800):
                make_text_card(product, img_path, "", 0)
        mains, detail, zip_imgs = design_product_package(img_path, product, brand, category, str(out_dir))
        # 视频
        video = make_video(product, str(out_dir), brand=brand)
        # 总包
        pack = OUTPUT_DIR / f"作战包_{product}_{today()}"
        if pack.exists():
            shutil.rmtree(pack)
        pack.mkdir()
        for f in Path(out_dir).glob("*"):
            if f.is_file():
                shutil.copy(f, pack)
        total_zip = shutil.make_archive(str(pack), "zip", pack)
        return (mains, detail, video, total_zip,
                f"✅ 完成：主图5张 + 详情页 + 短视频，打包于 {total_zip}")
    except Exception as e:
        return None, f"❌ 流水线失败：{str(e)[:300]}（缺ffmpeg请先安装）"

# ============================================================
# 八、Gradio 界面
# ============================================================
# ============================================================
# 高级感主题（简约大气：石板灰 + 琥珀金）
# ============================================================
PREMIUM_CSS = """
.gradio-container { max-width: 1280px !important; margin: 0 auto !important; }
footer { display: none !important; }
.primary, .primary.svelte-1ipelgc { background: linear-gradient(135deg, #0f172a, #334155) !important;
    border: none !important; box-shadow: 0 4px 14px rgba(15,23,42,.25); transition: all .15s; }
.primary:hover { transform: translateY(-1px); box-shadow: 0 8px 20px rgba(15,23,42,.35); }
h1, h2, h3 { font-weight: 600 !important; letter-spacing: 1.5px; }
.tab-nav { gap: 4px; }
.block { border-radius: 12px; }
"""

def premium_blocks(title):
    import gradio as gr
    theme = gr.themes.Soft(primary_hue="slate", secondary_hue="amber", neutral_hue="slate")
    return gr.Blocks(title=title, theme=theme, css=PREMIUM_CSS)


def launch_ui():
    import gradio as gr

    with premium_blocks("繁星电商作战台") as demo:
        gr.Markdown("# ⚡ 繁星电商作战台\n选品参谋 + 图片工厂 + 短视频工厂 三合一，外加一键流水线\n全免费 · 数据本机")

        # ---- 总览 ----
        with gr.Tab("📊 作战总览"):
            dash = gr.JSON(label="选品库概况")
            gr.Button("刷新").click(stats_summary, [], dash)
            demo.load(stats_summary, [], dash)
            gr.Markdown("**作战流程**：选品工作台打分定品 → 一键流水线出图出视频 → 一键发布助手发全网")

        # ---- 选品 ----
        with gr.Tab("🎯 选品"):
            with gr.Accordion("📈 趋势雷达（Google Trends 真数据，需可访问谷歌的网络）", open=False):
                tr_kw = gr.Textbox(label="关键词（逗号分隔，最多5个）")
                with gr.Row():
                    tr_region = gr.Textbox(label="地区", value="CN")
                    tr_days = gr.Dropdown([30, 90, 180], label="天数", value=90)
                    tr_btn = gr.Button("分析", variant="primary")
                tr_txt = gr.Markdown()
                tr_tbl = gr.DataFrame(interactive=False)
            with gr.Accordion("🏆 平台榜单台", open=False):
                rk_pf = gr.Dropdown(["全部"] + sorted({x[0] for x in PLATFORM_RANKS}),
                                    label="平台", value="全部")
                rk_md = gr.Markdown()
                rk_pf.change(ranks_markdown, [rk_pf], rk_md)
                demo.load(ranks_markdown, [rk_pf], rk_md)
            with gr.Row():
                with gr.Column(scale=1):
                    c_name = gr.Textbox(label="产品名称")
                    with gr.Row():
                        c_pf = gr.Dropdown(["抖音", "拼多多", "淘宝/天猫", "亚马逊", "TikTok", "其他"],
                                           label="平台", value="抖音")
                        c_cat = gr.Textbox(label="分类（如：宠物挂饰）")
                    with gr.Row():
                        c_cost = gr.Number(label="成本", value=0)
                        c_price = gr.Number(label="售价", value=0)
                    with gr.Row():
                        c_sd = gr.Slider(1, 5, 3, step=1, label="需求")
                        c_sc = gr.Slider(1, 5, 3, step=1, label="竞争小")
                        c_sp = gr.Slider(1, 5, 3, step=1, label="利润")
                        c_ss = gr.Slider(1, 5, 3, step=1, label="供应链")
                    c_tag = gr.Dropdown(TREND_TAGS, label="趋势", value="新品测试")
                    c_note = gr.Textbox(label="备注")
                    c_add = gr.Button("➕ 加入选品库", variant="primary")
                    c_msg = gr.Textbox()
                with gr.Column(scale=2):
                    c_filter = gr.Dropdown(["全部", "评估中", "重点跟进", "已放弃"], value="全部", label="筛选")
                    c_tbl = gr.DataFrame(label="选品库（综合分自动加权）", interactive=False)
                    with gr.Row():
                        c_id = gr.Number(label="ID", precision=0)
                        c_st = gr.Dropdown(["评估中", "重点跟进", "已放弃"], label="设为")
                        c_st_btn = gr.Button("变更")
                        c_del = gr.Button("删除")
                    c_op = gr.Textbox()
            c_add.click(add_candidate, [c_name, c_pf, c_cat, c_cost, c_price,
                                        c_sd, c_sc, c_sp, c_ss, c_tag, c_note], c_msg)\
                 .then(candidates_df, [c_filter], c_tbl)
            c_filter.change(candidates_df, [c_filter], c_tbl)
            c_st_btn.click(set_candidate_status, [c_id, c_st], c_op).then(candidates_df, [c_filter], c_tbl)
            c_del.click(delete_candidate, [c_id], c_op).then(candidates_df, [c_filter], c_tbl)
            demo.load(candidates_df, [c_filter], c_tbl)

        # ---- 图片 ----
        with gr.Tab("🖼️ 图片工厂"):
            gr.Markdown("上传白底产品图 → 自动出 橱窗主图5张 + 详情页长图")
            with gr.Row():
                d_img = gr.Image(label="白底产品图", type="filepath")
                with gr.Column():
                    d_name = gr.Textbox(label="产品名（留空用文件名）")
                    d_brand = gr.Textbox(label="品牌名", value="源头工厂")
                    d_cat = gr.Dropdown(["宠物挂饰", "汽车挂饰", "手机挂饰", "包包挂饰", "衣服挂饰", "默认"],
                                        label="分类", value="默认")
                    d_btn = gr.Button("🎨 生成设计包", variant="primary")
                    d_msg = gr.Textbox()
                    d_zip = gr.File(label="下载设计包 zip")
            d_gal = gr.Gallery(label="主图预览", columns=3)
            d_detail = gr.Image(label="详情页长图")
            def _design(img, name, brand, cat):
                if not img:
                    return [], None, None, "请先上传图片"
                product = name.strip() or Path(img).stem
                mains, detail, zipp = design_product_package(img, product, brand, cat, str(OUTPUT_DIR))
                return mains, detail, zipp, f"✅ 完成：{product} 主图5张+详情页"
            d_btn.click(_design, [d_img, d_name, d_brand, d_cat], [d_gal, d_detail, d_zip, d_msg])

        # ---- 视频 ----
        with gr.Tab("🎬 视频工厂"):
            gr.Markdown("输入产品名 → 自动生成带货短视频（文案/配音/字幕/运镜全自动）")
            with gr.Row():
                v_name = gr.Textbox(label="产品名（如：宠物铃铛脖挂）")
                v_brand = gr.Textbox(label="品牌口播名", value="源头工厂")
                v_btn = gr.Button("🎬 生成视频", variant="primary")
            v_msg = gr.Textbox()
            v_out = gr.Video(label="成片")
            def _video(name, brand):
                if not name.strip():
                    return None, "请填产品名"
                out = make_video(name.strip(), str(OUTPUT_DIR), brand=brand)
                return out, f"✅ 已生成：{out}"
            v_btn.click(_video, [v_name, v_brand], [v_out, v_msg])

        # ---- 一键流水线 ----
        with gr.Tab("⚡ 一键流水线"):
            gr.Markdown("**选一个选品库里的产品 → 自动出：主图5张 + 详情页 + 带货短视频 → 打包zip**")
            with gr.Row():
                p_sel = gr.Dropdown(label="选择候选品", interactive=True)
                p_img = gr.Image(label="产品白底图（可选，不传则自动生图）", type="filepath")
                p_brand = gr.Textbox(label="品牌名", value="源头工厂")
                p_btn = gr.Button("🚀 开始流水线", variant="primary")
            p_msg = gr.Textbox()
            p_gal = gr.Gallery(label="主图", columns=3)
            p_detail = gr.Image(label="详情页")
            p_video = gr.Video(label="短视频")
            p_zip = gr.File(label="下载作战包 zip")
            def _pipe(sel, img, brand):
                res = run_pipeline(sel, brand, img)
                if res[0] is None:
                    return [], None, None, None, res[1]
                mains, detail, video, zipp, msg = res
                return mains, detail, video, zipp, msg
            p_btn.click(_pipe, [p_sel, p_img, p_brand],
                        [p_gal, p_detail, p_video, p_zip, p_msg])
            demo.load(lambda: gr.update(choices=candidate_names()), [], p_sel)

        # ---- 笔记 / 系统 ----
        with gr.Tab("📝 选品笔记"):
            n_content = gr.Textbox(label="灵感/方法论", lines=3)
            n_tags = gr.Textbox(label="标签")
            n_add = gr.Button("记录", variant="primary")
            n_msg = gr.Textbox()
            n_tbl = gr.DataFrame(interactive=False)
            n_add.click(add_note, [n_content, n_tags], n_msg).then(notes_df, [], n_tbl)
            demo.load(notes_df, [], n_tbl)
        with gr.Tab("⚙️ 系统"):
            seed_btn = gr.Button("🎁 导入示例数据")
            s_msg = gr.Textbox()
            seed_btn.click(seed_demo, [], s_msg)\
                     .then(lambda: gr.update(choices=candidate_names()), [], p_sel)\
                     .then(candidates_df, [gr.Dropdown(value="全部", visible=False)], c_tbl)
            gr.Markdown("数据在 data/command.db；视频合成需 ffmpeg（一键启动自动提示）")

    demo.launch(server_name="0.0.0.0",
                server_port=int(os.environ.get("PORT", 7866)),
                share=os.environ.get("GRADIO_SHARE", "") == "1")

if __name__ == "__main__":
    init_db()
    if "--cli" in sys.argv:
        print(json.dumps(stats_summary(), ensure_ascii=False, indent=2))
    else:
        launch_ui()
