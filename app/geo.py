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

周老师 2026-10-03（v4）点名整改：
- 此前"特朗普"只躺在 _REGION_HINT 里当**地域词**，导致"特朗普宣布组建 AI 军力"
  判成「美国 / 未识别」——总统不配当主体，这就是用户看到的"什么狗屁未识别"。
  现在**政府人物与企业领袖进 PERSON 表**（特朗普 → 美国政府、黄仁勋 → Nvidia）。
- 补高校/研究机构（清华、北大、中科院、智源、MIT…）与中文常用公司名
  （通用汽车、微盟、睿尔曼、稳准智能、Shield AI、Waabi…）。
- 短英文缩写加**词边界**：原先 low.find("sec") 会命中 "security" 里的 sec，
  把任何含 security 的标题判成美国证监会。MIT 会命中 "limit"。一并修掉。

词典由 AI 代维护：出现新公司/新机构/新人物，加进对应词典即可，
不需要改任何逻辑或页面。
"""

from __future__ import annotations

import re

CHINA = "中国"
US = "美国"
OTHER = "其他"
UNKNOWN_ENTITY = "未识别"

# 政府人物统一落这个主体名（总统/部长的动作就是美国政府的行为）。
GOV_US_NAME = "美国政府"
GOV_CN_NAME = "中国政府"

# 全文判定时，主体至少出现这么多次才认（1 次多为"被顺带提到"）。
_MIN_FULLTEXT_HITS = 2

# 判定规则版本。改判定逻辑或增删主体词典后 +1。
# 启动迁移比对这个号决定是否全库重判，省得每加一批词就新造一个迁移标记。
# 1 = 标题→摘要；2 = 标题→全文（按出现次数）；3 = 主体词典补齐常见公司/机构
#     + 别称归并（"谷歌"与"Google"合到同一格）；
# 4 = 人物入判（特朗普→美国政府）+ 高校/中文公司补齐 + 短英文缩写加词边界。
JUDGE_VERSION = 4

# ===== 政府机构 → 归属地 =====
GOV_REGION: dict[str, str] = {
    # 美国
    "白宫": US, "国会": US, "参议院": US, "众议院": US, "FTC": US, "SEC": US,
    "司法部": US, "DOJ": US, "商务部": US, "五角大楼": US, "国防部": US,
    "州检察长": US, "FCC": US, "美联储": US, "CISA": US, "NIST": US, "FAA": US,
    "联邦贸易委员会": US, "美国国会": US, "加州总检察长": US,
    "财政部": US, "美国财政部": US, "能源部": US, "国土安全部": US,
    "DARPA": US, "NSA": US, "FBI": US, "贸易代表办公室": US, "USTR": US,
    # 美国（2026-10-03 v4 补）：政府人物统一落这里，别再判"未识别"。
    GOV_US_NAME: US, "美国卫生与公众服务部": US,
    # 中国
    "网信办": CHINA, "工信部": CHINA, "发改委": CHINA, "国务院": CHINA,
    "科技部": CHINA, "信通院": CHINA, "国家数据局": CHINA,
    "中央网信办": CHINA, "国家发改委": CHINA, "公安部": CHINA,
    "中国人民银行": CHINA, "证监会": CHINA, "市场监管总局": CHINA,
    "国家安全部": CHINA, "教育部": CHINA, "中央网信办": CHINA,
    # 其他
    "欧盟委员会": OTHER, "欧洲议会": OTHER, "欧盟": OTHER, "英国议会": OTHER,
    "联合国": OTHER, "INTERPOL": OTHER, "OECD": OTHER, "G7": OTHER,
    "英国政府": OTHER, "日本政府": OTHER, "韩国政府": OTHER,
    "英国信息专员办公室": OTHER, "欧盟数据保护委员会": OTHER,
}

# ===== 公司 / 机构（含高校、研究院）→ 归属地 =====
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
    "微软": US, "谷歌": US, "Dell": US, "戴尔": US, "HP": US, "惠普": US,
    "Cisco": US, "思科": US, "Twitter": US, "推特": US, "X Corp": US,
    "LinkedIn": US, "领英": US, "Netflix": US, "奈飞": US, "Airbnb": US,
    "爱彼迎": US, "Coinbase": US, "eBay": US, "PayPal": US, "Mastercard": US,
    "Waymo": US, "Rivian": US, "Anduril": US, "ServiceNow": US,
    "Twilio": US, "Robinhood": US, "Pinterest": US, "Figma": US,
    # 美国（2026-10-03 v4 补）：实体常出现在标题里却一直判"未识别"。
    "Shield AI": US, "Crusoe": US, "Agility Robotics": US, "Agility": US,
    "通用汽车": US, "福特": US, "通用电气": US, "波音": US, "Boeing": US,
    "洛克希德": US, "雷神": US, "Zoox": US, "Applied Intuition": US,
    "Sierra": US, "Anthropic": US, "Mistral AI": US,
    "Marvell": US, "CrowdStrike": US, "Palo Alto Networks": US,
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
    "荣耀": CHINA, "中兴": CHINA, "ZTE": CHINA, "OPPO": CHINA, "vivo": CHINA,
    "联想": CHINA, "Lenovo": CHINA, "寒武纪": CHINA, "摩尔线程": CHINA,
    "天数智芯": CHINA, "壁仞": CHINA, "哔哩哔哩": CHINA, "B站": CHINA,
    "知乎": CHINA, "微博": CHINA, "小红书": CHINA, "大疆": CHINA, "DJI": CHINA,
    "宁德时代": CHINA, "海尔": CHINA, "优必选": CHINA, "云深处": CHINA,
    "面壁智能": CHINA, "硅基流动": CHINA, "无问芯穹": CHINA, "生数科技": CHINA,
    "中国移动": CHINA, "中国电信": CHINA, "中国联通": CHINA, "携程": CHINA,
    "奇安信": CHINA, "深信服": CHINA, "联发科": CHINA, "MediaTek": CHINA,
    "飞书": CHINA, "钉钉": CHINA,
    # 中国（2026-10-03 v4 补）：高校 / 研究院 / 常上新闻的中文公司。
    "清华": CHINA, "清华大学": CHINA, "北大": CHINA, "北京大学": CHINA,
    "中科院": CHINA, "中国科学院": CHINA, "智源": CHINA, "北京智源": CHINA,
    "BAAI": CHINA, "上海人工智能实验室": CHINA, "上海AI Lab": CHINA,
    "西湖大学": CHINA, "浙大": CHINA, "上海交大": CHINA, "复旦": CHINA,
    "微盟": CHINA, "睿尔曼": CHINA, "稳准智能": CHINA, "加速进化": CHINA,
    "银河通用": CHINA, "星尘智能": CHINA, "乐聚": CHINA, "傅利叶": CHINA,
    "追觅": CHINA, "石头科技": CHINA, "方程豹": CHINA, "极氪": CHINA,
    "AGIBOT": CHINA, "Moonshot": CHINA, "智谱AI": CHINA,
    # 其他
    "Mistral": OTHER, "Sakana AI": OTHER, "NTT": OTHER, "软银": OTHER,
    "索尼": OTHER, "Sony": OTHER, "三星": OTHER, "LG": OTHER, "Naver": OTHER,
    "丰田": OTHER, "本田": OTHER, "乐天": OTHER, "ARM": OTHER,
    "SAP": OTHER, "ASML": OTHER, "三星电子": OTHER, "任天堂": OTHER,
    "Brookfield": OTHER, "Lightspeed": OTHER,
    "Stability AI": OTHER, "Cohere": OTHER, "DeepL": OTHER,
    "Aleph Alpha": OTHER, "Synthesia": OTHER, "SK海力士": OTHER,
    "Kakao": OTHER, "富士通": OTHER, "松下": OTHER, "日立": OTHER,
    "佳能": OTHER, "尼康": OTHER, "东京电子": OTHER, "信越": OTHER,
    "诺基亚": OTHER, "爱立信": OTHER, "西门子": OTHER, "飞利浦": OTHER,
    "Canva": OTHER, "Spotify": OTHER, "现代汽车": OTHER, "起亚": OTHER,
    "英飞凌": OTHER, "恩智浦": OTHER, "博世": OTHER, "Samsung": OTHER,
    # 其他（2026-10-03 v4 补）
    "Waabi": OTHER, "1X": OTHER, "Holiday Robotics": OTHER,
    "MIT": US, "斯坦福": US, "斯坦福大学": US, "伯克利": US,
    "卡内基梅隆": US, "CMU": US, "牛津": OTHER, "剑桥": OTHER,
}

# ===== 人物 → (归属地, 落库主体名) =====
# 为什么单独一张表：人名不带"公司/机构"字样，词典匹配不到；但新闻里
# 人物往往是唯一的行为主体（"特朗普宣布…"）。
# 规则：**只在标题里没有政府机构、也没有公司时**才看人物（judge 第 3 步），
# 这样"特斯拉财报，马斯克称…"仍归 Tesla，不会被马斯克抢走。
PERSON: dict[str, tuple[str, str]] = {
    # 政治人物 → 政府主体
    "特朗普": (US, GOV_US_NAME),
    "拜登": (US, GOV_US_NAME),
    "肯尼迪": (US, GOV_US_NAME),
    "RFK Jr.": (US, GOV_US_NAME),
    "RFK": (US, GOV_US_NAME),
    "万斯": (US, GOV_US_NAME),
    "哈里斯": (US, GOV_US_NAME),
    "鲍威尔": (US, "美联储"),
    "马克龙": (OTHER, GOV_US_NAME),
    "冯德莱恩": (OTHER, "欧盟委员会"),
    "Newsom": (US, GOV_US_NAME),
    "纽森": (US, GOV_US_NAME),
    # 企业领袖 → 其公司
    "黄仁勋": (US, "Nvidia"),
    "扎克伯格": (US, "Meta"),
    "奥特曼": (US, "OpenAI"),
    "Altman": (US, "OpenAI"),
    "Amodei": (US, "Anthropic"),
    "纳德拉": (US, "Microsoft"),
    "库克": (US, "Apple"),
    "苏姿丰": (US, "AMD"),
    "李彦宏": (CHINA, "百度"),
    "雷军": (CHINA, "小米"),
    "梁文锋": (CHINA, "DeepSeek"),
    # 刻意不收：马斯克（Tesla/xAI/SpaceX/政府效率部 多重身份，
    # 硬映射到任何一家都会抢走别家新闻，宁可留"未识别"）。
}

# ===== 英文产品 / 项目名 → 主体（需全文佐证）=====
# 有些标题只有产品名（"Galaxy 通用消费级机器人开卖"），光看标题判不出公司；
# 但正文里写明了公司中文全名（"Galaxy（银河通用机器人）"）就能认。
# 要求"全文也出现该公司名"是为了防误伤：三星 Galaxy 的新闻正文里
# 不会出现"银河通用"，所以不会被误判。
PRODUCT_ENTITY: dict[str, str] = {
    "Galaxy": "银河通用",
    "Optimus": "特斯拉",
    # 模型/产品名 → 其公司：标题只有产品名时，靠"全文也出现公司名"佐证。
    "ChatGPT": "OpenAI",
    "Codex": "OpenAI",
    "Claude": "Anthropic",
    "Sonnet": "Anthropic",
    "Gemini": "Google",
    "Copilot": "Microsoft",
}

# ===== 同一主体的别称 → 规范名 =====
# 词典里中英文两种写法都要留着（检测要认得"谷歌"也认得"Google"），但落库只能
# 落一个名字。否则树里会出现 "Google" 和 "谷歌" 两个格子，同一条新闻线被拆两半。
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
    # v4 补
    "清华大学": "清华",
    "北京大学": "北大",
    "中国科学院": "中科院",
    "北京智源": "智源",
    "Agility": "Agility Robotics",
    "ZTE": "中兴",
    "Lenovo": "联想",
    "MediaTek": "联发科",
    "DJI": "大疆",
    "B站": "哔哩哔哩",
}


def _canon(name: str) -> str:
    """把别称归到规范名；未识别原样返回。"""
    return ENTITY_ALIAS.get(name, name)


# 展示顺序用：长词优先（"阿里云"先于"阿里"），避免短词把长词切碎。
_GOV_SORTED = sorted(GOV_REGION, key=len, reverse=True)
_COMP_SORTED = sorted(COMPANY_REGION, key=len, reverse=True)
_PERSON_SORTED = sorted(PERSON, key=len, reverse=True)
# 政府人物（区别于企业领袖）：供分类器判"这是政府动作"用。
_GOV_PERSON_SORTED = sorted(
    (p for p, (_r, e) in PERSON.items() if e.endswith("政府") or e in ("美联储", "欧盟委员会")),
    key=len, reverse=True,
)
_ALL_SORTED = sorted(set(GOV_REGION) | set(COMPANY_REGION), key=len, reverse=True)

# 地域兜底词（没有识别出任何主体时，用地域词猜一手）
_REGION_HINT: list[tuple[str, str]] = [
    (r"中国|国内|国产|中方|我国|网信办|工信部|发改委|国务院|北京", CHINA),
    (r"美国|美方|白宫|国会|联邦|特朗普|硅谷", US),
    (r"欧盟|欧洲|英国|法国|德国|日本|韩国|印度|澳大利亚|加拿大|新加坡|中东|联合国",
     OTHER),
]

# 需要词边界的词：纯英文数字、长度 ≤6 —— 否则 "SEC" 命中 "security"、
# "MIT" 命中 "limit"、"HP" 命中 "php"。
_BOUND_RE = re.compile(r"[a-z0-9][a-z0-9\-\. ]*")


def _hits(hay: str, w: str) -> list[int]:
    """返回 w 在（已小写的）hay 中**所有**起始位置。

    短英文词（≤6 位纯 ASCII）要求前后不是 ASCII 字母数字，避免
    'SEC' 命中 'security'、'MIT' 命中 'limit'、'HP' 命中 'php'。
    非短词走普通子串查找，但必须返回**全部**位置——全文判定靠出现次数
    定主体，只返回首个位置会让所有词的计数都变成 1，判定直接失效。
    """
    lw = w.lower()
    if _BOUND_RE.fullmatch(lw) and len(lw) <= 6:
        out: list[int] = []
        for m in re.finditer(re.escape(lw), hay):
            i, j = m.start(), m.end()
            b = hay[i - 1] if i > 0 else ""
            a = hay[j] if j < len(hay) else ""
            if not (b.isascii() and b.isalnum()) and not (a.isascii() and a.isalnum()):
                out.append(i)
        return out
    out = []
    start = 0
    while True:
        i = hay.find(lw, start)
        if i < 0:
            return out
        out.append(i)
        start = i + 1


def _region_of(name: str) -> str:
    if name in GOV_REGION:
        return GOV_REGION[name]
    if name in COMPANY_REGION:
        return COMPANY_REGION[name]
    if name in PERSON:
        return PERSON[name][0]
    return OTHER


def _leftmost(text: str, words: list[str]) -> str | None:
    """标题里最靠前的实体；同一起点取更长者（"阿里云"优先于"阿里"）。"""
    low = text.lower()
    best: tuple[tuple[int, int], str] | None = None
    for w in words:
        for i in _hits(low, w):
            key = (i, -len(w))
            if best is None or key < best[0]:
                best = (key, w)
            break
    return best[1] if best else None


def _counts(text: str) -> dict[str, int]:
    """统计各实体出现次数。长词先扫、命中后抹掉，避免子串重复计数。"""
    low = text.lower()
    out: dict[str, int] = {}
    for w in _ALL_SORTED:
        hs = _hits(low, w)
        if hs:
            out[w] = len(hs)
            for i in hs:
                low = low[:i] + "\u0000" * len(w) + low[i + len(w):]
    return out


def judge(title: str, full_text: str = "") -> tuple[str, str, str]:
    """返回 (region, entity, kind)。kind ∈ {gov, company, none}。

    标题优先（政府机构 → 公司 → 人物），标题认不出才读全文按次数定。
    """
    t = (title or "").strip()

    # 1) 标题：政府机构最优先
    gov = _leftmost(t, _GOV_SORTED)
    if gov:
        return GOV_REGION[gov], _canon(gov), "gov"
    # 2) 标题：公司 / 机构（高校、研究院）
    comp = _leftmost(t, _COMP_SORTED)
    if comp:
        return COMPANY_REGION[comp], _canon(comp), "company"
    # 3) 标题：人物（无歧义映射；放最后，避免抢走公司新闻）
    per = _leftmost(t, _PERSON_SORTED)
    if per:
        reg, ent = PERSON[per]
        return reg, ent, "gov" if ent.endswith("政府") or ent == "美联储" else "company"

    # 3.5) 标题只有英文产品名 → 全文出现对应公司中文名时才认（三星 Galaxy 不会误判）
    ft = full_text or ""
    if ft:
        for pname, pent in PRODUCT_ENTITY.items():
            if pname.lower() in t.lower() and pent in ft:
                return _region_of(pent), _canon(pent), "company"

    # 4) 标题认不出 → 读全文，按出现次数定主体
    counts = _counts(ft)
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

    # 5) 主体没识别出来，用地域词兜底
    for text in (t, full_text or ""):
        for pat, reg in _REGION_HINT:
            if re.search(pat, text):
                return reg, UNKNOWN_ENTITY, "none"
    return OTHER, UNKNOWN_ENTITY, "none"


def has_gov_subject(text: str) -> bool:
    """文本里是否出现**政府机构或政府人物**。

    供分类器兜底用：标题/摘要没有可归口的动作词时，只要主体是政府
    （"特朗普宣布组建 AI 军力"），就该归「政策与监管」，而不是"其他"。
    短英文缩写走 _hits 的词边界，避免 SEC 命中 security 造成误归政策。
    """
    low = (text or "").lower()
    for w in _GOV_SORTED:
        if _hits(low, w):
            return True
    for w in _GOV_PERSON_SORTED:
        if _hits(low, w):
            return True
    return False
