"""
都道府県の一覧と、表記ゆれ（「大阪」「大阪府」など）をそろえるための関数。

Discordのプルダウン（セレクトメニュー）は1つにつき選択肢が25個までなので、
47都道府県を「北海道〜三重県」（24個）と「滋賀県〜沖縄県」（23個）の2つに分けて表示する。
"""

from __future__ import annotations

import unicodedata

PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県",
    "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県",
    "福岡県", "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]

# プルダウン2つに分ける（どちらも25個以内）
#   東日本：北海道・東北・関東・甲信越・北陸・東海（24）
#   西日本：近畿・中国・四国・九州・沖縄（23）
PREFECTURE_GROUPS = [
    ("東日本（北海道〜三重県）", PREFECTURES[:24]),
    ("西日本（滋賀県〜沖縄県）", PREFECTURES[24:]),
]

# 「大阪」→「大阪府」のように、末尾の都・道・府・県を省いた書き方からの対応表
_SHORT_NAMES = {}
for _pref in PREFECTURES:
    if _pref == "北海道":
        _SHORT_NAMES["北海道"] = _pref
    else:
        _SHORT_NAMES[_pref[:-1]] = _pref


def normalize_prefecture(text: str) -> str | None:
    """
    都道府県の書き方を正式名称にそろえる。
    「大阪」「大阪府」「 大阪 府 」→「大阪府」、「東京」→「東京都」など。
    都道府県だと判断できないもの（「札幌」「関西」「大阪府大阪市」など）は None を返す。
    """
    value = "".join(unicodedata.normalize("NFKC", str(text or "")).split())
    if not value:
        return None
    if value in PREFECTURES:
        return value
    return _SHORT_NAMES.get(value)


_SEPARATORS = "、,，・/／ 　\n"


def split_prefectures(text: str) -> list[str] | None:
    """
    1つのセルに複数の都道府県が入っているとき、正式名称のリストに分ける。
    「大阪府、東京都」「大阪・東京」「大阪府 東京都」「大阪東京」→ ["大阪府", "東京都"]
    1つでも都道府県だと判断できない部分があれば None を返す（手で直してもらう）。
    """
    value = unicodedata.normalize("NFKC", str(text or "")).strip()
    for sep in _SEPARATORS:
        value = value.replace(sep, "\0")
    tokens = [t for t in value.split("\0") if t]
    if not tokens:
        return []

    names = sorted(set(PREFECTURES) | set(_SHORT_NAMES), key=len, reverse=True)
    result: list[str] = []
    for token in tokens:
        rest = token
        while rest:
            match = next((n for n in names if rest.startswith(n)), None)
            if match is None:
                return None
            fixed = match if match in PREFECTURES else _SHORT_NAMES[match]
            rest = rest[len(match):]
            if fixed not in result:
                result.append(fixed)
    return result


def split_cities(text: str) -> list[str]:
    """市町村のセルを区切り文字で分ける（都道府県が複数あるときの対応付け用）。"""
    value = unicodedata.normalize("NFKC", str(text or "")).strip()
    for sep in "、,，・/／\n":
        value = value.replace(sep, "\0")
    return [t.strip() for t in value.split("\0") if t.strip()]
