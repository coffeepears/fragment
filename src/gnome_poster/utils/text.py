"""文本工具：拼音、模糊匹配、关键词 / 话题抽取。

pypinyin 是可选依赖：
  * 有它 → 拼音全拼 + 首字母都能搜（推荐，项目自带 vendor/ 里就有一份）
  * 没它 → 自动降级成「昵称子串 + 英文 handle 子序列」匹配，功能不崩，只是中文搜不了拼音
"""

from __future__ import annotations

import functools
import logging
import re
import unicodedata
import warnings
from collections import Counter

try:  # pragma: no cover - 取决于环境
    from pypinyin import Style, lazy_pinyin

    HAS_PYPINYIN = True
except ImportError:  # pragma: no cover
    HAS_PYPINYIN = False

try:  # pragma: no cover
    with warnings.catch_warnings():
        # jieba 0.42 的源码里有几处没转义的 `\.`，Python 3.12+ 会在编译期报警告。
        # 那是它的历史遗留，不属于本项目的问题，压掉别吓着用户。
        warnings.simplefilter("ignore", SyntaxWarning)
        import jieba  # type: ignore

    HAS_JIEBA = True
    jieba.setLogLevel(logging.ERROR)  # 别往 stdout 喷「Building prefix dict」
except ImportError:  # pragma: no cover
    HAS_JIEBA = False

#: jieba 的词性标注（posseg）是懒加载的，第一次要 0.5s 读模型
_posseg: object | None = None

#: 只把「名词类」当话题候选 —— 这是标签质量的关键一步
NOUN_FLAGS = frozenset({"n", "nr", "ns", "nt", "nz", "nw", "vn", "an", "eng", "l", "i", "j"})


CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK_RUN = re.compile(f"[{CJK}]+")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_'-]{2,}")
_HASHTAG = re.compile(r"[#＃]([^\s#＃]{1,40})")
_MENTION = re.compile(f"[@＠]([A-Za-z0-9_.\\-{CJK}]{{1,40}})")

__all__ = [
    "HAS_PYPINYIN",
    "HAS_JIEBA",
    "NOUN_FLAGS",
    "pinyin_forms",
    "normalize",
    "fuzzy_score",
    "match_score",
    "extract_hashtags",
    "extract_mentions",
    "extract_keywords",
    "strip_tags",
    "elide",
    "warm_up",
]


def _get_posseg(load: bool = False):
    """拿 jieba 的词性标注模块。

    **`load=False`（默认）时绝不触发模型加载** —— 那是 ~0.5s 的磁盘 IO，
    发生在主线程就会把窗口首帧卡住（真踩过：装成 deb 后跑自检，
    窗口 2 秒都没完成布局）。

    所以策略是：UI 路径用默认值，能蹭到就蹭；真正加载交给后台的
    `warm_up()`（`load=True`）。预热完成后所有后续调用自动用上词性。
    """
    global _posseg
    if _posseg is None:
        if not HAS_JIEBA:
            _posseg = False
        elif not load:
            return None
        else:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    import jieba.posseg as pseg  # type: ignore

                _posseg = pseg
            except Exception:  # noqa: BLE001
                _posseg = False
    return _posseg or None


# --------------------------------------------------------------------------- #
# 拼音
# --------------------------------------------------------------------------- #
@functools.lru_cache(maxsize=4096)
def pinyin_forms(text: str) -> tuple[str, str]:
    """返回 (全拼, 首字母)。非中文原样保留。

    >>> pinyin_forms("林深见鹿")
    ('linshenjianlu', 'lsjl')
    """
    if not text:
        return "", ""
    if not HAS_PYPINYIN:
        return "", ""
    if not _CJK_RUN.search(text):
        return "", ""
    try:
        full = "".join(lazy_pinyin(text, style=Style.NORMAL, errors="default"))
        initials = "".join(lazy_pinyin(text, style=Style.FIRST_LETTER, errors="default"))
    except Exception:  # noqa: BLE001 - 拼音库内部异常不该拖垮 UI
        return "", ""
    return normalize(full), normalize(initials)


def normalize(text: str) -> str:
    """去掉空白 / 下划线 / 连字符，转小写，NFKC 折叠全角。"""
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"[\s_\-·．.]+", "", text).casefold()


