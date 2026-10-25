"""
「プロフィール編集」チャンネルの画面（ボタン・プルダウン・入力フォーム）。

チャンネルに「プロフィールを編集する」ボタンを1つ置いておき、押した本人にだけ見える画面
（ephemeral）で、名前・年齢・性別・都道府県・市町村・業種と事業内容を編集できる。
スプレッドシートへの書き込みは profile_sheet.py が行う。
変更したら「id同期ログ」に、変更前と変更後を通知する。
"""

from __future__ import annotations

import asyncio
from typing import Callable

import discord

import prefectures
import profile_sheet
import sheets_client

AGE_OPTIONS = ["10代", "20代", "30代", "40代", "50代", "60代", "70代以上"]
GENDER_OPTIONS = ["男性", "女性", "その他"]

# bot.py から「id同期ログ」チャンネルを探す関数を受け取る（循環importを避けるため）
find_log_channel: Callable[[discord.Guild], discord.abc.Messageable | None] | None = None


# ---------------------------------------------------------------------------
# 共通の部品
# ---------------------------------------------------------------------------

def build_prefecture_selects(selected: str | None, on_select) -> list[discord.ui.Select]:
    """
    47都道府県を2つのプルダウンに分けて作る（Discordのプルダウンは25個までのため）。
    選ばれている都道府県には印（default）を付ける。on_select(interaction, 選んだ都道府県) を呼ぶ。
    """
    selects = []
    for placeholder, names in prefectures.PREFECTURE_GROUPS:
        select = discord.ui.Select(
            placeholder=placeholder,
            options=[discord.SelectOption(label=name, default=(name == selected)) for name in names],
            min_values=1,
            max_values=1,
        )

        async def callback(interaction: discord.Interaction, _select=select):
            await on_select(interaction, _select.values[0])

        select.callback = callback
        selects.append(select)
    return selects


def mark_defaults(select: discord.ui.Select, selected: str | None):
    for option in select.options:
        option.default = option.value == selected


def _business_type_select(options: list[str], current: str | None) -> discord.ui.Select:
    """業種のプルダウン（「設定用」シートの選択肢。今の業種が一覧に無ければ先頭に足す）。"""
    names = list(dict.fromkeys(o[:100] for o in options if o.strip()))
    if current and current[:100] not in names:
        names.insert(0, current[:100])
    names = names[:25]
    return discord.ui.Select(
        placeholder="業種を選択してください",
        options=[discord.SelectOption(label=n, value=n, default=(n == current)) for n in names],
        min_values=1,
        max_values=1,
    )


def _profile_text(profile: dict, notice: str = "") -> str:
    location = f"{profile['prefecture']}{profile['city']}" or "（未入力）"
    if profile["businesses"]:
        businesses = "\n".join(
            f"　{i + 1}. 【{b['type'] or '業種なし'}】{b['content']}" for i, b in enumerate(profile["businesses"])
        )
    else:
        businesses = "　（まだありません）"
    head = f"{notice}\n\n" if notice else ""
    return (
        f"{head}📝 **あなたのプロフィール**（あなたにだけ表示されています）\n"
        f"名前：{profile['name']}\n"
        f"年齢：{profile['age'] or '（未入力）'}　性別：{profile['gender'] or '（未入力）'}\n"
        f"地域：{location}\n"
        f"業種と事業内容：\n{businesses}\n\n"
        "変えたい項目のボタンを押してください。"
    )


