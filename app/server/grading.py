"""Локальная (мгновенная) проверка текстовых ответов: короткие вставки и определения."""
import re
from difflib import SequenceMatcher

STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня еще нет о из ему
теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам сказал ведь там потом себя ничего ей может они тут где есть
надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один
почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про всего них какая
много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более всегда конечно всю между это которые который которая
которое также либо чаще обычно называется называют используется означает
the a an of to in is are and or for with as by on that this it be from at which its their can used called
""".split())

_SPLIT = re.compile(r"[^\wЀ-ӿ]+", re.U)


def norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[«»\"'`“”„]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def stem(w: str) -> str:
    if re.search(r"[а-я]", w):
        if len(w) <= 4:
            return w
        return w[:4] if len(w) == 5 else w[:5]
    if len(w) <= 5:
        return w
    return w[:6]


def stems(text: str, min_len: int = 3) -> set[str]:
    out = set()
    for w in _SPLIT.split(norm(text)):
        if len(w) >= min_len and w not in STOP and not w.isdigit():
            out.add(stem(w))
    return out


def coverage(answer: str, reference: str) -> float:
    ref = stems(reference)
    if not ref:
        return 0.0
    ans = stems(answer)
    return len(ref & ans) / len(ref)


def _kp_hit(answer_stems: set[str], answer_norm: str, kp: str) -> bool:
    kps = stems(kp, 2)
    if not kps:
        return norm(kp) in answer_norm
    return len(kps & answer_stems) / len(kps) >= 0.6


def grade_write(answer: str, reference: str, key_points: list[str] | None = None) -> tuple[float, str]:
    """Вернёт (score, verdict): 1.0 верно, 0.5 частично, 0.0 неверно."""
    answer = (answer or "").strip()
    if len(stems(answer)) < 1 or len(answer) < 3:
        return 0.0, "wrong"
    ref_cov = coverage(answer, reference)
    kp_cov = None
    if key_points:
        a_st, a_n = stems(answer), norm(answer)
        kp_cov = sum(_kp_hit(a_st, a_n, k) for k in key_points) / len(key_points)
    sim = SequenceMatcher(None, norm(answer), norm(reference)).ratio()
    score = max(ref_cov, sim * 0.9)
    if kp_cov is not None:
        score = max(score * 0.6 + kp_cov * 0.4, kp_cov * 0.85, ref_cov * 0.9)
    if score >= 0.6:
        return 1.0, "correct"
    if score >= 0.35:
        return 0.5, "partial"
    return 0.0, "wrong"


def lev_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def grade_fill(answer: str, accept: list[str]) -> bool:
    a = norm(answer)
    a = re.sub(r"^[\s\W_]+|[\s\W_]+$", "", a)
    if not a:
        return False
    for ref in accept:
        r = norm(ref)
        r = re.sub(r"^[\s\W_]+|[\s\W_]+$", "", r)
        if not r:
            continue
        if a == r:
            return True
        if len(r) >= 4 and lev_ratio(a, r) >= 0.86:
            return True
        ra, aa = stems(r, 2), stems(a, 2)
        if ra and ra == aa:
            return True
        if len(r.split()) >= 2 and r in a and len(a) <= len(r) * 2:
            return True
    return False
