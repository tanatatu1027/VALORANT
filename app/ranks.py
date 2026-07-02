"""VALORANTのランク定義と順序。"""

from __future__ import annotations

# 低い順に並べたランク帯（ティア単位）
RANKS = [
    "アイアン",
    "ブロンズ",
    "シルバー",
    "ゴールド",
    "プラチナ",
    "ダイヤモンド",
    "アセンダント",
    "イモータル",
    "レディアント",
]

# 英語表記との対応（API入力の揺れを吸収する）
RANK_ALIASES = {
    "iron": "アイアン",
    "bronze": "ブロンズ",
    "silver": "シルバー",
    "gold": "ゴールド",
    "platinum": "プラチナ",
    "plat": "プラチナ",
    "diamond": "ダイヤモンド",
    "dia": "ダイヤモンド",
    "ascendant": "アセンダント",
    "immortal": "イモータル",
    "radiant": "レディアント",
}


def normalize_rank(rank: str) -> str:
    """入力されたランク名を正規化する。不明な場合は ValueError。"""
    r = rank.strip()
    if r in RANKS:
        return r
    lowered = r.lower()
    if lowered in RANK_ALIASES:
        return RANK_ALIASES[lowered]
    raise ValueError(f"不明なランクです: {rank}")


def next_rank(rank: str) -> str:
    """次のランク帯を返す。レディアントの場合はそのまま返す。"""
    r = normalize_rank(rank)
    idx = RANKS.index(r)
    if idx >= len(RANKS) - 1:
        return r
    return RANKS[idx + 1]