async def _log(interaction: discord.Interaction, profile_name: str, lines: list[str]):
    """id同期ログに「誰が何をどう変えたか」を通知する。"""
    guild = interaction.guild
    if guild is None or find_log_channel is None:
        return
    channel = find_log_channel(guild)
    if channel is None:
        return
    body = "\n".join(f"・{line}" for line in lines)
    try:
        await channel.send(
            f"✏️ {interaction.user.mention}（{profile_name}）さんがプロフィールを編集しました\n{body}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        pass


def _short(text: str, limit: int = 40) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _refresh_cache():
    """会員一覧ページなどに、すぐ反映されるようにする。"""
    try:
        await asyncio.to_thread(sheets_client.get_members, True)
    except Exception:  # noqa: BLE001
        pass


async def _report_error(interaction: discord.Interaction, error: Exception):
    if isinstance(error, profile_sheet.ProfileError):
        message = str(error)
    else:
        import traceback

        traceback.print_exception(type(error), error, error.__traceback__)
        message = f"処理中にエラーが発生しました：{error}\nお手数ですが管理者に連絡してください。"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


class _BaseView(discord.ui.View):
    def __init__(self, member_id: int, timeout: float = 900):
        super().__init__(timeout=timeout)
        self.member_id = member_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("この画面はあなた用ではありません。", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error, item):
        await _report_error(interaction, error)


class _BaseModal(discord.ui.Modal):
    async def on_error(self, interaction, error):
        await _report_error(interaction, error)


async def show_menu(interaction: discord.Interaction, notice: str = "", business_type_options=None):
    """最新のプロフィールを読み直して、編集メニューを表示する（同じメッセージを書き換える）。"""
    profile = await asyncio.to_thread(profile_sheet.get_profile, str(interaction.user.id))
    if business_type_options is None:
        business_type_options = await asyncio.to_thread(sheets_client.get_business_type_options)
    view = ProfileEditMenu(interaction.user.id, profile, business_type_options)
    await interaction.edit_original_response(content=_profile_text(profile, notice), view=view)


# ---------------------------------------------------------------------------
# チャンネルに置くボタン（再起動しても押せるよう custom_id を固定）
# ---------------------------------------------------------------------------

class ProfileEditPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="プロフィールを編集する",
        style=discord.ButtonStyle.primary,
        emoji="✏️",
        custom_id="persistent_profile_edit_button",
    )
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            profile = await asyncio.to_thread(profile_sheet.get_profile, str(interaction.user.id))
            options = await asyncio.to_thread(sheets_client.get_business_type_options)
        except profile_sheet.ProfileError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        view = ProfileEditMenu(interaction.user.id, profile, options)
        await interaction.followup.send(_profile_text(profile), view=view, ephemeral=True)

    async def on_error(self, interaction, error, item):
        await _report_error(interaction, error)


# ---------------------------------------------------------------------------
# 編集メニュー
# ---------------------------------------------------------------------------