# --------------------------------------------------------------------------- #
# 模糊匹配
# --------------------------------------------------------------------------- #
def _subsequence(haystack: str, needle: str) -> tuple[bool, int]:
    """needle 是否为 haystack 的子序列，并返回“紧凑度”惩罚（越小越好）。"""
    if not needle:
        return True, 0
    it = iter(haystack)
    gaps = 0
    last_hit = -1
    index = -1
    for ch in needle:
        found = -1
        for pos, hay in enumerate(it, start=index + 1):
            if hay == ch:
                found = pos
                break
        if found < 0:
            return False, 0
        if last_hit >= 0:
            gaps += found - last_hit - 1
        last_hit = found
        index = found
    return True, gaps


def fuzzy_score(query: str, target: str) -> int:
    """给一个候选串打分，0 表示不匹配。分数越高越靠前。

    分层（这也是大多数补全器的套路）：
      完全相等 > 前缀 > 包含（越靠前越高）> 子序列（越紧凑越高）
    """
    q = normalize(query)
    t = normalize(target)
    if not q or not t:
        return 0
    if q == t:
        return 1200
    if t.startswith(q):
        return 1000 - min(len(t) - len(q), 100)
    pos = t.find(q)
    if pos >= 0:
        return 800 - min(pos, 100)
    ok, gaps = _subsequence(t, q)
    if ok:
        return max(400 - gaps, 1)
    return 0


def match_score(
    query: str,
    *,
    name: str = "",
    handle: str = "",
    pinyin: str = "",
    initials: str = "",
    handle_weight: float = 0.9,
) -> int:
    """对一个联系人算总分。昵称 > 拼音全拼 > 拼音首字母 > handle。"""
    q = (query or "").strip()
    if not q:
        return 1  # 空查询：全都算候选
    if not pinyin and not initials and name:
        pinyin, initials = pinyin_forms(name)

    score = fuzzy_score(q, name)
    if pinyin:
        score = max(score, int(fuzzy_score(q, pinyin) * 0.95))
    if initials:
        # 首字母命中给高权重：敲 "lsjl" 想找 "林深见鹿" 是主场景
        score = max(score, int(fuzzy_score(q, initials) * 1.05))
    if handle:
        score = max(score, int(fuzzy_score(q, handle.lstrip("@")) * handle_weight))
    return score


# --------------------------------------------------------------------------- #
# 标签 / 提及抽取
# --------------------------------------------------------------------------- #
def extract_hashtags(text: str) -> list[str]:
    seen: list[str] = []
    for match in _HASHTAG.finditer(text or ""):
        tag = match.group(1).strip()
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def extract_mentions(text: str) -> list[str]:
    seen: list[str] = []
    for match in _MENTION.finditer(text or ""):
        handle = match.group(1).strip()
        if handle and handle not in seen:
            seen.append(handle)
    return seen


def strip_tags(text: str) -> str:
    return _HASHTAG.sub(" ", text or "")


# 高频虚词 / 无意义组合，直接过滤掉
_STOPWORDS = frozenset(
    """
    我们 你们 他们 她们 它们 这个 那个 这些 那些 什么 怎么 为什么 因为 所以 但是 而且 就是
    可以 一个 一些 已经 还有 没有 不是 不能 不要 这样 那样 时候 现在 今天 明天 昨天 自己
    觉得 感觉 真的 非常 特别 有点 一下 一直 一起 之后 之前 的话 东西 事情 问题 地方 样子
    分享 推荐 记录 日常 生活 大家 朋友 姐妹 兄弟 希望 喜欢 开心 好看 好吃 这件 那次
    这次 一次 到处 上去 下来 进来 出来 起来 过来 开来 帮忙 看看 试试 知道 觉得 认为
    the and for with that this from your you are was were will can not but all any how what
    when where which while have has had been being into over under about just very more most
    """.split()
)

# 明显不该当话题的短词
_TAG_BLACKLIST = frozenset(
    "的了呢吧啊嘛呀哦嗯哈 我你他她它 是不在有和与或 就都也还很太 上中下前后里外 一二三四五六七八九十".split()
)

#: 出现在词首/词尾就丢掉整个候选的功能字（切词切歪时靠它挡掉「#的事做顺」这类垃圾）
_FUNCTION_CHARS = frozenset(
    "的了着呢吧啊嘛呀哦嗯哈 和与或及 就都也还太更最再又已 是不没无非未 上中下前后里外"
    "把被给对从向往于 这那某每各 个件种次些 我你他她它们 会要能可 做发用让使 时天年月日"
    "什么怎为所以但而且 过起来去 很挺稍微 和跟同 一"
)


