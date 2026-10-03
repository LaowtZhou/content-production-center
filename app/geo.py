"""地域与主体判定（三级树第二、三级）—— 规则唯一事实来源。

周老师 2026-10-03 定：
- 二级 = 中国 / 美国 / 其他（三档，不新造国家清单）。
- 三级 = 政府机构 或 具体公司（主体名）。
- 判定顺序：**标题 → 全文**。
  1. 标题里有政府机构 → 政府（谁在做事谁就是主体）。
  2. 标题里有公司 → 取**最靠前**的那个（中文标题主语通常在最前；
     如"苹果起诉 OpenAI"主体是苹果，不是被提到的 OpenAI）。
  3. 标题认不出 → 读**全文**（raw_content），按实体**出现次数**取最高频，
     再次要实体也用得少时判"未识别"（宁可漏，不可错——错了污染格子）。
- 主体都没识别出来 → 用地域词兜底；仍无 → 其他/未识别。

周老师 2026-10-03 追加：
- 不再读 summary（AI 摘要常把"被提到"写成"当事主体"），改读 raw_content 全文。

词典由 AI 代维护：出现新公司/新机构，加进 COMPANY_REGION / GOV_REGION 即可，
不需要改任何逻辑或页面。
"""

from __future__ import annotations

import re

CHINA = "中国"
US = "美国"
OTHER = "其他"
UNKNOWN_ENTITY = "未识别"

# 全文判定时，主体至少出现这么多次才认（1 次多为"被顺带提到"）。
_MIN_FULLTEXT_HITS = 2

# 判定规则版本。改判定逻辑或增删主体词典后 +1。
# 启动迁移比对这个号决定是否全库重判，省得每加一批词就新造一个迁移标记。
# 1 = 标题→摘要；2 = 标题→全文（按出现次数）；3 = 主体词典补齐常见公司/机构
#     + 别称归并（"谷歌"与"Google"合到同一格）。
JUDGE_VERSION = 3

# ===== 政府机构 → 归属地 =====
GOV_REGION: dict[str, str] = {
    # 美国
    "白宫": US, "国会": US, "参议院": US, "众议院": US, "FTC": US, "SEC": US,
    "司法部": US, "DOJ": US, "商务部": US, "五角大楼": US, "国防部": US,
    "州检察长": US, "FCC": US, "美联储": US, "CISA": US, "NIST": US, "FAA": US,
    "联邦贸易委员会": US, "美国国会": US, "司法部": US, "加州总检察长": US,
    "财政部": US, "美国财政部": US, "能源部": US, "国土安全部": US,
    "DARPA": US, "NSA": US, "FBI": US, "贸易代表办公室": US, "USTR": US,
    # 中国
    "网信办": CHINA, "工信部": CHINA, "发改委": CHINA, "国务院": CHINA,
    "科技部": CHINA, "信通院": CHINA, "国家数据局": CHINA,
    "中央网信办": CHINA, "国家发改委": CHINA, "公安部": CHINA,
    "中国人民银行": CHINA, "证监会": CHINA, "市场监管总局": CHINA,
    # 其他
    "欧盟委员会": OTHER, "欧洲议会": OTHER, "欧盟": OTHER, "英国议会": OTHER,
    "联合国": OTHER, "INTERPOL": OTHER, "OECD": OTHER, "G7": OTHER,
    "英国政府": OTHER, "日本政府": OTHER, "韩国政府": OTHER,
    "英国信息专员办公室": OTHER, "欧盟数据保护委员会": OTHER,
}

