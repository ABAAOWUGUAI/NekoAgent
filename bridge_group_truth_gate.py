"""Group final truth gate, reply-plan enforcement and persona cadence budget.

The 2026-08-08 group defects were: media/sensory claims without evidence,
echoing unverified facts, target/topic mismatch, ``degraded`` results still
being sent, and mechanical signature-token overuse.  This module owns the
deterministic, server-side checks that decide whether a draft may be sent
(B3/B4) and how the signature budget is counted from confirmed projections
only (B5).  A model may reword a draft, but it may never upgrade a media
state or a claim type here.

Execution order used by the caller:

    raw draft
     -> normalize exact delivery text
     -> target/topic consistency check
     -> grounding/fact check
     -> safety and anti-sycophancy check
     -> persona cadence check
     -> passed: send
     -> failed: one controlled rewrite preserving evidence
     -> still failed: block/silent, record reason, do not enqueue Delivery
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime

from bridge_social_reply import group_reply_style_issues

# Stable issue taxonomy from the repair command (B4).
ISSUE_MEDIA_CLAIM_WITHOUT_EVIDENCE = "media_claim_without_evidence"
ISSUE_FABRICATED_PERSONAL_EXPERIENCE = "fabricated_personal_experience"
ISSUE_UNSUPPORTED_SPECIFIC_FACT = "unsupported_specific_fact"
ISSUE_REPLY_TARGET_MISMATCH = "reply_target_mismatch"
ISSUE_TOPIC_ANCHOR_MISMATCH = "topic_anchor_mismatch"
ISSUE_SYCOPHANTIC_AGREEMENT = "sycophantic_agreement_without_basis"
ISSUE_SIGNATURE_OVERUSE = "persona_signature_overuse"
ISSUE_RESEARCH_EVIDENCE_MISSING = "research_evidence_missing"
ISSUE_RESEARCH_UNVERIFIED = "research_fact_unverified"
ISSUE_UNJUSTIFIED_HOSTILITY = "unjustified_hostility"
ISSUE_IDENTITY_EVASION = "identity_evasion"
ISSUE_RELATIONSHIP_OVERREACH = "relationship_overreach"
ISSUE_CURRENT_DAYPART_MISMATCH = "current_daypart_mismatch"

_CRISIS_SELF = re.compile(r"(?:我|自己).{0,15}(?:自杀|自残|割腕|轻生|不想活|想死|想跳楼)|(?:想|准备|打算)(?:自杀|自残|割腕|跳楼)")
_CRISIS_FICTION = re.compile(r"游戏里|游戏中|小说里|电影里|角色台词|这句台词")
_CRISIS_MOCKERY = re.compile(r"演这|戏精|整活|矫情|装什么|闹呢|哈哈|笑死|活该|去死|心虚|喵|略[～~]?")
_CRISIS_SUPPORT = re.compile(r"别伤害|不要伤害|先别|陪[着你]|找.{0,12}(?:人|朋友|家人)|安全|急救|求助|救护|撑不住|很难受|担心|我在|愿意听|现在.{0,8}(?:哪|谁|一个人)")
_MEDICAL_SELF_DISCLOSURE = re.compile(
    r"(?:^|[，。！？!?\n])\s*(?:我|本人|自己)[^。！？!?\n]{0,32}"
    r"(?:有|患|得了|确诊|被诊断)[^。！？!?\n]{0,18}"
    r"(?:抑郁症|焦虑症|双相情感障碍|躁郁症|精神分裂症|自闭症|ADHD|心理疾病|精神疾病)"
)
_MEDICAL_MOCKERY = re.compile(
    r"病历本[^。！？!?\n]{0,28}(?:板砖|砸人|笑料)"
    r"|(?:buff|boss)[^。！？!?\n]{0,28}(?:叠|厚|病|症)"
    r"|(?:病|症)[^。！？!?\n]{0,14}(?:笑死|乐子|整活|戏精|矫情)",
    re.IGNORECASE,
)
_DENIAL = re.compile(r"我没有|我没[做说想]|不是我|不是这个意思|别再猜|不要猜|你(?:误会|猜错|想多)|别瞎说|没有这回事")
_INSINUATION = re.compile(r"心虚|嘴硬|破防|急了|装傻|装无辜|不敢承认|被我说中")
_SLUR = re.compile(r"唐氏|脑瘫|弱智|智障")
_SLUR_REJECTION = re.compile(r"(?:别|不要|不该|不能).{0,10}(?:拿|用).{0,16}(?:骂|攻击|嘲笑|称呼)|(?:歧视|污名化).{0,5}(?:不对|不好|不合适)")


_DAYPART_CURRENT = re.compile(r"大白天的?(?=别|还|就|都|你|我|这|那|怎么|居然|竟然|不|也|[，。！？!?]|$)")
_DAYPART_QUOTED = re.compile(r'“[^”]*大白天[^”]*”|「[^」]*大白天[^」]*」|"[^"]*大白天[^"]*"')
_DAYPART_OTHER_TIME = re.compile(r"昨天|前天|那天|当时|上次|以前|曾经|明天|后天|未来|将来")


def _current_daypart_mismatch(reply: str) -> bool:
    """Only correct an unquoted, current-sounding daytime claim at night."""
    text = str(reply or "")
    if not _DAYPART_CURRENT.search(text) or _DAYPART_QUOTED.search(text) or _DAYPART_OTHER_TIME.search(text):
        return False
    hour = datetime.now().astimezone().hour
    return hour >= 20 or hour < 6


def _echo_key(value: object) -> str:
    return re.sub(r"[\s，。！？!?、~～]", "", str(value or ""))


def group_reply_quality_context(current: Mapping, history: Sequence[Mapping]) -> dict:
    """Derive body-free facts from readable, same-group, recent source rows.

    Never use a different member's denial as the addressed member's boundary.
    Missing actor/time evidence does not authorize a chorus exception.
    """
    text = str(current.get("content") or "")
    actor, group = str(current.get("sender_id") or ""), str(current.get("group_id") or "")
    same_actor = [text]
    echo_actors = {actor} if actor and actor != "bot" else set()
    try:
        now = datetime.fromisoformat(str(current.get("created_at") or "").replace("Z", "+00:00"))
    except ValueError:
        now = None
    for item in list(history)[-20:]:
        if not isinstance(item, Mapping) or item.get("body_redacted_at") or str(item.get("group_id") or "") != group:
            continue
        try:
            elapsed = (now - datetime.fromisoformat(str(item.get("created_at") or "").replace("Z", "+00:00"))).total_seconds()
        except (TypeError, ValueError):
            continue
        if not 0 <= elapsed <= 180:
            continue
        other = str(item.get("sender_id") or "")
        body = str(item.get("content") or "")
        if other and other == actor:
            same_actor.append(body)
        if other and other != "bot" and _echo_key(body) == _echo_key(text):
            echo_actors.add(other)
    return {
        "anchor_message_id": int(current.get("id") or 0),
        "same_actor_denied": any(_DENIAL.search(body) for body in same_actor),
        "crisis_context": any(_CRISIS_SELF.search(body) and not _CRISIS_FICTION.search(body) for body in same_actor),
        "chorus_supported": len(echo_actors) >= 2,
    }


def group_contextual_quality_issues(request_text: str, reply: str, context: Mapping | None = None) -> list[str]:
    """Narrow, contextual risk checks, not a general joke or sentiment ban."""
    facts = context if isinstance(context, Mapping) else {}
    request, text = str(request_text or ""), str(reply or "")
    if not text.strip():
        return []
    issues = []
    crisis = bool(facts.get("crisis_context") or (_CRISIS_SELF.search(request) and not _CRISIS_FICTION.search(request)))
    if crisis and (_CRISIS_MOCKERY.search(text) or not _CRISIS_SUPPORT.search(text)):
        issues.append("crisis_response_inappropriate")
    # A first-person condition report is not necessarily a self-harm crisis,
    # but it still must not become the target of illness-based ridicule.
    if (_MEDICAL_SELF_DISCLOSURE.search(request)
            and not _CRISIS_FICTION.search(request)
            and _MEDICAL_MOCKERY.search(text)):
        issues.append("medical_disclosure_mockery")
    denied = bool(facts.get("same_actor_denied") or _DENIAL.search(request))
    if denied:
        for sentence in re.split(r"[。！？!?；;\n]", text):
            if _INSINUATION.search(sentence) and not re.search(r"(?:不是|没有|不该|别).{0,5}(?:心虚|嘴硬|破防|猜|揣测)|我.{0,6}(?:猜错|误会|误解)", sentence):
                issues.append("unsupported_insinuation_after_denial")
                break
    if _SLUR.search(text) and not _SLUR_REJECTION.search(text):
        medical = bool(re.search(r"唐氏综合征|医学|染色体|神经系统", text)) and not re.search(r"你|这群|一群|开会|活该", text)
        if not medical:
            issues.append("stigmatizing_echo")
    if len(_echo_key(request)) >= 5 and _echo_key(text) == _echo_key(request) and not facts.get("chorus_supported"):
        issues.append("ungrounded_verbatim_echo")
    return issues

# Signature tokens may be removed; an explicit current-daypart mismatch may
# be corrected from the trusted local clock. Every other evidence defect stays
# terminal and cannot become sendable by merely changing the wording.
SOFT_REWRITE_ISSUES = frozenset({
    ISSUE_SIGNATURE_OVERUSE,
    ISSUE_CURRENT_DAYPART_MISMATCH,
})

SEND_STATE_PASSED = "passed"
SEND_STATE_REWRITTEN_PASSED = "rewritten_passed"
SEND_STATE_BLOCKED = "blocked"
# Historical compatibility value that must never be treated as sendable.
SEND_STATE_DEGRADED = "degraded"

# Closed set of signature tokens (B5).  Server-configurable bounds live in
# ``signature_budget_limits``; the default is 3/10 (~30% meow ratio, leaving
# headroom under the 35% owner cap) and max 1 consecutive meow ending.
SIGNATURE_TOKENS = ("喵", "～")
MEOW_TOKEN = "喵"
DEFAULT_SIGNATURE_BUDGET_WINDOW = 10
DEFAULT_SIGNATURE_BUDGET_MAX = 3
DEFAULT_SIGNATURE_BUDGET_MAX_CONSECUTIVE = 1
SIGNATURE_MIN_WINDOW = 4
SIGNATURE_MAX_WINDOW = 40
SIGNATURE_MIN_MAX = 0
SIGNATURE_MAX_MAX = 10
SIGNATURE_MIN_CONSECUTIVE = 1
SIGNATURE_MAX_CONSECUTIVE = 5

# Serious-category replies default to no meow token regardless of window
# ratio: identity clarification, error/diagnostic, factual negative, refusal,
# capability boundary, and strong personal boundary.  These read as "trying
# hard to stay cute" when a 喵 is bolted on, which is exactly the template feel
# the owner rejected.  The reply is still allowed — just without the token.
_SERIOUS_REPLY_PATTERNS = (
    re.compile(r"(?:不是真人|不是人|AI助手|我是(?:个)?助手|我没有|我没法|无法|做不到|不能直接|暂时没有|没有符合|请检查|网络代理|连接异常|异常|抱歉|不哄|认真|不答应|不同意|拒绝|说不|不行)", re.IGNORECASE),
    re.compile(r"(?:澄清|纠正|更正|说明一下|解释一下|意思是|我是说)"),
)

# The outgoing wording alone cannot identify an earnest incoming request: a
# short practical answer can contain none of the phrases above.  Keep this
# narrow to a member's own difficulty or an explicit request to stop joking;
# third-person discussion and ordinary banter must not consume the category.
_SERIOUS_INBOUND_PATTERNS = (
    re.compile(r"(?:^|[，。！？!?])\s*(?:我|本人)[^。！？!?]{0,24}(?:受伤|生病|不舒服|难受|疼|焦虑|撑不住|求助|需要帮助)"),
    re.compile(r"(?:^|[，。！？!?])\s*(?:认真(?:问|说)(?:一下)?|别(?:再)?(?:开玩笑|闹|怼))"),
)


def normalize_signature_budget(value: Mapping | None) -> dict:
    value = value if isinstance(value, Mapping) else {}
    window = value.get("window") if isinstance(value, Mapping) else None
    max_tokens = value.get("max_tokens") if isinstance(value, Mapping) else None
    max_consecutive = value.get("max_consecutive") if isinstance(value, Mapping) else None
    try:
        window = int(window)
    except (TypeError, ValueError):
        window = DEFAULT_SIGNATURE_BUDGET_WINDOW
    try:
        max_tokens = int(max_tokens)
    except (TypeError, ValueError):
        max_tokens = DEFAULT_SIGNATURE_BUDGET_MAX
    try:
        max_consecutive = int(max_consecutive)
    except (TypeError, ValueError):
        max_consecutive = DEFAULT_SIGNATURE_BUDGET_MAX_CONSECUTIVE
    return {
        "window": max(SIGNATURE_MIN_WINDOW, min(window, SIGNATURE_MAX_WINDOW)),
        "max_tokens": max(SIGNATURE_MIN_MAX, min(max_tokens, SIGNATURE_MAX_MAX)),
        "max_consecutive": max(
            SIGNATURE_MIN_CONSECUTIVE,
            min(max_consecutive, SIGNATURE_MAX_CONSECUTIVE),
        ),
    }


def _has_signature_token(text: str) -> bool:
    value = str(text or "").strip()
    return any(token in value for token in SIGNATURE_TOKENS)


def _has_meow_token(text: str) -> bool:
    return MEOW_TOKEN in str(text or "")


def _is_serious_reply(draft: str) -> bool:
    return any(pattern.search(str(draft or "")) for pattern in _SERIOUS_REPLY_PATTERNS)


def _is_serious_inbound(request_text: str) -> bool:
    return any(pattern.search(str(request_text or "")) for pattern in _SERIOUS_INBOUND_PATTERNS)


def signature_budget_issues(
    draft: str,
    recent_confirmed: Sequence[Mapping],
    *,
    budget: Mapping | None = None,
    request_text: str = "",
) -> list[str]:
    """Return overuse issues for a draft against confirmed projections only.

    ``recent_confirmed`` is the rolling window of *confirmed* assistant group
    projections (post-ACK), never model drafts or failed deliveries.

    Two independent gates (owner directive, 2026-08-09):

    - cadence: the ratio of replies carrying the meow token within the window
      must stay within ``max_tokens`` (default 3/10 = 30%, under the 35% cap),
      and consecutive meow endings must not exceed ``max_consecutive``.
    - category: clarification / error / factual-negative / refusal / boundary
      replies default to *no* meow token regardless of the window ratio.
    """

    limits = normalize_signature_budget(budget)
    issues: list[str] = []
    if not _has_signature_token(draft):
        return issues
    if _is_serious_reply(draft) or _is_serious_inbound(request_text):
        issues.append(ISSUE_SIGNATURE_OVERUSE)
        return issues
    window = int(limits["window"])
    # complete_group_dispatch supplies confirmed replies newest first.  Taking
    # the tail and reversing it treated the oldest reply as the latest one.
    recent = [item for item in recent_confirmed if isinstance(item, Mapping)][:window]
    used = sum(1 for item in recent if _has_meow_token(str(item.get("content") or "")))
    if used >= int(limits["max_tokens"]):
        issues.append(ISSUE_SIGNATURE_OVERUSE)
    consecutive = 0
    for item in recent:
        if _has_meow_token(str(item.get("content") or "")):
            consecutive += 1
            if consecutive >= int(limits["max_consecutive"]):
                issues.append(ISSUE_SIGNATURE_OVERUSE)
                break
        else:
            break
    return issues


def group_final_send_state(gate_value: object) -> str:
    """Map a historical ``group_style_gate`` value to a final send decision.

    ``degraded`` is no longer a soft warning; it is blocked.  ``not_applicable``
    and empty mean the style gate does not apply to this path (control/work/
    non-group), so a genuine reply is not blocked by a missing style value.
    """

    value = str(gate_value or "").strip().lower()
    if value in {SEND_STATE_PASSED, SEND_STATE_REWRITTEN_PASSED}:
        return value
    if value == SEND_STATE_DEGRADED:
        return SEND_STATE_BLOCKED
    if value == SEND_STATE_BLOCKED:
        return SEND_STATE_BLOCKED
    if value in {"provider_failed", "not_applicable", ""}:
        return SEND_STATE_PASSED if value != "provider_failed" else SEND_STATE_BLOCKED
    # Unknown style state: fail closed to blocked rather than silently sending.
    return SEND_STATE_BLOCKED


_MEDIA_CLAIM_PATTERNS = (
    re.compile(r"(?:这个|这张|这图|图中|图片|画面|杯子|玩偶|立牌|手办).{0,20}(?:是|像|颜色|造型|立体|材质)", re.IGNORECASE),
    re.compile(r"(?:我看到|看起来|看上去|颜色是|造型是)", re.IGNORECASE),
)
# Visual-experience claims that describe having *looked at* media.  These are
# only checked while observation is not ``ready``; a factual visual statement
# requires an observation.  Meta statements about media transport ("你们发了
# 不少媒体内容") are intentionally not matched here.
_VISUAL_EXPERIENCE_PATTERNS = (
    re.compile(r"(?:刚看|刚看到|刚才看到|看过).{0,8}(?:图|图片|照片|画面|视频|动画|图吧|刷图)", re.IGNORECASE),
    re.compile(r"(?:看到|看见了|看见|看到了).{0,8}(?:刷图|发图|发图片|发照片|晒图|发视频|动图)", re.IGNORECASE),
    re.compile(r"(?:这张|这图|那图|图片|截图).{0,8}(?:我)?(?:看了|看过|看过了)", re.IGNORECASE),
)
_SENSORY_CLAIM_PATTERNS = (
    re.compile(r"(?:我听到|听过|在循环|播放的是|这首歌是|声音.{0,6}(?:是|像))", re.IGNORECASE),
)
_EXPERIENCE_CLAIM_PATTERNS = (
    re.compile(r"(?:我研究过|我记得|我执行过|我们之前|我之前)", re.IGNORECASE),
)
# An ambiguous Reply/mention target means the system has no factual object on
# which to take a position.  "I think it makes sense" is still an unsupported
# judgement even if the same sentence subsequently asks for details.  These
# patterns intentionally cover both endorsement and rejection: guessing that
# an unseen decision is bad is no more grounded than guessing that it is good.
_AMBIGUOUS_TARGET_JUDGEMENT_PATTERNS = (
    re.compile(
        r"(?:我(?:觉得|认为|看)|逻辑上|整体上|感觉上).{0,12}"
        r"(?:说得通|合理|不合理|可行|不可行|有道理|没问题|值得|不值得|"
        r"正确|不对|支持|反对|赞同|同意)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:这(?:个|项|件|种).{0,16})"
        r"(?:说得通|合理|不合理|可行|不可行|有道理|没问题|值得|不值得|"
        r"正确|不对|支持|反对|赞同|同意)",
        re.IGNORECASE,
    ),
)
# Direct questions with no concrete target need a structural response contract,
# not a growing dictionary of agreement words.  The *leading clause* must ask
# which object, decision or background is meant before the assistant can take
# any position.  Asking only which part worries the user leaves the unknown
# object unresolved and is not an honest clarification.
_AMBIGUOUS_TARGET_OBJECT_CLARIFICATION_PATTERNS = (
    re.compile(r"(?:你|您).{0,10}(?:指|说|问).{0,10}(?:哪|什么|具体)", re.IGNORECASE),
    re.compile(r"(?:哪(?:个|项|一条|件)|什么).{0,8}(?:决定|方案|事情|问题|部分|内容)", re.IGNORECASE),
    re.compile(r"(?:补充|提供|说(?:一)?下|讲(?:一)?下|说明).{0,10}(?:背景|上下文|前提|具体|选项)", re.IGNORECASE),
    re.compile(r"(?:先|得先|需要先|我得).{0,14}(?:知道|弄清|确认|说明).{0,14}(?:哪|什么|背景|上下文|前提|具体)", re.IGNORECASE),
)
_DIRECTED_REPLY_ATTENTIONS = frozenset({"reply_to_assistant", "explicit_mention"})
_DIRECTED_MEDIA_BOUNDARY = "我现在没有可确认的媒体内容；你想问哪一部分？"
_DIRECTED_FACT_BOUNDARY = "这件事我现在没有足够依据判断；你能给我具体信息吗？"
_DIRECTED_TARGET_BOUNDARY = "我还不确定你指的是哪一条；能具体说一下吗？"
_DIRECTED_FAILURE_BOUNDARY = "刚才这句我没接住，你再发一次？"
_DIRECTED_RESEARCH_BOUNDARY = "这个公开事实我现在没有拿到可引用的资料，先不把它当结论说。"
_SPECIFIC_FACT_PATTERNS = (
    re.compile(r"\d+%|百分之[\d一二三四五六七八九十百]+\d*|¥|￥|\$\s?\d+|\d+\s*(?:元|块|万|亿|倍|家|条)"),
    re.compile(r"(?:违法|犯法|稳赚|必赔|板上钉钉|铁定|绝对会)", re.IGNORECASE),
)

_HOSTILE_REPLY = re.compile(
    r"(?:把你|给你|将你).{0,8}(?:禁言|踢(?:出)?|挂路灯|收拾|弄死)|"
    r"(?:该|得|去).{0,3}吃药|舌头.{0,8}(?:捋不直|说不清)|"
    r"答题卡.{0,12}(?:脚踩|脚写)|"
    r"(?:脑子|智商).{0,8}(?:坏了|修好|有问题)|"
    r"(?:闭嘴|滚开|废物|蠢货|脑残)",
    re.IGNORECASE,
)
_TARGETED_SHARP_REPLY = re.compile(
    r"(?:^|[，。！？\s])就这[？?!！]?|你这(?:步|操作|题).{0,12}(?:点反|写反|绕远|离谱)",
    re.IGNORECASE,
)
_BANTER_INVITATION = re.compile(r"(?:互怼|来怼我|怼我|吐槽我|开玩笑|逗你|调侃|哈哈)")
_BANTER_STOP = re.compile(r"(?:别怼|别说|别闹|够了|不想聊|不舒服|难受|生病|认真说|求助)")
_IDENTITY_QUESTION = re.compile(r"(?:你是谁|你到底是什么(?:人|东西)?|你是(?:不?是)?\s*AI|什么助手|真人吗)", re.IGNORECASE)
_IDENTITY_TRUTH = re.compile(r"(?:虚拟助手|AI\s*助手|人工智能|不是(?:现实中的)?真人|这里的助手)", re.IGNORECASE)
_RELATIONSHIP_REQUEST = re.compile(r"(?:做|当|是)(?:我|俺)的?(?:老婆|老公|女朋友|男朋友|对象|恋人)", re.IGNORECASE)
_RELATIONSHIP_ACCEPT = re.compile(r"(?:我就是|我是|行啊|可以啊|好啊).{0,8}(?:你(?:的)?|当你)?(?:老婆|老公|女朋友|男朋友|对象|恋人)", re.IGNORECASE)


def _first_boundary_reply_not_recent(result: Mapping, candidates: Sequence[str]) -> str:
    """Pick a bounded safe reply without repeating a recent confirmed one."""

    recent = {
        re.sub(r"\s+", "", str(item.get("content") or ""))
        for item in (result.get("recent_confirmed") or [])
        if isinstance(item, Mapping) and str(item.get("content") or "").strip()
    }
    for candidate in candidates:
        if re.sub(r"\s+", "", candidate) not in recent:
            return candidate
    return candidates[0]


def group_persona_boundary_issues(
    request_text: object, reply: object, conversation_frame: Mapping | None = None,
) -> list[str]:
    """Detect three high-cost persona failures at the final send boundary.

    This is intentionally a small hard gate over observed production failures,
    not a sentiment classifier.  Grounded warmth, gratitude and ordinary
    playful language remain available to Persona and Assistant Affect.
    """

    request = str(request_text or "").strip()
    draft = str(reply or "").strip()
    frame = conversation_frame if isinstance(conversation_frame, Mapping) else {}
    issues: list[str] = []
    # The existing frame establishes who is talking to whom.  An explicitly
    # invited retort in an uninterrupted same-member exchange is different
    # from an ungrounded judgement of a member.  This is a narrow guard for
    # observed failures, not a replacement for Persona or a sentiment model.
    invited_banter = bool(
        frame.get("active_exchange")
        and frame.get("same_dialogue_actor")
        and not frame.get("intervening_other_actor")
        and not frame.get("ambiguous_target")
        and frame.get("reply_target_in_window")
        and _BANTER_INVITATION.search(request)
        and not _BANTER_STOP.search(request)
    )
    if _HOSTILE_REPLY.search(draft) or (_TARGETED_SHARP_REPLY.search(draft) and not invited_banter):
        issues.append(ISSUE_UNJUSTIFIED_HOSTILITY)
    if _IDENTITY_QUESTION.search(request) and (
        not _IDENTITY_TRUTH.search(draft)
        or re.fullmatch(r"我是助手(?:呀|啊|。)?", draft)
    ):
        issues.append(ISSUE_IDENTITY_EVASION)
    if _RELATIONSHIP_REQUEST.search(request) and _RELATIONSHIP_ACCEPT.search(draft):
        issues.append(ISSUE_RELATIONSHIP_OVERREACH)
    return issues


def _set_persona_boundary_fallback(result: dict, issues: Sequence[str]) -> str:
    selected = set(issues)
    if ISSUE_IDENTITY_EVASION in selected:
        name = str(result.get("assistant_name") or "当前助理").strip()[:80] or "当前助理"
        reply = _first_boundary_reply_not_recent(result, (
            f"我是{name}，这里的虚拟助手。",
            f"我叫{name}，在这里是虚拟助手。",
            f"这里是{name}，一个虚拟助手。",
        ))
        code = "identity_evasion_boundary"
    elif ISSUE_RELATIONSHIP_OVERREACH in selected:
        reply = _first_boundary_reply_not_recent(result, (
            "我可以继续和你熟悉地聊，但不会把关系说成已经确定了。",
            "可以好好聊，不过关系不靠一句话直接定下来。",
            "熟一点当然行，但不把关系直接说死。",
        ))
        code = "relationship_overreach_boundary"
    else:
        request = str(result.get("group_request_text") or "").strip()
        if "闭嘴" in request:
            candidates = ("好，我先不插话。", "行，这句先放着。", "知道了，我先安静会儿。")
        elif "滚" in request:
            candidates = ("行，我先不插话。", "好，这句就先到这儿。", "知道了，我先退一步。")
        else:
            candidates = ("别急，正常说就行。", "这句咱先放下，正常聊吧。", "聊事情就行，别往人身上招呼。")
        reply = _first_boundary_reply_not_recent(result, candidates)
        code = "unjustified_hostility_boundary"
    result.update({
        "ok": True,
        "dispatch": "chat",
        "reply": reply,
        "output": reply,
        "group_truth_blocked": False,
        "group_truth_send_state": SEND_STATE_REWRITTEN_PASSED,
        "group_truth_issues": [],
        "group_truth_rewrite_codes": [code],
        "group_style_gate": SEND_STATE_REWRITTEN_PASSED,
    })
    return reply


def _media_claim_without_evidence(reply: str, envelope: Mapping) -> str | None:
    media = envelope.get("media") if isinstance(envelope.get("media"), Mapping) else {}
    visual_context = str(media.get("visual_context") or "none")
    observation = str(media.get("observation") or "none")
    if visual_context == "ready" and observation not in {"none", "deferred", "blocked"}:
        return None
    for pattern in _MEDIA_CLAIM_PATTERNS:
        if pattern.search(reply):
            return ISSUE_MEDIA_CLAIM_WITHOUT_EVIDENCE
    for pattern in _SENSORY_CLAIM_PATTERNS:
        if pattern.search(reply):
            return ISSUE_MEDIA_CLAIM_WITHOUT_EVIDENCE
    for pattern in _VISUAL_EXPERIENCE_PATTERNS:
        if pattern.search(reply):
            return ISSUE_MEDIA_CLAIM_WITHOUT_EVIDENCE
    return None


def _fabricated_personal_experience(reply: str) -> str | None:
    for pattern in _EXPERIENCE_CLAIM_PATTERNS:
        if pattern.search(reply):
            return ISSUE_FABRICATED_PERSONAL_EXPERIENCE
    return None


def _unsupported_specific_fact(reply: str, envelope: Mapping) -> str | None:
    forbidden = envelope.get("forbidden_claim_types") if isinstance(envelope.get("forbidden_claim_types"), list) else []
    if "concrete_attribution" in forbidden:
        for pattern in _SPECIFIC_FACT_PATTERNS:
            if pattern.search(reply):
                return ISSUE_UNSUPPORTED_SPECIFIC_FACT
    return None


def _reply_target_mismatch(reply: str, envelope: Mapping) -> str | None:
    target = envelope.get("target") if isinstance(envelope.get("target"), Mapping) else {}
    if target.get("ambiguous"):
        # Ambiguous target: a draft that names a specific addressee or a
        # strong stance as if it knows the target is a mismatch.
        if (
            re.search(r"(?:甲|乙|丙|你说的)", reply)
            or any(pattern.search(reply) for pattern in _AMBIGUOUS_TARGET_JUDGEMENT_PATTERNS)
        ):
            return ISSUE_REPLY_TARGET_MISMATCH
    return None


def _directed_ambiguous_target_needs_boundary(reply: str, envelope: Mapping) -> bool:
    """Require a first-clause object clarification for a direct unknown target."""

    target = envelope.get("target") if isinstance(envelope.get("target"), Mapping) else {}
    if not target.get("ambiguous"):
        return False
    leading_clause = re.split(r"[。！？!?；;]", str(reply or "").strip(), maxsplit=1)[0]
    return not any(pattern.search(leading_clause) for pattern in _AMBIGUOUS_TARGET_OBJECT_CLARIFICATION_PATTERNS)


def group_final_truth_issues(
    reply: str,
    grounding_envelope: Mapping | None = None,
    *,
    recent_confirmed: Sequence[Mapping] | None = None,
    signature_budget: Mapping | None = None,
    request_text: str = "",
) -> list[str]:
    """Return the ordered truth-gate issues for one draft.

    Deterministic rules are the minimum guarantee; a model judge may only be
    an additive reviewer and must fail closed.
    """

    envelope = grounding_envelope if isinstance(grounding_envelope, Mapping) else {}
    issues: list[str] = []
    media_issue = _media_claim_without_evidence(reply, envelope)
    if media_issue:
        issues.append(media_issue)
    experience_issue = _fabricated_personal_experience(reply)
    if experience_issue:
        issues.append(experience_issue)
    fact_issue = _unsupported_specific_fact(reply, envelope)
    if fact_issue:
        issues.append(fact_issue)
    target_issue = _reply_target_mismatch(reply, envelope)
    if target_issue:
        issues.append(target_issue)
    if _current_daypart_mismatch(reply):
        issues.append(ISSUE_CURRENT_DAYPART_MISMATCH)
    if recent_confirmed is not None:
        cadence_issues = signature_budget_issues(
            reply,
            recent_confirmed,
            budget=signature_budget,
            request_text=request_text,
        )
        issues.extend(issue for issue in cadence_issues if issue not in issues)
    return issues


def _normalize_rewritten_text(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"[，,]\s*([。！？!?])", r"\1", text)
    if text and text[-1] not in "。！？!?…":
        text += "。"
    return text


def controlled_soft_rewrite(reply: str, issues: Sequence[str]) -> str:
    """Apply one evidence-preserving rewrite for known soft gate findings.

    Remove signature tokens or correct one current-daypart assertion using
    a trusted local clock. Agreement words never replace a concrete reply.
    A later gate pass is still mandatory before Delivery can be enqueued.
    """

    rewritten = str(reply or "").strip()
    selected = {str(issue or "") for issue in issues}
    if ISSUE_SIGNATURE_OVERUSE in selected:
        rewritten = re.sub(r"[喵～]+", "", rewritten)
    if ISSUE_CURRENT_DAYPART_MISMATCH in selected:
        rewritten = re.sub(r"大白天的?", "大晚上的", rewritten)
    return _normalize_rewritten_text(rewritten)


def _directed_reply_fallback(issues: Sequence[str], *, failure: bool = False) -> tuple[str, str]:
    """Return a user-safe direct-reply boundary without reusing the unsafe draft."""

    selected = {str(issue or "") for issue in issues}
    if failure:
        return _DIRECTED_FAILURE_BOUNDARY, "directed_reply_failure_fallback"
    if ISSUE_MEDIA_CLAIM_WITHOUT_EVIDENCE in selected:
        return _DIRECTED_MEDIA_BOUNDARY, "directed_media_boundary_fallback"
    if ISSUE_REPLY_TARGET_MISMATCH in selected:
        return _DIRECTED_TARGET_BOUNDARY, "directed_target_boundary_fallback"
    if {ISSUE_RESEARCH_EVIDENCE_MISSING, ISSUE_RESEARCH_UNVERIFIED} & selected:
        return _DIRECTED_RESEARCH_BOUNDARY, "directed_research_boundary_fallback"
    return _DIRECTED_FACT_BOUNDARY, "directed_fact_boundary_fallback"


def _set_directed_reply_fallback(result: dict, issues: Sequence[str], *, failure: bool = False) -> str:
    reply, code = _directed_reply_fallback(issues, failure=failure)
    result.update({
        # Delivery needs a genuine user-safe body.  The original model error
        # remains internal evidence; it is never copied to the group.
        "ok": True,
        "dispatch": "chat",
        "reply": reply,
        "output": reply,
        "group_truth_blocked": False,
        "group_truth_send_state": SEND_STATE_REWRITTEN_PASSED,
        "group_truth_issues": [],
        "group_truth_rewrite_codes": [code],
        "group_style_gate": SEND_STATE_REWRITTEN_PASSED,
    })
    return reply


def apply_group_safety_gate(result: dict, payload: dict, conversation_frame: dict | None = None, reply: str = "") -> str:
    """Cancel an uninvited targeted judgement before the final truth gate."""

    if (
        reply
        and str((conversation_frame or {}).get("attention") or "") == "active_continuation"
        and "uninvited_targeted_judgement" in group_reply_style_issues(
            str(payload.get("message") or ""), reply, uninvited=True,
        )
    ):
        result.update({
            "reply": "",
            "output": "",
            "group_safety_blocked": True,
            "group_safety_reason": "uninvited_targeted_judgement",
        })
        return ""
    return reply


def _apply_group_final_truth_gate(
    result: dict,
    conversation_frame: dict | None = None,
    *,
    reply_obligation: bool = False,
) -> str:
    """Apply the B4 final truth gate to one draft result in place.

    Mutates ``result`` by clearing ``reply``/``output`` and recording
    ``group_truth_blocked``/``group_truth_send_state``/``group_truth_issues``
    when the draft must not be sent.  Returns the possibly-emptied reply text.
    """

    frame = conversation_frame if isinstance(conversation_frame, dict) else {}
    reply_obligation = bool(
        reply_obligation
        or str(frame.get("attention") or "") in _DIRECTED_REPLY_ATTENTIONS
    )
    reply = str(result.get("reply") or "").strip()
    if not reply:
        # A malformed SinglePlan is a handled, terminal non-send.  A direct
        # mention does not turn an absent model answer into a fabricated one.
        if result.get("group_generation_invalid"):
            result.update({
                "reply": "", "output": "", "group_truth_send_state": SEND_STATE_BLOCKED,
                "group_truth_issues": ["group_direct_single_plan_reply_invalid"],
            })
            return ""
        # Media preflight can intentionally keep an unread image silent.  It is
        # not a failed model answer, even when the turn was a direct reply.
        if (
            result.get("dispatch") == "silent"
            and result.get("capability_limited")
            and result.get("reason") in {
                "media_observation_deferred", "media_notice_suppressed",
            }
        ):
            return ""
        if reply_obligation:
            return _set_directed_reply_fallback(result, [], failure=True)
        return reply
    style_gate = str(result.get("group_style_gate") or "")
    grounding_envelope = (
        frame.get("grounding_envelope")
    )
    style_issues = [
        str(item).strip()
        for item in (result.get("group_style_final_issues") or [])
        if str(item).strip()
    ]
    # A run of short, differently grounded one-sentence replies is a useful
    # expression diagnostic, not by itself a reason to lose a selected turn.
    # Keep all other style findings terminal and leave the exact-duplicate
    # check in apply_group_final_truth_gate intact.
    if style_gate == "degraded" and set(style_issues) == {"repeated_reply_shape"}:
        result["group_style_advisory_issues"] = style_issues
        result["group_style_gate"] = style_gate = SEND_STATE_PASSED
    final_send_state = group_final_send_state(style_gate)
    if reply_obligation and style_gate == "provider_failed":
        return _set_directed_reply_fallback(result, [], failure=True)
    if reply_obligation and style_gate == "degraded":
        if "internal_state_leak" in style_issues:
            return _set_directed_reply_fallback(result, [], failure=True)
        # Naturalness retry failure is a presentation concern.  It must not
        # erase an explicit question; hard truth checks below still apply.
        result.update({
            "group_truth_blocked": False,
            "group_truth_send_state": SEND_STATE_PASSED,
            "group_truth_nonblocking_style_issues": style_issues,
        })
        final_send_state = SEND_STATE_PASSED
    recent_confirmed = [
        item
        for item in (result.get("recent_confirmed") or [])
        if isinstance(item, dict)
    ]
    persona_issues = group_persona_boundary_issues(
        result.get("group_request_text") or frame.get("request_text") or "",
        reply,
        frame,
    )
    if persona_issues:
        if ISSUE_UNJUSTIFIED_HOSTILITY in persona_issues:
            result.update({
                "reply": "", "output": "", "group_truth_blocked": True,
                "group_truth_send_state": SEND_STATE_BLOCKED,
                "group_truth_issues": persona_issues,
                "group_truth_rewrite_codes": [],
                "group_style_gate": SEND_STATE_BLOCKED,
            })
            return ""
        if reply_obligation:
            return _set_persona_boundary_fallback(result, persona_issues)
        result.update({
            "reply": "",
            "output": "",
            "group_truth_blocked": True,
            "group_truth_send_state": SEND_STATE_BLOCKED,
            "group_truth_issues": persona_issues,
            "group_truth_rewrite_codes": [],
            "group_style_gate": SEND_STATE_BLOCKED,
        })
        return ""
    if reply_obligation and _directed_ambiguous_target_needs_boundary(reply, grounding_envelope):
        return _set_directed_reply_fallback(result, [ISSUE_REPLY_TARGET_MISMATCH])
    truth_issues = group_final_truth_issues(
        reply,
        grounding_envelope,
        recent_confirmed=recent_confirmed,
        request_text=str(result.get("group_request_text") or frame.get("request_text") or ""),
    )
    for issue in research_evidence_issues(reply, result.get("group_research")):
        if issue not in truth_issues:
            truth_issues.append(issue)
    hard_issues = [issue for issue in truth_issues if issue not in SOFT_REWRITE_ISSUES]
    soft_issues = [issue for issue in truth_issues if issue in SOFT_REWRITE_ISSUES]
    if final_send_state in {"blocked", "degraded"} or hard_issues:
        if reply_obligation:
            return _set_directed_reply_fallback(result, hard_issues)
        result.update({
            "reply": "",
            "output": "",
            "group_truth_blocked": True,
            "group_truth_send_state": final_send_state,
            "group_truth_issues": truth_issues,
            "group_truth_rewrite_codes": [],
            "group_style_gate": "blocked",
        })
        return ""
    if soft_issues:
        rewritten = controlled_soft_rewrite(reply, soft_issues)
        remaining_issues = group_final_truth_issues(
            rewritten,
            grounding_envelope,
            recent_confirmed=recent_confirmed,
            request_text=str(result.get("group_request_text") or frame.get("request_text") or ""),
        )
        for issue in research_evidence_issues(rewritten, result.get("group_research")):
            if issue not in remaining_issues:
                remaining_issues.append(issue)
        if rewritten and not remaining_issues:
            result.update({
                "reply": rewritten,
                "output": rewritten,
                "group_truth_blocked": False,
                "group_truth_send_state": SEND_STATE_REWRITTEN_PASSED,
                "group_truth_issues": [],
                "group_truth_rewrite_codes": soft_issues,
                "group_style_gate": SEND_STATE_REWRITTEN_PASSED,
            })
            return rewritten
        result.update({
            "reply": "",
            "output": "",
            "group_truth_blocked": True,
            "group_truth_send_state": SEND_STATE_BLOCKED,
            "group_truth_issues": remaining_issues or truth_issues,
            "group_truth_rewrite_codes": soft_issues,
            "group_style_gate": SEND_STATE_BLOCKED,
        })
        return ""
    return reply


def apply_group_final_truth_gate(
    result: dict,
    conversation_frame: dict | None = None,
    *,
    reply_obligation: bool = False,
) -> str:
    """Validate the exact outgoing body after every local truth rewrite."""
    reply = _apply_group_final_truth_gate(
        result, conversation_frame, reply_obligation=reply_obligation,
    )
    # Inspect exact normalized text, including any earlier rewrite/fallback.
    risk_issues = group_contextual_quality_issues(
        str(result.get("group_request_text") or (conversation_frame or {}).get("request_text") or ""),
        reply, result.get("_group_quality_context"),
    ) if reply else []
    if risk_issues or result.get("group_quality_repair_failed"):
        result.update({"reply": "", "output": "", "group_truth_blocked": True,
                       "group_truth_send_state": SEND_STATE_BLOCKED,
                       "group_truth_issues": risk_issues or result.get("group_quality_risk_codes") or ["quality_repair_failed"],
                       "group_style_gate": SEND_STATE_BLOCKED})
        return ""
    recent = result.get("recent_confirmed") or []
    # complete_group_dispatch supplies newest confirmed projection first.
    previous = next((str(item.get("content") or "").strip()
                     for item in recent if isinstance(item, Mapping)
                     and str(item.get("content") or "").strip()), "")
    if not reply or reply != previous:
        return reply
    decision = result.get("group_decision") or {}
    directed = reply_obligation or str((conversation_frame or {}).get("attention") or "") in _DIRECTED_REPLY_ATTENTIONS
    # A real direct answer or a declared echo is not an unsolicited duplicate.
    # Truth checks above still apply; this exception never repairs unsafe facts.
    if directed or decision.get("social_action") == "echo_reaction":
        return reply
    issues = list(result.get("group_style_final_issues") or [])
    if "repeated_final_reply" not in issues:
        issues.append("repeated_final_reply")
    result.update({
        "reply": "", "output": "", "group_truth_blocked": True,
        "group_truth_send_state": SEND_STATE_BLOCKED,
        "group_style_gate": SEND_STATE_BLOCKED,
        "group_style_final_issues": issues,
    })
    return ""


def research_evidence_issues(reply: str, research: object) -> list[str]:
    """Require a visible citation whenever R7 supplied public research facts."""

    if not isinstance(research, Mapping):
        return []
    if research.get("fact_unverified"):
        return [ISSUE_RESEARCH_UNVERIFIED]
    urls = [
        str(item).strip()
        for item in (research.get("source_urls") or [])
        if str(item).strip().startswith("https://")
    ]
    if urls and not any(url in str(reply or "") for url in urls):
        return [ISSUE_RESEARCH_EVIDENCE_MISSING]
    return []


__all__ = [
    "DEFAULT_SIGNATURE_BUDGET_MAX",
    "DEFAULT_SIGNATURE_BUDGET_MAX_CONSECUTIVE",
    "DEFAULT_SIGNATURE_BUDGET_WINDOW",
    "ISSUE_FABRICATED_PERSONAL_EXPERIENCE",
    "ISSUE_MEDIA_CLAIM_WITHOUT_EVIDENCE",
    "ISSUE_REPLY_TARGET_MISMATCH",
    "ISSUE_RESEARCH_EVIDENCE_MISSING",
    "ISSUE_RESEARCH_UNVERIFIED",
    "ISSUE_UNJUSTIFIED_HOSTILITY",
    "ISSUE_IDENTITY_EVASION",
    "ISSUE_RELATIONSHIP_OVERREACH",
    "ISSUE_SIGNATURE_OVERUSE",
    "ISSUE_SYCOPHANTIC_AGREEMENT",
    "ISSUE_TOPIC_ANCHOR_MISMATCH",
    "ISSUE_UNSUPPORTED_SPECIFIC_FACT",
    "SEND_STATE_BLOCKED",
    "SEND_STATE_DEGRADED",
    "SEND_STATE_PASSED",
    "SEND_STATE_REWRITTEN_PASSED",
    "SIGNATURE_TOKENS",
    "apply_group_final_truth_gate",
    "apply_group_safety_gate",
    "controlled_soft_rewrite",
    "group_final_send_state",
    "group_final_truth_issues",
    "group_persona_boundary_issues",
    "normalize_signature_budget",
    "research_evidence_issues",
    "signature_budget_issues",
]
