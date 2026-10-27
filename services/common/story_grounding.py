"""Domain models and helpers for children's story grounding and volume control.

Enforces:
1. Priority 1: Match and prioritize local pre-stored Flash/SD audio stories
   (story_01: 小星星的枕头, story_02: 月亮不着急, story_03: 大熊和森林里的悄悄话, night_soothe).
2. Fallback to cloud LLM generation for unknown or non-local stories.
3. Anti-hallucination search-then-generate for IP-based stories (e.g. 汪汪队立大功, 小猪佩奇, 奥特曼):
   extracts IP, triggers canon search, and injects authoritative grounded constraints.
4. Voice volume command recognition and adjustment (loudness_rate: -50 to 100).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

# ---------------------------------------------------------------------------
# 1. Local Pre-stored Stories
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalStoryInfo:
    story_id: str
    title: str
    aliases: tuple[str, ...]
    synopsis: str


LOCAL_STORIES: Final[Mapping[str, LocalStoryInfo]] = {
    "story_01": LocalStoryInfo(
        story_id="story_01",
        title="小星星的枕头",
        aliases=(
            "小星星的枕头",
            "小星星",
            "星星枕头",
            "星星的枕头",
            "故事一",
            "第一个故事",
            "第一篇故事",
        ),
        synopsis=(
            "天黑了，小星星的枕头不见了。小星星跟着风在小河边找到了正抱着它睡觉的小青蛙。"
            "小星星帮小青蛙找到了荷叶床，拿回了自己的枕头，安心入睡。"
        ),
    ),
    "story_02": LocalStoryInfo(
        story_id="story_02",
        title="月亮不着急",
        aliases=(
            "月亮不着急",
            "月亮",
            "小兔子和月亮",
            "小兔子的胡萝卜",
            "故事二",
            "第二个故事",
            "第二篇故事",
        ),
        synopsis=(
            "小兔子跑着去奶奶家摔了一跤，胡萝卜散落一地。月亮把光洒在草地上，教小兔子慢慢捡起胡萝卜。"
            "小兔子学会了慢慢走，听到了虫鸣，看到了萤火虫，平安温暖地到达了奶奶家。"
        ),
    ),
    "story_03": LocalStoryInfo(
        story_id="story_03",
        title="大熊和森林里的悄悄话",
        aliases=(
            "大熊和森林里的悄悄话",
            "大熊",
            "一只会听的熊",
            "森林里的悄悄话",
            "故事三",
            "第三个故事",
            "第三篇故事",
        ),
        synopsis=(
            "森林里的大熊不太爱说话，但特别会认真倾听。它帮小松鼠掏出树洞里的松果，"
            "陪练习游泳的小鸭子加油，安安静静听小刺猬倾诉烦恼，给森林里的小伙伴带来温暖。"
        ),
    ),
    "night_soothe": LocalStoryInfo(
        story_id="night_soothe",
        title="夜晚安抚",
        aliases=(
            "夜晚安抚",
            "睡前安抚",
            "哄睡",
            "安抚",
            "睡前音乐",
            "晚安故事",
        ),
        synopsis="用最温润轻缓的语调安抚孩子，陪伴孩子放松身心进入甜美梦乡。",
    ),
}

ORDERED_LOCAL_STORY_IDS: Final[tuple[str, ...]] = ("story_01", "story_02", "story_03")

# ---------------------------------------------------------------------------
# 2. Known Children's IPs and Verified Canon Lore
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChildIpCanon:
    canonical_name: str
    aliases: tuple[str, ...]
    canon_summary: str


KNOWN_CHILD_IPS: Final[Mapping[str, ChildIpCanon]] = {
    "汪汪队": ChildIpCanon(
        canonical_name="汪汪队立大功",
        aliases=("汪汪队", "汪汪队立大功", "paw patrol", "阿奇", "毛毛", "天天", "莱德"),
        canon_summary=(
            "《汪汪队立大功》（PAW Patrol）官方角色、职责、装备与载具设定：\n"
            "1. 莱德队长（Ryder）：10岁精通科技的少年队长，红色全地形越野车，口号：“只要你遇到麻烦，就大声呼救！”\n"
            "2. 阿奇（Chase）：德国牧羊犬，担任交警与特警，驾驶蓝色警车，配扩音器和发射网枪，口号：“包在警犬阿奇身上！”\n"
            "3. 毛毛（Marshall）：大麦町斑点狗，担任消防犬与急救犬，驾驶红色消防车/急救车，用水炮灭火，口号：“我，火力全开！”\n"
            "4. 天天（Skye）：可卡颇犬，飞行搜救犬，驾驶粉色直升机，背负飞行羽翼，口号：“狗狗要飞天了！”\n"
            "5. 灰灰（Rocky）：混血环保维修犬，驾驶绿色回收车，随身工具背包能修理一切，口号：“绿色代表走，我绝不浪费！”\n"
            "6. 路马（Zuma）：拉布拉多水上搜救犬，驾驶橙色气垫船与潜水艇，口号：“开始行动吧！”\n"
            "7. 小砾（Rubble）：英国斗牛犬工程犬，驾驶黄色推土机与挖掘机，口号：“小砾往前冲！”"
        ),
    ),
    "小猪佩奇": ChildIpCanon(
        canonical_name="小猪佩奇",
        aliases=("小猪佩奇", "佩奇", "乔治", "peppa pig", "粉红猪小妹"),
        canon_summary=(
            "《小猪佩奇》（Peppa Pig）官方家庭成员与特色设定：\n"
            "1. 佩奇（Peppa）：4岁活泼可爱的粉红小猪，最喜欢穿红裙子踩泥坑，爱和小伙伴苏西羊一起玩。\n"
            "2. 乔治（George）：佩奇的弟弟，最喜欢的玩具是绿色恐龙，喜欢喊“恐龙，嗷呜！”。\n"
            "3. 猪爸爸（Daddy Pig）：戴眼镜、爱看报纸、爱吹牛自己是专家，虽然容易迷路但极具幽默感。\n"
            "4. 猪妈妈（Mummy Pig）：温柔智慧，擅长做美味煎饼，在家用电脑工作。"
        ),
    ),
    "奥特曼": ChildIpCanon(
        canonical_name="奥特曼",
        aliases=("奥特曼", "迪迦", "赛罗", "特利迦", "欧布", "捷德", "光之巨人"),
        canon_summary=(
            "《奥特曼》系列英雄与世界观设定：\n"
            "1. 来自M78星云光之国的宇宙英雄，保卫地球与宇宙和平，化身光芒对抗怪兽与黑暗。\n"
            "2. 迪迦奥特曼：拥有复合型、强力型（红色力量）、空中型（紫色速度）三种战斗形态，经典必杀技为哉佩利敖光线。\n"
            "3. 赛罗奥特曼：赛文奥特曼之子，胸前装备终极铠甲，手持头标武器，性格热情勇敢，口号：“想赢我还早两万年呢！”。"
        ),
    ),
    "海底小纵队": ChildIpCanon(
        canonical_name="海底小纵队",
        aliases=("海底小纵队", "巴克队长", "呱唧", "皮医生", "章鱼堡", "octonauts"),
        canon_summary=(
            "《海底小纵队》（Octonauts）官方角色与行动信条：\n"
            "核心口号：“探索、拯救、保护”（Explore, Rescue, Protect）。基地为章鱼堡。\n"
            "1. 巴克队长（Barnacles）：北极熊，小纵队领袖，沉着冷静，驾驶蓝鲸艇A。\n"
            "2. 呱唧（Kwazii）：海盗猫，勇敢甚至有点莽撞，驾驶剑鱼艇B。\n"
            "3. 皮医生（Peso）：企鹅军医，善良胆小但面对受伤海洋生物时无比勇敢，携带急救箱。\n"
            "4. 谢灵通（Shellington）：海獭生物学家，知识渊博，容易惊叹“哎呀我的天！”。"
        ),
    ),
    "超级飞侠": ChildIpCanon(
        canonical_name="超级飞侠",
        aliases=("超级飞侠", "乐迪", "多多", "小爱", "super wings"),
        canon_summary=(
            "《超级飞侠》（Super Wings）官方角色设定：\n"
            "1. 乐迪（Jett）：红白相间喷气式飞机，任务是准时向全球小朋友派送包裹，口号：“我是乐迪，每时每刻准时送达！”\n"
            "2. 多多（Donnie）：黄色工程救援机，配备丰富工具箱，擅长维修建造，口号：“我随时准备好动手！”\n"
            "3. 小爱（Dizzy）：粉色救援直升机，擅长急救与绳索搜救，口号：“我是小爱，救援行动开始！”。"
        ),
    ),
    "熊出没": ChildIpCanon(
        canonical_name="熊出没",
        aliases=("熊出没", "熊大", "熊二", "光头强"),
        canon_summary=(
            "《熊出没》主要角色与关系设定：\n"
            "1. 熊大：沉稳聪明的棕熊，狗熊岭的主心骨，善于出谋划策保护森林。\n"
            "2. 熊二：憨厚可爱的黄熊，性格贪吃爱吃蜂蜜，口号：“熊大，等等俺！”\n"
            "3. 光头强：从前伐木，现已转型为狗熊岭热情负责的导游，与熊大熊二成为并肩守护大自然的好伙伴。"
        ),
    ),
    "喜羊羊": ChildIpCanon(
        canonical_name="喜羊羊与灰太狼",
        aliases=("喜羊羊", "喜羊羊与灰太狼", "灰太狼", "美羊羊", "懒羊羊"),
        canon_summary=(
            "《喜羊羊与灰太狼》主要角色设定：\n"
            "1. 喜羊羊：青青草原上最聪明的羊，跑步极快，擅长用智慧化解危机。\n"
            "2. 懒羊羊：贪吃爱睡但福气满满、关键时刻讲义气的小羊。\n"
            "3. 灰太狼：极具毅力的发明家，虽然屡遭波折但始终深爱家庭，口号：“我一定会回来的！”与羊村共创和平。"
        ),
    ),
    "孙悟空": ChildIpCanon(
        canonical_name="西游记",
        aliases=("孙悟空", "齐天大圣", "美猴王", "西游记", "猪八戒", "唐僧", "沙僧"),
        canon_summary=(
            "《西游记》齐天大圣孙悟空官方设定：\n"
            "由仙石孕育而生，拜菩提祖师学得七十二变与筋斗云，手持如意金箍棒，火眼金睛识破妖魔。\n"
            "一路保护师父唐僧西天取经，性格机智勇敢、不畏艰险，降妖伏魔保佑正义。"
        ),
    ),
    "哪吒": ChildIpCanon(
        canonical_name="哪吒闹海",
        aliases=("哪吒", "三太子", "李哪吒", "哪吒传奇"),
        canon_summary=(
            "《哪吒闹海》/《哪吒传奇》少年英雄哪吒官方设定：\n"
            "手持乾坤圈，身披混天绫，脚踏风火轮，配火尖枪，英勇无畏、惩恶扬善、孝顺重情义的小英雄。"
        ),
    ),
    "白雪公主": ChildIpCanon(
        canonical_name="白雪公主",
        aliases=("白雪公主", "七个小矮人", "白雪公主与七个小矮人"),
        canon_summary=(
            "《白雪公主》经典童话官方设定：\n"
            "美丽纯洁善良的白雪公主，在神秘大森林里与七个性格各异但忠诚友爱的小矮人生活，最终战胜邪恶皇后，迎来幸福。"
        ),
    ),
}

# ---------------------------------------------------------------------------
# 3. Query Analysis & IP Extraction
# ---------------------------------------------------------------------------

_STORY_VERBS = ("讲", "听", "说", "念", "放", "播", "看", "来")
_STORY_NOUNS = ("故事", "童话", "绘本", "寓言")

_GENERIC_BEDTIME_KEYWORDS = frozenset(
    {
        "睡前故事",
        "讲个故事",
        "讲一个故事",
        "给我讲个故事",
        "我想听故事",
        "我想听睡前故事",
        "讲睡前故事",
        "放睡前故事",
        "听睡前故事",
        "放个故事",
        "来个故事",
        "说个故事",
        "讲小故事",
        "讲个小故事",
        "讲一个小故事",
    }
)

_CONTINUATION_PATTERNS = (
    "再讲一个",
    "还想听一个",
    "再来一个",
    "换一个故事",
    "下一个故事",
    "另外一个故事",
    "继续讲",
    "再讲一篇",
    "换一个",
)

_EXCLUDE_IP_WORDS = frozenset(
    {
        "睡前",
        "好听",
        "好听的",
        "有趣",
        "有趣的",
        "精彩",
        "精彩的",
        "经典",
        "经典的",
        "童话",
        "一个",
        "一个新",
        "一个新的",
        "一个小",
        "新的",
        "个",
        "个小",
        "个新",
        "个短",
        "个大",
        "另外",
        "另外一个",
        "另一个",
        "别的",
        "别讲",
        "不要",
        "不听",
        "什么",
        "哪个",
        "那",
        "这",
        "这个",
        "那个",
        "短",
        "短的",
        "长的",
        "小",
        "大",
        "短篇",
        "长篇",
        "故事",
        "小故事",
        "大故事",
        "短故事",
    }
)

_GENERIC_THEMES_AND_SETTINGS = frozenset(
    {
        "森林",
        "森林里",
        "大森林",
        "树林",
        "丛林",
        "草地",
        "草原",
        "花园",
        "小河",
        "池塘",
        "大海",
        "大海里",
        "海洋",
        "海洋里",
        "太空",
        "宇宙",
        "星球",
        "天空",
        "白云",
        "城堡",
        "城堡里",
        "农场",
        "学校",
        "家里",
        "城市",
        "王国",
        "小镇",
        "村庄",
        "动物",
        "小动物",
        "昆虫",
        "恐龙",
        "小鸟",
        "鱼儿",
        "小鱼",
        "小兔",
        "小兔子",
        "小狗",
        "小猫",
        "小熊",
        "大熊",
        "小猴",
        "小猴子",
        "友谊",
        "朋友",
        "小伙伴",
        "冒险",
        "探险",
        "魔法",
        "奇幻",
        "勇敢",
        "诚实",
        "善良",
        "爱心",
        "成长",
        "暖心",
        "温馨",
        "治愈",
        "好玩",
        "好笑",
        "搞笑",
        "幽默",
        "科幻",
        "益智",
        "白天",
        "晚上",
        "夜晚",
        "睡前",
        "阳光",
        "月夜",
        "星空",
        "春天",
        "夏天",
        "秋天",
        "冬天",
        "四季",
        "自然",
        "大自然",
        "寓言",
        "成语",
        "神话",
        "民间",
        "童话",
        "绘本",
        "生活",
        "日常",
    }
)

_GENERIC_STORY_CHARS = frozenset("一个新小大短长好听有趣精彩经典睡前故事童话绘本篇部首的另外别什么这那")


def _normalize(text: str) -> str:
    return re.sub(r"[\s，,。！？!?；;：:、\"'“”‘’（）()]+", "", text).lower()


def is_story_request(query: str) -> bool:
    """Check if the user turn requests a story or bedtime narration."""
    compact = _normalize(query)
    if not compact:
        return False
    if any(p in compact for p in _CONTINUATION_PATTERNS):
        return True
    if any(n in compact for n in _STORY_NOUNS):
        return any(v in compact for v in _STORY_VERBS) or "我想听" in compact or "想听" in compact
    return False


def is_continuation_story_request(query: str) -> bool:
    """Check if user requests another / next story (e.g. 再讲一个, 换一个)."""
    compact = _normalize(query)
    return any(pattern in compact for pattern in _CONTINUATION_PATTERNS)


def match_local_story(query: str, *, prioritize_generic: bool = True) -> str | None:
    """Match request to a pre-stored local Flash/SD story ID.

    Priority:
    1. Explicit alias match (e.g. "小星星的枕头", "月亮不着急", "大熊和森林里的悄悄话", "夜晚安抚").
    2. If prioritize_generic is True and query is a generic bedtime request without
       naming an external IP, returns "story_01" (the primary bedtime story).
    """
    compact = _normalize(query)
    if not compact:
        return None

    # Check explicit alias match first across all local stories
    for story_id, info in LOCAL_STORIES.items():
        for alias in info.aliases:
            if _normalize(alias) in compact:
                return story_id

    # If generic bedtime story requested and no external IP named:
    if prioritize_generic:
        ip = extract_story_ip(query)
        if ip is None and any(kw in compact for kw in _GENERIC_BEDTIME_KEYWORDS):
            return "story_01"

    return None


def extract_story_ip(query: str) -> str | None:
    """Extract animated or story IP name from user turn (e.g. 汪汪队, 小猪佩奇)."""
    compact = _normalize(query)
    if not compact:
        return None

    # 1. Check known children IPs first
    for key, canon in KNOWN_CHILD_IPS.items():
        if any(_normalize(alias) in compact for alias in canon.aliases):
            return key

    # 2. Check regex patterns for generic "[IP]的故事" or "我想听[IP]"
    patterns = (
        r"(?:想听|要听|讲一个|讲个|讲|听|说|放|播)(?P<ip>[一-龥a-zA-Z0-9]{2,10}?)(?:的)?(?:睡前)?(?:故事|童话|绘本)",
        r"(?:有没有|换成)(?P<ip>[一-龥a-zA-Z0-9]{2,10}?)(?:的)?(?:故事|童话)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, query):
            candidate = match.group("ip").strip()
            norm_cand = _normalize(candidate)
            # Strip leading classifiers / quantifiers if captured by loose boundaries
            for pfx in ("另外一个", "另一个", "换一个", "一个新的", "一个新", "一个小", "一个", "个小", "个新", "个短", "个大", "个", "关于"):
                if norm_cand.startswith(pfx):
                    norm_cand = norm_cand[len(pfx):]
                    break
            for sfx in ("的", "小", "新", "短"):
                if norm_cand.endswith(sfx) and len(norm_cand) > len(sfx) + 1:
                    norm_cand = norm_cand[:-len(sfx)]
                    break

            if (
                len(norm_cand) < 2
                or norm_cand in _EXCLUDE_IP_WORDS
                or norm_cand in _GENERIC_THEMES_AND_SETTINGS
            ):
                continue
            if (norm_cand.endswith("里") or norm_cand.endswith("中")) and len(norm_cand) > 2:
                if norm_cand[:-1] in _GENERIC_THEMES_AND_SETTINGS:
                    continue
            if set(norm_cand).issubset(_GENERIC_STORY_CHARS):
                continue
            # Ensure it is not one of our local stories or an alias substring
            if any(
                norm_cand in _normalize(alias) or _normalize(alias) in norm_cand
                for s in LOCAL_STORIES.values()
                for alias in s.aliases
            ):
                continue
            return norm_cand

    return None


# ---------------------------------------------------------------------------
# 4. Search Query & Grounded Prompt Assembly
# ---------------------------------------------------------------------------


def build_ip_story_search_query(ip: str) -> str:
    """Build a search engine query targeting authoritative plot & character canon."""
    canon = KNOWN_CHILD_IPS.get(ip)
    canonical_name = canon.canonical_name if canon is not None else ip
    return f"{canonical_name} 剧情概要 角色设定 主要故事"


def build_grounded_story_system_instruction(ip: str, canon_summary: str) -> str:
    """Assemble anti-hallucination system prompt block enforcing canon adherence."""
    canon = KNOWN_CHILD_IPS.get(ip)
    canonical_name = canon.canonical_name if canon is not None else ip
    summary_text = canon_summary.strip() or (canon.canon_summary if canon else "")

    return (
        f"【权威原著设定依据】用户请求听《{canonical_name}》的故事。\n"
        f"以下是通过网络检索与官方资料核实的剧情概要与角色设定：\n"
        f"{summary_text}\n\n"
        "【创作要求（严禁瞎编）】\n"
        f"1. 必须严格遵守上述《{canonical_name}》的官方角色设定、职责、载具装备及标志性口号，严禁张冠李戴、胡乱捏造。\n"
        "2. 讲述一个温暖有趣的完整小故事：有开头、有经过、有结尾，叙事节奏舒缓，适合儿童睡前倾听。\n"
        "3. 篇幅充实（至少六句话），语言充满画面感与亲和力，不要出现暴力、恐吓或负面导向内容。\n"
        "4. 故事结尾以温馨入睡或晚安问候收尾，问孩子喜不喜欢或者想不想闭上眼睛睡觉。"
    )


def build_local_story_prompt_instruction(story_id: str) -> str:
    """Build guidance for narrating a local pre-stored story."""
    info = LOCAL_STORIES.get(story_id)
    if info is None:
        info = LOCAL_STORIES["story_01"]
    return (
        f"【本地睡前故事精选】请为孩子温润讲述经典故事《{info.title}》。\n"
        f"故事概要：{info.synopsis}\n"
        "要求：语气温柔舒缓，每句生动清晰，陪伴孩子放松身心进入甜美梦乡。"
    )


# ---------------------------------------------------------------------------
# 5. Volume Voice Control
# ---------------------------------------------------------------------------

VOLUME_UP_PATTERNS: Final[tuple[str, ...]] = (
    "声音大一点",
    "声音调大",
    "声音放大",
    "声音大点",
    "大声一点",
    "大声点",
    "大一点声",
    "放大声",
    "放大一点声音",
    "音量大一点",
    "音量调大",
    "音量大点",
    "把声音调大",
    "把音量调大",
    "声音再大一点",
    "音量再大一点",
)

VOLUME_DOWN_PATTERNS: Final[tuple[str, ...]] = (
    "声音小一点",
    "声音调小",
    "声音放小",
    "声音小点",
    "小声一点",
    "小声点",
    "小一点声",
    "放小声",
    "放小一点声音",
    "音量小一点",
    "音量调小",
    "音量小点",
    "把声音调小",
    "把音量调小",
    "太吵了声音小点",
    "太吵了小声点",
    "声音再小一点",
    "音量再小一点",
)

def _names_volume(compact: str, pattern: str) -> bool:
    start = compact.find(pattern)
    if start < 0:
        return False
    window = compact[max(0, start - 4) : start]
    return "声音" in window or "音量" in window or pattern.startswith(("声音", "音量", "大声", "小声"))


DEFAULT_VOLUME_STEP: Final[int] = 15
VOLUME_UP_REPLY: Final[str] = "好的，声音调大了一点。"
VOLUME_DOWN_REPLY: Final[str] = "好的，声音调小了一点。"


def is_volume_command(query: str) -> tuple[bool, int, str | None]:
    """Parse volume adjustment command.

    Returns:
        (is_volume, delta_loudness, direct_reply)
        delta_loudness is +15 for louder, -15 for softer.
    """
    compact = _normalize(query)
    if not compact:
        return False, 0, None

    # Match only where the words name the volume. A bare "调低一点" also describes pitch and pace,
    # and a substring match would move the volume when the child asked for something else.
    for pattern in VOLUME_UP_PATTERNS:
        if _names_volume(compact, _normalize(pattern)):
            return True, DEFAULT_VOLUME_STEP, VOLUME_UP_REPLY

    for pattern in VOLUME_DOWN_PATTERNS:
        if _names_volume(compact, _normalize(pattern)):
            return True, -DEFAULT_VOLUME_STEP, VOLUME_DOWN_REPLY

    return False, 0, None


_POLITE_AFFIXES = re.compile(r"^(?:请|帮我|请帮我|给我|麻烦)+|(?:啊|吧|呀|哦|啦|一下)+$")


def is_pure_volume_command(query: str) -> tuple[bool, int, str | None]:
    """Check if the user turn is purely a volume command without other intents."""
    is_vol, delta, reply = is_volume_command(query)
    if not is_vol:
        return False, 0, None

    cleaned = _normalize(query)
    cleaned = _POLITE_AFFIXES.sub("", cleaned)
    # Check if cleaned is primarily one of the volume patterns
    for p in VOLUME_UP_PATTERNS + VOLUME_DOWN_PATTERNS:
        norm_p = _normalize(p)
        if cleaned == norm_p or cleaned.removesuffix("点") == norm_p.removesuffix("点"):
            return True, delta, reply

    return False, 0, None
