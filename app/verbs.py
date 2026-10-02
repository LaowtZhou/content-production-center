"""动词优先归类（v3）—— 按"发生了什么"归口，不按"谁的事"。

周老师 2026-10-03 定调：
- 分类主导权在**动词/动名词**。失控 = 安全，不管是"大模型失控"还是"智能体失控"；
  同一件事的每条素材动词相同，所以事件线不会因为成员各说各话而跨分类（默认不跨分类）。
- 优先级链（高 → 低）：安全 > 政策 > 商业 > Agent > 技术 > 应用。
  高优先级动作命中即定，不再看低优先级 —— 避免"OpenAI 融资"被模型名词抢走。

与 taxonomy.py 的关系（分工，不是两套分类）：
- `taxonomy.CATEGORIES` 仍是唯一的 7 类清单，本文件不新增类别。
- `verbs.classify()` 是判定实现（动词优先）；动词与领域词全不命中时，
  退回 `taxonomy.classify()` 的名词弱信号兜底，最后一档才是"其他"。
"""

from __future__ import annotations

import re

from app.taxonomy import OTHER, classify as _noun_classify

# ===== 动词/动名词优先级链（顺序即优先级，勿随意调）=====
# 政策排在安全前：标题主语是动作发出者 —— "FTC 调查 OpenAI 的安全实践"主线是监管动作，
# 应归政策；"OpenAI 智能体失控"没有监管动词，仍落安全（周老师要的那条不受影响）。
_VERB_CHAIN: list[tuple[str, re.Pattern]] = [
    ("政策与监管", re.compile(
        r"监管|调查|起诉|诉讼|被诉|罚款|立法|法案|法规|禁令|封禁|下架|制裁|听证|传唤|合规|判决|裁定"
        r"|牌照|许可|出口管制|行政令|关税|反垄断|备案|审查|处罚|追责|质询|诉请|管制|限制|审批|资质|禁售"
        r"|\bregulat|\bantitrust|\blawsuit|\bsued?\b|\bcourt\b|\bruling|\bbanned?\b|\bcompliance"
        r"|\bcongress|\bsenate\b|\bfine[sd]?\b|\bpenalt|\bprobe\b|\binvestigat",
        re.I)),
    ("AI 安全与风险", re.compile(
        r"失控|越狱|入侵|渗透|突破沙箱|攻击|遭攻击|被攻击|泄露|窃取|诈骗|欺诈|伪造|深度伪造|欺骗|隐瞒"
        r"|滥用|事故|宕机|崩溃|叛变|逃逸|伤害|偏见|歧视|隐私|后门|投毒|勒索|蠕虫|漏洞|恶意|钓鱼"
        r"|叫停|紧急叫停|暂停训练|暂停模型|暂停研发|安全事件|安全担忧|安全组织|安全联盟|安全标准|安全准则|安全协议|对齐失败|对齐|作恶|作弊|违规|翻车|闯祸"
        r"|\bmisalign|\bjailbreak|\bexploit|\bvulnerab|\battacks?\b|\bbreach|\bleak|\bscam|\bfraud"
        r"|\bdeepfake|\bprivacy|\bethic|\bbias\b|\bdiscriminat|\bworm\b|\bmalicious|\bpoison|\bhack"
        r"|\bstole|\bstolen|\bpaus\w*|\bhalted|\bsuspended|shut ?down|\bsafety (incident|concern)|\bmisuse",
        re.I)),
    ("行业与商业", re.compile(
        r"融资|募资|筹资|上市|IPO|招股|收购|并购|估值|营收|财报|亏损|盈利|裁员|离职|辞职|跳槽|合作|签署"
        r"|战略合作|投资|出售|破产|涨价|降价|定价|市值|股价|订单|市占|扩张|出海|订阅|套餐|商业化|变现"
        r"|获投|入股|分拆|重组|融资轮|亿元|万美元"
        r"|\bfunding|\braise[sd]?\b|\bvaluation|\brevenue|\bipo\b|\bacquisition|\bacquire|\bmerger\b"
        r"|\blayoffs?\b|\bearnings|\bprofit|\bmarket share|\bpartnership|\bseries [a-e]\b|\bsubscription"
        r"|\bpricing|\bbillion|\bmillion",
        re.I)),
    ("Agent 与智能体", re.compile(
        r"智能体|多智能体|自主执行|自主完成|编排|代理|智能代理|工作流|常驻智能体|沙箱|记忆系统"
        r"|computer use|\bmcp\b|autogpt|manus|swarm|crewai|langgraph|dify|扣子"
        r"|\bagents?\b|\bagentic|\bmulti-agent|tool use|\borchestrat|\bswarm\b|\bcrewai|\blanggraph|\bsandbox\b",
        re.I)),
    ("大模型与技术", re.compile(
        r"训练|微调|蒸馏|推理|架构|参数|上下文|多模态|跑分|评测|算力|芯片|数据集|权重|量化|思维链"
        r"|世界模型|基座模型|预训练|开源|模型发布|新模型|上线模型|模型路由|长文本|缩放|大模型"
        r"|\bllms?\b|large language model|foundation model|pre-?train|fine-?tun|distillat|\breasoning"
        r"|\bparameters|\bcontext window|\bmultimodal|long context|\barchitecture|\bbenchmark|\bgpu"
        r"|\bchips?\b|\btraining|\bdataset|\btokens?\b|\bscaling|open-?source|\bweights\b|\bquantiz"
        r"|\bchain of thought|world model|video model|speech model|text-to-(image|video)",
        re.I)),
    ("AI 工具与应用", re.compile(
        r"工具|应用|插件|集成|\bapi\b|功能|产品|办公|写作|绘画|出图|配音|视频生成|剪辑|翻译|笔记|日程"
        r"|客服|头像|简历|\bppt\b|表格|会议纪要|代码助手|copilot|\bide\b|小红书|抖音|公众号|短视频"
        r"|直播|电商|营销|独立开发|副业|效率工具|浏览器扩展|小程序|桌面端|手机端|智能硬件|穿戴|眼镜"
        r"|机器人|教育|医疗|搜索|上线|推出|发布|演示|体验|测评|实测|教程"
        r"|\btools?\b|\bapps?\b|\bplugins?\b|\bcopilot\b|\bide\b|\beditor\b|\bbrowser|\boffice\b"
        r"|\bwriting\b|\btranslati|\bnotes?\b|\bcalendar|\bcustomer service|\beducation|\bhealthcare"
        r"|\be-commerce|\bmarketing|\bdesign\b|\bediting|\bsearch\b|\bglasses\b|\brobots?\b|\bwearable"
        r"|powerpoint|\bpresentation|\blaunch|\brelease|\bupgrade|\bproduct|\bfeature",
        re.I)),
]


def classify(title: str, summary: str = "") -> str:
    """动词优先归口：标题先按优先级链扫，再扫摘要；全不命中才退回名词兜底。"""
    t = title or ""
    s = summary or ""
    for name, pat in _VERB_CHAIN:
        if pat.search(t):
            return name
    for name, pat in _VERB_CHAIN:
        if pat.search(s):
            return name
    return _noun_classify(t, s)


def hit_chain(text: str) -> str | None:
    """诊断用：单独看一段文本命中哪一类（不落库）。"""
    for name, pat in _VERB_CHAIN:
        if pat.search(text or ""):
            return name
    return None
