"""地域与主体判定（三级树第二、三级）—— 规则唯一事实来源。

周老师 2026-10-03 定：
- 二级 = 中国 / 美国 / 其他（三档，不新造国家清单）。
- 三级 = 政府机构 或 具体公司（主体名）。
- 判定优先级：**政府机构先生效**（"FTC 调查 OpenAI" → 主体 FTC、地域美国），
  没有政府主体时才看公司主体；两者都没有 → 主体"未识别"、地域归其他。

词典由 AI 代维护：出现新公司/新机构，加进 COMPANY_REGION / GOV_REGION 即可，
不需要改任何逻辑或页面。
"""

from __future__ import annotations

import re

CHINA = "中国"
US = "美国"
OTHER = "其他"
UNKNOWN_ENTITY = "未识别"

# ===== 政府机构 → 归属地 =====
GOV_REGION: dict[str, str] = {
    # 美国
    "白宫": US, "国会": US, "参议院": US, "众议院": US, "FTC": US, "SEC": US,
    "司法部": US, "DOJ": US, "商务部": US, "五角大楼": US, "国防部": US,
    "州检察长": US, "FCC": US, "美联储": US, "CISA": US, "NIST": US, "FAA": US,
    "联邦贸易委员会": US, "美国国会": US,
    # 中国
    "网信办": CHINA, "工信部": CHINA, "发改委": CHINA, "国务院": CHINA,
    "科技部": CHINA, "信通院": CHINA, "国家数据局": CHINA,
    # 其他
    "欧盟委员会": OTHER, "欧洲议会": OTHER, "欧盟": OTHER, "英国议会": OTHER,
    "联合国": OTHER, "INTERPOL": OTHER, "OECD": OTHER, "G7": OTHER,
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
    "OpenRouter": US, "Stability AI": US, "Cohere": US, "Adept": US,
    "Inflection": US, "Midjourney": US, "Runway": US, "Suno": US,
    "ElevenLabs": US, "Character.AI": US, "Cloudflare": US, "Shopify": US,
    "Reddit": US, "Adobe": US, "Cerebras": US, "Uber": US, "YouTube": US,
    "AWS": US, "Harvey": US, "Pew": US, "Atlas": US, "Vivodyne": US,
    "Xenomi": US, "Tenet": US, "Together AI": US, "Groq": US, "SambaNova": US,
    "Databricks": US, "Snowflake": US, "Salesforce": US, "IBM": US,
    "Anysphere": US, "Windsurf": US,
    # 中国
    "字节跳动": CHINA, "字节": CHINA, "阿里巴巴": CHINA, "阿里云": CHINA,
    "阿里": CHINA, "通义": CHINA, "千问": CHINA, "腾讯": CHINA, "腾讯云": CHINA,
    "华为": CHINA, "昇腾": CHINA, "百度": CHINA, "文心": CHINA, "商汤": CHINA,
    "科大讯飞": CHINA, "讯飞": CHINA, "智谱": CHINA, "ZCode": CHINA,
    "月之暗面": CHINA, "Kimi": CHINA, "MiniMax": CHINA, "DeepSeek": CHINA,
    "深度求索": CHINA, "快手": CHINA, "美团": CHINA, "京东": CHINA,
    "小米": CHINA, "网易": CHINA, "百川智能": CHINA, "零一万物": CHINA,
    "阶跃星辰": CHINA, "宇树": CHINA, "Unitree": CHINA, "蚂蚁": CHINA,
    "拼多多": CHINA, "滴滴": CHINA, "蔚来": CHINA, "小鹏": CHINA,
    "理想": CHINA, "比亚迪": CHINA, "绿盟": CHINA, "Manus": CHINA,
    "Monica": CHINA, "智元": CHINA, "台积电": CHINA, "TSMC": CHINA,
    # 其他
    "Mistral": OTHER, "Sakana AI": OTHER, "NTT": OTHER, "软银": OTHER,
    "索尼": OTHER, "Sony": OTHER, "三星": OTHER, "LG": OTHER, "Naver": OTHER,
    "丰田": OTHER, "本田": OTHER, "乐天": OTHER, "ARM": OTHER,
    "SAP": OTHER, "ASML": OTHER, "三星电子": OTHER, "任天堂": OTHER,
}

# 展示顺序用：政府主体优先（同一标题里政府和公司都在时，政府是动作发出者）
_GOV_SORTED = sorted(GOV_REGION, key=len, reverse=True)
_COMP_SORTED = sorted(COMPANY_REGION, key=len, reverse=True)

# 地域兜底词（没有识别出任何主体时，用标题里的地域词猜一手）
_REGION_HINT: list[tuple[str, str]] = [
    (r"中国|国内|国产|中方|我国|网信办|工信部|发改委|国务院", CHINA),
    (r"美国|美方|白宫|国会|联邦|特朗普|硅谷", US),
    (r"欧盟|欧洲|英国|法国|德国|日本|韩国|印度|澳大利亚|加拿大|新加坡|中东|联合国",
     OTHER),
]


def _find(text: str, words: list[str]) -> str | None:
    low = text.lower()
    for w in words:
        if w.lower() in low:
            return w
    return None


def judge(title: str, summary: str = "") -> tuple[str, str, str]:
    """返回 (region, entity, kind)。kind ∈ {gov, company, none}。

    标题优先，标题判不出才用摘要。政府机构压过公司（谁在做事，谁就是主体）。
    """
    for text in (title or "", summary or ""):
        if not text:
            continue
        gov = _find(text, _GOV_SORTED)
        if gov:
            return GOV_REGION[gov], gov, "gov"
        comp = _find(text, _COMP_SORTED)
        if comp:
            return COMPANY_REGION[comp], comp, "company"
    # 主体没识别出来，用地域词兜底
    for text in (title or "", summary or ""):
        if not text:
            continue
        for pat, reg in _REGION_HINT:
            if re.search(pat, text):
                return reg, UNKNOWN_ENTITY, "none"
    return OTHER, UNKNOWN_ENTITY, "none"
