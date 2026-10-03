"""选题类别体系 —— 8 类固定清单（唯一事实来源）。

约束（2026-10-02 周老师定）：
- 类别固定为下面这几个，AI 只能从这几个里选；要加新类只能人工改本文件。
  这样类别永远膨胀不了。
  2026-10-03 周老师授权新增第 8 类「行业讨论」：用于收编**没有单一主体的**
  观点、评论、圆桌、盘点、趋势综述——此前这类内容无处可去，全被判"未识别"，
  在归类树里翻不到。
- 分类只做"归口"，不做价值判断；分类结果写入 topics.category。
- 历史回填、增量自动分类、Agent 覆盖，全部收敛到 classify() / is_valid_category()，
  同一套规则只允许有一份实现。

分类算法（v2，修正 v1"泛词吃掉一半标题"的问题）：
- 每个类别分「强信号」和「弱信号」两档词表。
  强信号 = 出现这个词，几乎可断定属于该类（如"融资/法案/越狱"）。
  弱信号 = 只是相关，不足以定类（如"发布/合作/助手"）。
- 先看强信号：谁强信号得分最高就归谁（并列按 PRIORITY）。
- 没有强信号时看弱信号：达到 _MIN_WEAK 才归口，否则进"其他"。
- 标题权重 1.0，摘要权重 0.4。
"""

from __future__ import annotations

import re

# ===== 唯一的类别清单（顺序即展示顺序，也是并列时的优先级）=====
CATEGORIES = [
    {"key": "model",    "name": "大模型与技术",   "desc": "模型发布、能力评估、技术路线、训练与推理"},
    {"key": "agent",    "name": "Agent 与智能体", "desc": "智能体、自动化、工作流、MCP、多智能体协作"},
    {"key": "safety",   "name": "AI 安全与风险",  "desc": "安全、对齐、失控、漏洞、滥用、隐私、伦理"},
    {"key": "business", "name": "行业与商业",     "desc": "融资、收购、估值、营收、公司动态、合作"},
    {"key": "policy",   "name": "政策与监管",     "desc": "法规、监管、诉讼、禁令、标准、政府动作"},
    {"key": "app",      "name": "AI 工具与应用",  "desc": "工具、产品、应用、插件、Copilot、办公提效"},
    {"key": "discuss",  "name": "行业讨论",       "desc": "观点、评论、圆桌、盘点、趋势综述、访谈辩论"},
    {"key": "other",    "name": "其他",           "desc": "不属于以上各类"},
]

CATEGORY_NAMES = [c["name"] for c in CATEGORIES]
OTHER = "其他"

# 并列时的优先级：越靠前越"具体"，优先归它（在 CATEGORIES 顺序基础上微调）
PRIORITY = ["政策与监管", "AI 安全与风险", "Agent 与智能体",
            "行业与商业", "AI 工具与应用", "大模型与技术"]

# 弱信号单独归口所需的最低分（低于此进"其他"，避免硬塞）
_MIN_WEAK = 1.5