# ===== 公司 / 机构 → 归属地 =====
COMPANY_REGION: dict[str, str] = {
    # 美国
    "OpenAI": US, "Anthropic": US, "Google DeepMind": US, "DeepMind": US,
    "Google": US, "Alphabet": US, "Microsoft": US, "Meta": US, "Nvidia": US,
    "英伟达": US, "AMD": US, "Apple": US, "苹果": US, "Amazon": US, "亚马逊": US,
    "xAI": US, "Tesla": US, "特斯拉": US, "Cursor": US, "GitHub": US,
    "Perplexity": US, "Scale AI": US, "Hugging Face": US, "Oracle": US,
    "甲骨文": US, "Intel": US, "英特尔": US, "Qualcomm": US, "高通": US,
    "Palantir": US, "Figure": US, "Boston Dynamics": US, "波士顿动力": US,
    "OpenRouter": US, "Adept": US,
    "Inflection": US, "Midjourney": US, "Runway": US, "Suno": US,
    "ElevenLabs": US, "Character.AI": US, "Cloudflare": US, "Shopify": US,
    "Reddit": US, "Adobe": US, "Cerebras": US, "Uber": US, "YouTube": US,
    "AWS": US, "Harvey": US, "Pew": US, "Atlas": US, "Vivodyne": US,
    "Xenomi": US, "Tenet": US, "Together AI": US, "Groq": US, "SambaNova": US,
    "Databricks": US, "Snowflake": US, "Salesforce": US, "IBM": US,
    "Anysphere": US, "Windsurf": US, "Broadcom": US, "博通": US, "Micron": US,
    "美光": US, "SpaceX": US, "Disney": US, "迪士尼": US, "Patreon": US,
    # 美国（2026-10-03 补）：这些是新闻里最常出现的主体，此前缺席，
    # 导致标题里明写着公司名也判成"未识别"。
    "微软": US, "谷歌": US, "Dell": US, "戴尔": US, "HP": US, "惠普": US,
    "Cisco": US, "思科": US, "Twitter": US, "推特": US, "X Corp": US,
    "LinkedIn": US, "领英": US, "Netflix": US, "奈飞": US, "Airbnb": US,
    "爱彼迎": US, "Coinbase": US, "eBay": US, "PayPal": US, "Mastercard": US,
    "Waymo": US, "Rivian": US, "Anduril": US, "ServiceNow": US,
    "Twilio": US, "Robinhood": US, "Pinterest": US, "Figma": US,
    # 中国
    "字节跳动": CHINA, "字节": CHINA, "阿里巴巴": CHINA, "阿里云": CHINA,
    "阿里": CHINA, "通义": CHINA, "千问": CHINA, "Qwen": CHINA, "腾讯": CHINA,
    "腾讯云": CHINA, "华为": CHINA, "昇腾": CHINA, "百度": CHINA, "文心": CHINA,
    "商汤": CHINA, "科大讯飞": CHINA, "讯飞": CHINA, "智谱": CHINA, "ZCode": CHINA,
    "GLM": CHINA, "月之暗面": CHINA, "Kimi": CHINA, "MiniMax": CHINA,
    "DeepSeek": CHINA, "深度求索": CHINA, "快手": CHINA, "美团": CHINA,
    "京东": CHINA, "小米": CHINA, "网易": CHINA, "百川智能": CHINA,
    "零一万物": CHINA, "阶跃星辰": CHINA, "宇树": CHINA, "Unitree": CHINA,
    "蚂蚁": CHINA, "拼多多": CHINA, "滴滴": CHINA, "蔚来": CHINA, "小鹏": CHINA,
    "理想": CHINA, "比亚迪": CHINA, "绿盟": CHINA, "Manus": CHINA,
    "Monica": CHINA, "智元": CHINA, "台积电": CHINA, "TSMC": CHINA,
    "地平线": CHINA, "浪潮": CHINA, "平安": CHINA, "太初元碁": CHINA,
    "Z.ai": CHINA, "Libcom": CHINA,
    # 中国（2026-10-03 同批补齐）
    "荣耀": CHINA, "中兴": CHINA, "ZTE": CHINA, "OPPO": CHINA, "vivo": CHINA,
    "联想": CHINA, "Lenovo": CHINA, "寒武纪": CHINA, "摩尔线程": CHINA,
    "天数智芯": CHINA, "壁仞": CHINA, "哔哩哔哩": CHINA, "B站": CHINA,
    "知乎": CHINA, "微博": CHINA, "小红书": CHINA, "大疆": CHINA, "DJI": CHINA,
    "宁德时代": CHINA, "海尔": CHINA, "优必选": CHINA, "云深处": CHINA,
    "面壁智能": CHINA, "硅基流动": CHINA, "无问芯穹": CHINA, "生数科技": CHINA,
    "中国移动": CHINA, "中国电信": CHINA, "中国联通": CHINA, "携程": CHINA,
    "奇安信": CHINA, "深信服": CHINA, "联发科": CHINA, "MediaTek": CHINA,
    "飞书": CHINA, "钉钉": CHINA,
    # 其他
    "Mistral": OTHER, "Sakana AI": OTHER, "NTT": OTHER, "软银": OTHER,
    "索尼": OTHER, "Sony": OTHER, "三星": OTHER, "LG": OTHER, "Naver": OTHER,
    "丰田": OTHER, "本田": OTHER, "乐天": OTHER, "ARM": OTHER,
    "SAP": OTHER, "ASML": OTHER, "三星电子": OTHER, "任天堂": OTHER,
    "Brookfield": OTHER, "Lightspeed": OTHER,
    # 其他（2026-10-03 同批补齐）：Stability AI 在英国、Cohere 在加拿大，
    # 此前被挂在"美国"下，属于真实错判，一并纠正。
    "Stability AI": OTHER, "Cohere": OTHER, "DeepL": OTHER,
    "Aleph Alpha": OTHER, "Synthesia": OTHER, "SK海力士": OTHER,
    "Kakao": OTHER, "富士通": OTHER, "松下": OTHER, "日立": OTHER,
    "佳能": OTHER, "尼康": OTHER, "东京电子": OTHER, "信越": OTHER,
    "诺基亚": OTHER, "爱立信": OTHER, "西门子": OTHER, "飞利浦": OTHER,
    "Canva": OTHER, "Spotify": OTHER, "现代汽车": OTHER, "起亚": OTHER,
    "英飞凌": OTHER, "恩智浦": OTHER, "博世": OTHER, "Samsung": OTHER,
}