class ProfileEditMenu(_BaseView):
    def __init__(self, member_id: int, profile: dict, business_type_options: list[str]):
        super().__init__(member_id)
        self.profile = profile
        self.options = business_type_options

    @discord.ui.button(label="名前", style=discord.ButtonStyle.secondary, emoji="👤", row=0)
    async def edit_name(self, interaction: discord.Interaction, button):
        await interaction.response.send_modal(NameEditModal(self))

    @discord.ui.button(label="年齢・性別", style=discord.ButtonStyle.secondary, emoji="🎂", row=0)
    async def edit_age_gender(self, interaction: discord.Interaction, button):
        view = AgeGenderEditView(self)
        await interaction.response.edit_message(content=view.text(), view=view)

    @discord.ui.button(label="都道府県・市町村", style=discord.ButtonStyle.secondary, emoji="📍", row=0)
    async def edit_location(self, interaction: discord.Interaction, button):
        view = LocationEditView(self)
        await interaction.response.edit_message(content=view.text(), view=view)

    @discord.ui.button(label="業種を追加", style=discord.ButtonStyle.success, emoji="➕", row=1)
    async def add_business(self, interaction: discord.Interaction, button):
        if not self.options:
            await interaction.response.send_message(
                "業種の選択肢が設定されていないため追加できません。管理者に連絡してください。", ephemeral=True
            )
            return
        await interaction.response.send_modal(BusinessModal(self, None))

    @discord.ui.button(label="業種を修正", style=discord.ButtonStyle.secondary, emoji="🛠️", row=1)
    async def edit_business(self, interaction: discord.Interaction, button):
        businesses = self.profile["businesses"]
        if not businesses:
            await interaction.response.send_message("修正できる業種がありません。「業種を追加」を使ってください。", ephemeral=True)
            return
        if len(businesses) == 1:
            await interaction.response.send_modal(BusinessModal(self, businesses[0]))
            return
        view = BusinessPickView(self, "edit")
        await interaction.response.edit_message(content="修正する業種を選んでください。", view=view)

    @discord.ui.button(label="業種を削除", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def delete_business(self, interaction: discord.Interaction, button):
        if len(self.profile["businesses"]) <= 1:
            await interaction.response.send_message(
                "業種と事業内容は最低1件必要なため、最後の1件は削除できません。「業種を修正」を使ってください。",
                ephemeral=True,
            )
            return
        view = BusinessPickView(self, "delete")
        await interaction.response.edit_message(content="削除する業種を選んでください。", view=view)


class _BackButton(discord.ui.Button):
    def __init__(self, menu: ProfileEditMenu, row: int):
        super().__init__(label="戻る", style=discord.ButtonStyle.secondary, row=row)
        self.menu = menu

    async def callback(self, interaction: discord.Interaction):
        menu = ProfileEditMenu(self.menu.member_id, self.menu.profile, self.menu.options)
        await interaction.response.edit_message(content=_profile_text(self.menu.profile), view=menu)


# --- 名前 -----------------------------------------------------------------

class NameEditModal(_BaseModal, title="名前を変更"):
    def __init__(self, menu: ProfileEditMenu):
        super().__init__()
        self.menu = menu
        self.name_input = discord.ui.TextInput(
            label="名前（本名）",
            default=menu.profile["name"],
            min_length=1,
            max_length=32,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        new_name = " ".join(self.name_input.value.split())
        diffs = await asyncio.to_thread(
            profile_sheet.update_fields, str(interaction.user.id), {"name": new_name}
        )
        await _after_field_update(interaction, self.menu, diffs)


async def _after_field_update(interaction: discord.Interaction, menu: ProfileEditMenu, diffs: dict):
    if not diffs:
        await show_menu(interaction, "変更はありませんでした。", menu.options)
        return
    lines = [f"{profile_sheet.FIELD_LABELS[k]}：{old or '（空欄）'} → {new}" for k, (old, new) in diffs.items()]
    name = diffs["name"][1] if "name" in diffs else menu.profile["name"]
    await _log(interaction, name, lines)
    await _refresh_cache()
    await show_menu(interaction, "✅ 保存しました\n" + "\n".join(lines), menu.options)


# --- 年齢・性別 -------------------------------------------------------------

class AgeGenderEditView(_BaseView):
    def __init__(self, menu: ProfileEditMenu):
        super().__init__(menu.member_id)
        self.menu = menu
        self.age = menu.profile["age"] if menu.profile["age"] in AGE_OPTIONS else None
        self.gender = menu.profile["gender"] if menu.profile["gender"] in GENDER_OPTIONS else None

        self.age_select = discord.ui.Select(
            placeholder="年齢を選択",
            options=[discord.SelectOption(label=a, default=(a == self.age)) for a in AGE_OPTIONS],
            row=0,
        )
        self.age_select.callback = self.on_age
        self.gender_select = discord.ui.Select(
            placeholder="性別を選択",
            options=[discord.SelectOption(label=g, default=(g == self.gender)) for g in GENDER_OPTIONS],
            row=1,
        )
        self.gender_select.callback = self.on_gender
        self.add_item(self.age_select)
        self.add_item(self.gender_select)
        self.add_item(_BackButton(menu, row=2))

    def text(self) -> str:
        return "年齢と性別を選んで「保存する」を押してください。"

    async def on_age(self, interaction: discord.Interaction):
        self.age = self.age_select.values[0]
        mark_defaults(self.age_select, self.age)
        await interaction.response.edit_message(view=self)

    async def on_gender(self, interaction: discord.Interaction):
        self.gender = self.gender_select.values[0]
        mark_defaults(self.gender_select, self.gender)
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="保存する", style=discord.ButtonStyle.primary, row=2)
    async def save(self, interaction: discord.Interaction, button):
        if not self.age or not self.gender:
            await interaction.response.send_message("年齢と性別の両方を選んでください。", ephemeral=True)
            return
        await interaction.response.defer()
        diffs = await asyncio.to_thread(
            profile_sheet.update_fields, str(interaction.user.id), {"age": self.age, "gender": self.gender}
        )
        await _after_field_update(interaction, self.menu, diffs)


# --- 都道府県・市町村 ---------------------------------------------------------

class LocationEditView(_BaseView):
    def __init__(self, menu: ProfileEditMenu):
        super().__init__(menu.member_id)
        self.menu = menu
        self.prefecture = prefectures.normalize_prefecture(menu.profile["prefecture"])
        self.pref_selects = build_prefecture_selects(self.prefecture, self.on_prefecture)
        for i, select in enumerate(self.pref_selects):
            select.row = i
            self.add_item(select)
        self.add_item(_BackButton(menu, row=2))

    def text(self) -> str:
        current = self.prefecture or "（未選択）"
        return (
            "都道府県を、どちらかのプルダウンから選んでください。\n"
            "選んだら「次へ（市町村を入力）」を押してください。\n\n"
            f"選択中：**{current}**"
        )

    async def on_prefecture(self, interaction: discord.Interaction, value: str):
        self.prefecture = value
        for select in self.pref_selects:
            mark_defaults(select, value)
        await interaction.response.edit_message(content=self.text(), view=self)

    @discord.ui.button(label="次へ（市町村を入力）", style=discord.ButtonStyle.primary, row=2)
    async def next_step(self, interaction: discord.Interaction, button):
        if not self.prefecture:
            await interaction.response.send_message("先に都道府県を選んでください。", ephemeral=True)
            return
        await interaction.response.send_modal(CityModal(self))


class CityModal(_BaseModal, title="市町村を入力"):
    def __init__(self, parent: LocationEditView):
        super().__init__()
        self.parent = parent
        self.city_input = discord.ui.TextInput(
            label=f"市町村（{parent.prefecture}）",
            placeholder="例：大阪市北区",
            default=parent.menu.profile["city"] or None,
            max_length=50,
        )
        self.add_item(self.city_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        diffs = await asyncio.to_thread(
            profile_sheet.update_fields,
            str(interaction.user.id),
            {"prefecture": self.parent.prefecture, "city": self.city_input.value.strip()},
        )
        await _after_field_update(interaction, self.parent.menu, diffs)


# --- 業種と事業内容 -----------------------------------------------------------

class BusinessModal(_BaseModal):
    """business が None なら追加、指定があればその1件を修正する。"""

    def __init__(self, menu: ProfileEditMenu, business: dict | None):
        super().__init__(title="業種と事業内容を修正" if business else "業種と事業内容を追加")
        self.menu = menu
        self.business = business
        self.type_select = _business_type_select(menu.options, business["type"] if business else None)
        self.content_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder="例：カフェの経営",
            default=(business["content"] if business else None) or None,
            max_length=1000,
        )
        self.add_item(discord.ui.Label(text="業種", component=self.type_select))
        self.add_item(
            discord.ui.Label(
                text="事業/活動内容",
                description="上で選んだ業種で、どんな事業・活動をしているか",
                component=self.content_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        business_type = self.type_select.values[0] if self.type_select.values else ""
        content = self.content_input.value.strip()
        user_id = str(interaction.user.id)

        if self.business is None:
            await asyncio.to_thread(profile_sheet.add_business, user_id, business_type, content)
            lines = [f"業種を追加：【{business_type}】{_short(content)}"]
        else:
            old = (self.business["type"], self.business["content"])
            await asyncio.to_thread(
                profile_sheet.update_business, user_id, self.business["row"], old, business_type, content
            )
            lines = [f"業種を修正：【{old[0]}】{_short(old[1])} → 【{business_type}】{_short(content)}"]

        await _log(interaction, self.menu.profile["name"], lines)
        await _refresh_cache()
        await show_menu(interaction, "✅ 保存しました\n" + lines[0], self.menu.options)


class BusinessPickView(_BaseView):
    """修正・削除する業種を選ぶ画面。mode は "edit" か "delete"。"""

    def __init__(self, menu: ProfileEditMenu, mode: str):
        super().__init__(menu.member_id)
        self.menu = menu
        self.mode = mode
        select = discord.ui.Select(
            placeholder="業種を選択",
            options=[
                discord.SelectOption(
                    label=f"{i + 1}. {b['type'] or '業種なし'}"[:100],
                    description=_short(b["content"], 90) or None,
                    value=str(i),
                )
                for i, b in enumerate(menu.profile["businesses"][:25])
            ],
            row=0,
        )
        select.callback = self.on_pick
        self.select = select
        self.add_item(select)
        self.add_item(_BackButton(menu, row=1))

    async def on_pick(self, interaction: discord.Interaction):
        business = self.menu.profile["businesses"][int(self.select.values[0])]
        if self.mode == "edit":
            await interaction.response.send_modal(BusinessModal(self.menu, business))
            return
        view = DeleteConfirmView(self.menu, business)
        await interaction.response.edit_message(
            content=f"この業種を削除しますか？\n【{business['type']}】{business['content']}",
            view=view,
        )


class DeleteConfirmView(_BaseView):
    def __init__(self, menu: ProfileEditMenu, business: dict):
        super().__init__(menu.member_id)
        self.menu = menu
        self.business = business
        self.add_item(_BackButton(menu, row=0))

    @discord.ui.button(label="削除する", style=discord.ButtonStyle.danger, row=0)
    async def confirm(self, interaction: discord.Interaction, button):
        await interaction.response.defer()
        old = (self.business["type"], self.business["content"])
        await asyncio.to_thread(
            profile_sheet.delete_business, str(interaction.user.id), self.business["row"], old
        )
        line = f"業種を削除：【{old[0]}】{_short(old[1])}"
        await _log(interaction, self.menu.profile["name"], [line])
        await _refresh_cache()
        await show_menu(interaction, "✅ 削除しました\n" + line, self.menu.options)