# ===== 分类词表 =====
# strong: 出现即高度确定；weak: 相关但不充分。均按正则，忽略大小写。
_RULES: dict[str, dict[str, list[str]]] = {
    "政策与监管": {
        "strong": [
            r"监管|法案|法规|立法|合规|法院|诉讼|起诉|被诉|判决|裁定|禁令|封禁|下架|政府|国会|议会|欧盟|白宫|罚款|制裁|政策|条例|听证|著作权|版权|反垄断|备案|牌照|许可|AI Act|行政令|关税|出口管制|审计要求",
            r"\bregulation|\bregulator|\blawsuit|\bsued?\b|\bcourt\b|\bruling|\bbanned?\b|\bantitrust|\bcongress|\bsenate\b|\bEU\b|white house|\bfine\b|\bpenalty|\bcopyright|\bcompliance\b|executive order|export control|\btariff",
        ],
        "weak": [r"审查|标准|规定|要求|争议|合规性|条例",
                 r"\bstandard|\breview\b|\brequirement|\bpolicy\b"],
    },
    "AI 安全与风险": {
        "strong": [
            r"安全|对齐|失控|越狱|漏洞|攻击|滥用|诈骗|虚假信息|深度伪造|deepfake|后门|投毒|欺骗|歧视|偏见|隐私|数据泄露|伦理|事故|暗黑|风险控制|防护|网络攻击|断电|劫持|蠕虫",
            r"\bsecur|\bsafety|\balignment|misalign|\bjailbreak|\bexploit|\bvulnerab|\battack|\bmisuse|\bscam|\bfraud|\bdeepfake|\bprivacy|\bleak|\bbreach|\bethic|\bbias\b|\bdiscriminat|\bworm\b|\bmalicious|\bhallucinat|\bpoison",
        ],
        "weak": [r"风险|隐患|争议|质疑|担忧|警告|威胁",
                 r"\brisk|\bconcern|\bwarning|\bdebate|\bcontrovers|\bworry|\bthreat"],
    },
    "Agent 与智能体": {
        "strong": [
            r"智能体|多智能体|\bmcp\b|工具调用|computer use|autogpt|manus|自主执行|浏览器操作|任务执行|记忆系统|规划能力|swarm|crewai|langgraph|dify|扣子|智能代理",
            r"\bagents?\b|\bagentic|\bmulti-agent|\bmcp\b|computer use|tool use|\borchestrat|\bmanus\b|\bautogpt|\bswarm\b|\bcrewai|\blanggraph|\bsandbox|\bmemory\b",
        ],
        "weak": [r"代理|工作流|自动化|编排|自主|插件市场|沙箱",
                 r"\bworkflow|\bautomation|\bautonomous|\bassistant\b"],
    },
    "行业与商业": {
        "strong": [
            r"融资|收购|并购|估值|营收|上市|\bipo\b|财报|裁员|亿美元|亿元|万亿美元|股东|变现|商业模式|定价|涨价|降价|亏损|盈利|市值|股价|订单|市占率|市场份额|战略合作|签署协议|获投|高管离职|反垄断调查",
            r"\bfunding\b|\braise[sd]?\b|\bvaluation|\brevenue|\bipo\b|\bacquisition|\bacquire|\bmerger\b|\blayoffs?\b|\bearnings|\bprofit\b|\bmarket share|\bpartnership|\bseries [a-e]\b",
        ],
        "weak": [r"合作|协议|竞争|扩张|布局|客户|出海|商业化|生态",
                 r"\bpartnership|\bdeal\b|\bcompetit|\bexpansion|\bcustomers?\b|\bbillion|\bmillion|\bmarket\b"],
    },
    "AI 工具与应用": {
        "strong": [
            r"办公|写作|绘画|出图|配音|视频生成|剪辑|翻译|笔记|日程|客服|头像|简历|ppt|表格|会议纪要|代码助手|\bcopilot\b|\bide\b|小红书|抖音|公众号|\bb站\b|短视频|直播|电商|营销|私域|独立开发|副业|效率工具|浏览器扩展|小程序|\bapp\b|桌面端|手机端|智能硬件|穿戴|眼镜|机器人|教育|医疗|法律助手|搜索",
            r"\btools?\b|\bapps?\b|\bplugins?\b|\bcopilot\b|\bide\b|\beditor\b|\bbrowser|\boffice\b|\bwriting\b|\btranslati|\bnotes?\b|\bcalendar|\bcustomer service|\beducation|\bhealthcare|\be-commerce|\bmarketing|\bdesign|\bediting|\bsearch\b|\bglasses\b|\brobots?\b|\bwearable|powerpoint|\bpresentation",
        ],
        "weak": [r"工具|产品|应用|助手|功能|上线|发布|平台|体验|教程|测评|实测",
                 r"\bproduct|\bfeature|\blaunch|\bplatform|\bexperience|\btutorial|\breview"],
    },
    "大模型与技术": {
        "strong": [
            r"大模型|基座模型|预训练|微调|蒸馏|推理能力|参数|上下文窗口|多模态|长文本|架构|跑分|基准|评测|benchmark|算子|算力|芯片|\bgpu\b|训练|数据集|token|上下文|缩放|scaling|开源模型|权重|量化|思维链|推理模型|世界模型|视频模型|语音模型|文生图|文生视频|模型发布|新模型|上线模型|模型路由",
            r"\bllms?\b|large language model|foundation model|pre-?train|fine-?tun|distillat|\breasoning|\bparameters|\bcontext window|\bmultimodal|long context|\barchitecture|\bbenchmark|\bgpu|\bchips?\b|\btraining|\bdataset|\btokens?\b|\bscaling|open-?source model|\bweights\b|\bquantiz|\bchain of thought|world model|video model|speech model|text-to-(image|video)|\brouting\b",
        ],
        "weak": [
            r"gpt|claude|gemini|llama|qwen|通义|文心|deepseek|豆包|kimi|grok|gemma|mistral|sora|midjourney|stable diffusion|flux|\bglm\b|minimax|混元|星火|模型|能力|版本|开源|发布|上线|升级",
            r"\bmodels?\b|\bversion|\brelease|\bupgrade",
        ],
    },
}

_COMPILED: list[tuple[str, list[tuple[re.Pattern, str]]]] = [
    (name, [(re.compile(p, re.I), tier) for tier in ("strong", "weak") for p in tiers[tier]])
    for name, tiers in _RULES.items()
]


def is_valid_category(name: str | None) -> bool:
    """是否为合法的 7 类之一。AI 只能写这 7 个值。"""
    return bool(name) and name in CATEGORY_NAMES


def classify(title: str, summary: str = "") -> str:
    """按关键词给选题归口，返回 7 类之一。看不懂就归"其他"。

    标题权重 1.0，摘要权重 0.4。强信号优先；无强信号时按弱信号定夺。
    """
    t = title or ""
    s = summary or ""

    strong: dict[str, float] = {}
    weak: dict[str, float] = {}
    for name, rules in _COMPILED:
        for pattern, tier in rules:
            w = 0.0
            if pattern.search(t):
                w = 1.0
            elif pattern.search(s):
                w = 0.4
            if w == 0:
                continue
            bucket = strong if tier == "strong" else weak
            bucket[name] = bucket.get(name, 0.0) + w

    pool = strong if strong else weak
    if not pool:
        return OTHER
    best = max(pool.values())
    if not strong and best < _MIN_WEAK:
        return OTHER
    winners = [n for n, v in pool.items() if v == best]
    if len(winners) == 1:
        return winners[0]
    for name in PRIORITY:
        if name in winners:
            return name
    return winners[0]


def category_meta() -> list[dict]:
    """给前端用的类别元数据（含 key / name / desc）。"""
    return [dict(c) for c in CATEGORIES]