# ===== 同一主体的别称 → 规范名 =====
# 词典里中英文两种写法都要留着（检测要认得"谷歌"也认得"Google"），但落库只能
# 落一个名字。否则树里会出现 "Google" 和 "谷歌" 两个格子，同一条新闻线被拆两半——
# 用户看到的"怎么是散的"有一部分就是这么来的。
# 规范名：美国公司用英文官方名，中国公司用中文名，日韩欧用中文常用名。
ENTITY_ALIAS: dict[str, str] = {
    "微软": "Microsoft",
    "谷歌": "Google",
    "英伟达": "Nvidia",
    "苹果": "Apple",
    "亚马逊": "Amazon",
    "英特尔": "Intel",
    "高通": "Qualcomm",
    "甲骨文": "Oracle",
    "博通": "Broadcom",
    "美光": "Micron",
    "迪士尼": "Disney",
    "波士顿动力": "Boston Dynamics",
    "DeepMind": "Google DeepMind",
    "字节": "字节跳动",
    "阿里巴巴": "阿里",
    "阿里云": "阿里",
    "深度求索": "DeepSeek",
    "科大讯飞": "科大讯飞",
    "讯飞": "科大讯飞",
    "千问": "通义",
    "Qwen": "通义",
    "Kimi": "月之暗面",
    "GLM": "智谱",
    "Z.ai": "智谱",
    "ZCode": "智谱",
    "文心": "百度",
    "Unitree": "宇树",
    "TSMC": "台积电",
    "三星电子": "三星",
    "Samsung": "三星",
    "Sony": "索尼",
}


def _canon(name: str) -> str:
    """把别称归到规范名；未识别原样返回。"""
    return ENTITY_ALIAS.get(name, name)


# 展示顺序用：长词优先（"阿里云"先于"阿里"），避免短词把长词切碎。
_GOV_SORTED = sorted(GOV_REGION, key=len, reverse=True)
_COMP_SORTED = sorted(COMPANY_REGION, key=len, reverse=True)
_ALL_SORTED = sorted(set(GOV_REGION) | set(COMPANY_REGION), key=len, reverse=True)

# 地域兜底词（没有识别出任何主体时，用地域词猜一手）
_REGION_HINT: list[tuple[str, str]] = [
    (r"中国|国内|国产|中方|我国|网信办|工信部|发改委|国务院", CHINA),
    (r"美国|美方|白宫|国会|联邦|特朗普|硅谷", US),
    (r"欧盟|欧洲|英国|法国|德国|日本|韩国|印度|澳大利亚|加拿大|新加坡|中东|联合国",
     OTHER),
]


def _region_of(name: str) -> str:
    return GOV_REGION.get(name) or COMPANY_REGION.get(name) or OTHER


def _leftmost(text: str, words: list[str]) -> str | None:
    """标题里最靠前的实体；同一起点取更长者（"阿里云"优先于"阿里"）。"""
    low = text.lower()
    best: tuple[tuple[int, int], str] | None = None
    for w in words:
        i = low.find(w.lower())
        if i < 0:
            continue
        key = (i, -len(w))
        if best is None or key < best[0]:
            best = (key, w)
    return best[1] if best else None


def _counts(text: str) -> dict[str, int]:
    """统计各实体出现次数。长词先扫、命中后抹掉，避免子串重复计数。"""
    low = text.lower()
    out: dict[str, int] = {}
    for w in _ALL_SORTED:
        c = low.count(w.lower())
        if c:
            out[w] = c
            low = low.replace(w.lower(), "\u0000")
    return out


def judge(title: str, full_text: str = "") -> tuple[str, str, str]:
    """返回 (region, entity, kind)。kind ∈ {gov, company, none}。

    标题优先（政府压过公司、公司取最靠前），标题认不出才读全文按次数定。
    """
    t = (title or "").strip()

    # 1) 标题：政府机构最优先
    gov = _leftmost(t, _GOV_SORTED)
    if gov:
        return GOV_REGION[gov], _canon(gov), "gov"
    # 2) 标题：公司取最靠前的那个
    comp = _leftmost(t, _COMP_SORTED)
    if comp:
        return COMPANY_REGION[comp], _canon(comp), "company"

    # 3) 标题认不出 → 读全文，按出现次数定主体
    counts = _counts(full_text or "")
    if counts:
        top = max(counts.values())
        tied = [w for w, n in counts.items() if n == top]
        # 最高频要明显不止提了一次才认；只出现 1 次的多半是"被顺带提到"，
        # 宁可判未识别，也不能把它塞进某个公司的格子污染归类。
        if top >= _MIN_FULLTEXT_HITS:
            # 平票：政府优先，其次名字更长（更具体）
            tied.sort(key=lambda w: (w in COMPANY_REGION, -len(w)))
            w = tied[0]
            return _region_of(w), _canon(w), "gov" if w in GOV_REGION else "company"

    # 4) 主体没识别出来，用地域词兜底
    for text in (t, full_text or ""):
        for pat, reg in _REGION_HINT:
            if re.search(pat, text):
                return reg, UNKNOWN_ENTITY, "none"
    return OTHER, UNKNOWN_ENTITY, "none"