def _cjk_ngrams(run: str, min_n: int = 2, max_n: int = 4) -> Counter:
    grams: Counter = Counter()
    length = len(run)
    for n in range(min_n, max_n + 1):
        for i in range(length - n + 1):
            grams[run[i : i + n]] += 1
    return grams


def _dedupe_substrings(scored: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """去掉被更长词条完全包含且分数更低的碎片（"咖啡" vs "咖啡店"）。"""
    ordered = sorted(scored, key=lambda kv: (-kv[1], -len(kv[0])))
    kept: list[tuple[str, float]] = []
    for tag, score in ordered:
        if any(tag in longer and tag != longer for longer, _ in kept):
            continue
        kept.append((tag, score))
    return kept


def _is_clean_candidate(word: str) -> bool:
    """候选词的基础卫生检查：长度、停用词、首尾不能是功能字。"""
    if len(word) < 2:
        return False
    if word in _STOPWORDS or word in _TAG_BLACKLIST:
        return False
    if word[0] in _FUNCTION_CHARS or word[-1] in _FUNCTION_CHARS:
        return False
    if _CJK_RUN.fullmatch(word):
        return True
    return bool(_LATIN_WORD.fullmatch(word))


def extract_keywords(text: str, limit: int = 12, *, load_posseg: bool = False) -> list[str]:
    """从正文里抽候选话题。

    有 jieba：按词切，能做词性标注就只留名词（质量最好）。
    `load_posseg=False`（默认，UI 路径）时不会触发 posseg 模型加载，
    预热完成后词性自动生效；命令行/脚本里想要最好效果可以传 True。

    没 jieba：**精度优先**的 n-gram 兜底 —— 只保留「在文中重复出现」的 n-gram，
    或者「整段本身就是个短词」的情况。宁可少给，也不给「#的事做顺」这种垃圾。
    """
    body = strip_tags(text or "")
    if not body.strip():
        return []

    scores: dict[str, float] = {}

    def bump(word: str, score: float) -> None:
        scores[word] = max(scores.get(word, 0.0), score)

    if HAS_JIEBA:
        pseg = _get_posseg(load=load_posseg)
        if pseg is not None:
            # 有词性标注：只要名词类，动词 / 副词 / 代词全丢掉
            for pair in pseg.cut(body):
                word = pair.word.strip()
                if pair.flag not in NOUN_FLAGS or not _is_clean_candidate(word):
                    continue
                bump(word, 2.4 + len(word) * 0.4)
        else:
            for word in jieba.cut(body):  # type: ignore[union-attr]
                word = word.strip()
                if not _is_clean_candidate(word):
                    continue
                bump(word, 2.0 + len(word) * 0.35)
    else:
        for run in _CJK_RUN.findall(body):
            if len(run) < 2:
                continue
            # 整段就是个短词（「咖啡」「露营」），直接算一个候选
            if len(run) <= 4 and _is_clean_candidate(run):
                bump(run, 2.0 + len(run) * 0.35)
            for gram, count in _cjk_ngrams(run, 2, 5).items():
                # 只信重复出现的组合；只出现一次的长串基本都是切歪的
                if count < 2 or not _is_clean_candidate(gram):
                    continue
                bump(gram, count * 1.6 + len(gram) * 0.35)

    for word in _LATIN_WORD.findall(body):
        if word.lower() in _STOPWORDS:
            continue
        bump(word, 1.6 + len(word) * 0.2)

    ranked = _dedupe_substrings(list(scores.items()))
    return [tag for tag, _ in ranked[:limit]]


def elide(text: str, length: int = 24) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= length else text[: length - 1] + "…"


def warm_up() -> bool:
    """预热 jieba + 词性模型（首次合计约 1s，全是磁盘 IO）。

    必须在后台线程调用 —— 这是唯一会触发 posseg 模型加载的地方。
    """
    if not HAS_JIEBA:
        return False
    try:
        jieba.initialize()  # type: ignore[union-attr]
        list(jieba.cut("预热"))  # type: ignore[union-attr]
        pseg = _get_posseg(load=True)
        if pseg is not None:
            list(pseg.cut("预热"))
        return True
    except Exception:  # noqa: BLE001
        return False
