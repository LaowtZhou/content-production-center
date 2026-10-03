"""归类树的展示顺序 —— 二级地域固定序、三级主体按字母/拼音首字母序。

周老师 2026-10-03 定：
- 二级「地域」顺序**固定**为 中国 → 美国 → 其他，不按条数排。
  （按条数排会随数据变动跳来跳去，找东西时位置不固定。）
- 三级「主体」按**首字母**排：英文按字母，中文按拼音首字母。
  同一首字母内再按完整拼音/字母序。

为什么不用 pypinyin：本项目是一条"不加依赖"的约束（架构锁定），
而 GB2312 一级汉字区（0xB0A1–0xD7F9，3755 字）本身就是**按拼音排序**的，
其首字母边界是公开的固定表。用 `str.encode('gbk')` 取码位查表即可，
零依赖、零维护；覆盖不到的冷僻字退回 '#'，排在字母之后（不消失、不报错）。
"""

from __future__ import annotations

# ===== 二级：地域固定顺序 =====
# 顺序即展示顺序，不参与排序键的"计算"。
REGION_ORDER = ["中国", "美国", "其他"]

# 收在字母/汉字之后（数字、符号、空值），保证任何值都排得进去、不丢。
_TAIL = "~"


def region_index(name: str | None) -> int:
    """地域在固定顺序里的位置；不在清单里的排到最后（不丢数据）。"""
    try:
        return REGION_ORDER.index((name or "").strip())
    except ValueError:
        return len(REGION_ORDER)


def region_sort_key(name: str | None) -> tuple[int, str]:
    return (region_index(name), (name or "").strip())


# ===== 中文首字母：GB2312 一级汉字区边界表 =====
# (起始码位, 首字母)。升序，取"最后一个 <= 码位"的那一格。
_PY_BOUNDS: tuple[tuple[int, str], ...] = (
    (0xB0A1, "A"), (0xB0C5, "B"), (0xB2C1, "C"), (0xB4EE, "D"),
    (0xB6EA, "E"), (0xB7A2, "F"), (0xB8C1, "G"), (0xB9FE, "H"),
    (0xBBF7, "J"), (0xBFA6, "K"), (0xC0AC, "L"), (0xC2E8, "M"),
    (0xC4C3, "N"), (0xC5B6, "O"), (0xC5BE, "P"), (0xC6DA, "Q"),
    (0xC8BB, "R"), (0xC8F6, "S"), (0xCBFA, "T"), (0xCDDA, "W"),
    (0xCEF4, "X"), (0xD1B9, "Y"), (0xD4D1, "Z"),
)
_PY_END = 0xD7FA  # 一级汉字区上界（不含）

# 兜底表：GB2312 **二级区**（0xD8A1 起）是按部首排的，取不到拼音。
# 这里只补"中文公司名/人名里真会当首字"的那批冷僻字，
# **只在边界表取不到时才生效**（不会覆盖一级区的正确结果）。
# 实测缺口来自：哔哩哔哩（哔）、昇腾（昇）、睿尔曼（睿）；其余为常见字号/人名首字。
_PY_EXTRA: dict[str, str] = {
    "昇": "S", "哔": "B", "睿": "R", "岚": "L", "骁": "X", "誉": "Y",
    "曦": "X", "晗": "H", "骏": "J", "鑫": "X", "淼": "M", "垚": "Y",
    "喆": "Z", "玥": "Y", "珺": "J", "颢": "H", "熠": "Y", "罡": "G",
    "铄": "S", "昊": "H", "昀": "Y", "珩": "H", "琨": "K", "珂": "K",
    "瑶": "Y", "璐": "L", "茜": "Q", "薇": "W", "蕾": "L", "莹": "Y",
    "楷": "K", "楠": "N", "桐": "T", "荃": "Q", "茗": "M", "萱": "X",
    "藤": "T", "屹": "Y", "岷": "M", "峻": "J", "峪": "Y", "岑": "C",
    "峥": "Z", "旻": "M", "氪": "K", "奕": "Y", "晟": "S", "溥": "P",
}


def pinyin_initial(ch: str) -> str:
    """取单个汉字的拼音首字母；查不到返回 _TAIL（排到字母之后，不消失）。"""
    if not ch:
        return _TAIL
    try:
        raw = ch.encode("gbk")
    except UnicodeEncodeError:
        return _PY_EXTRA.get(ch, _TAIL)
    if len(raw) != 2:
        return _TAIL
    code = (raw[0] << 8) | raw[1]
    if not (_PY_BOUNDS[0][0] <= code < _PY_END):
        # 二级区 / 非汉字：先查兜底表
        return _PY_EXTRA.get(ch, _TAIL)
    letter = _PY_BOUNDS[0][1]
    for bound, lt in _PY_BOUNDS:
        if code < bound:
            break
        letter = lt
    return letter


def _sort_bytes(s: str) -> bytes:
    """同首字母内的次序：GB2312 码位序 ≈ 拼音序，英文即字母序。"""
    try:
        return s.encode("gbk")
    except UnicodeEncodeError:
        return s.encode("utf-8")


def entity_letter(name: str | None) -> str:
    """主体的分组首字母：英文取首字母，中文取拼音首字母，其余归 _TAIL。"""
    n = (name or "").strip()
    if not n:
        return _TAIL
    ch = n[0]
    if ch.isascii() and ch.isalpha():
        return ch.upper()
    if "\u4e00" <= ch <= "\u9fff":
        return pinyin_initial(ch)
    return _TAIL


def entity_sort_key(name: str | None) -> tuple[str, bytes, str]:
    """三级主体的排序键：(首字母, 同字母内码位序, 原名兜底)。"""
    n = (name or "").strip()
    letter = entity_letter(n)
    # 有首字母的排前面（bucket=0），_TAIL（数字/符号/空）排最后（bucket=1）。
    bucket = "1" if letter == _TAIL else "0"
    return (bucket + letter, _sort_bytes(n), n)
