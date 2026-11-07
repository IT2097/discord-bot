"""
コミュニティのフォーラム（例：「趣味」）に、新しい枠（投稿）を作るためのリクエストと承認の仕組み。

会員が自由に投稿（枠）を作れないようにし、代わりに次の流れにする。
1. 「📝 新しい枠をリクエスト」ボタン → フォームで「どのフォーラムに」「枠の名前」「説明」を入力して送る
   （送れるのは PREMIUM_ROLE_NAME のロールを持つ人＝4000円プランの人だけ。承認待ちは1人1件まで）
2. 管理者だけが見られる「投稿リクエスト」チャンネルに、承認・却下ボタン付きで届く
3. 承認すると、そのフォーラムに、リクエストした人の名前とアイコンで枠（投稿）が作られ、
   「チャンネル紹介」でメンション付きで告知される。リクエストした人にはDMでも知らせる
4. 却下すると、リクエストした人にDMで知らせる（理由を書いた場合は理由も）

リクエストの中身（どのフォーラム・名前・説明・誰から）は、管理者向けメッセージの埋め込みに書いておくので、
botが再起動しても承認・却下ボタンはそのまま使える（ボタンの custom_id にリクエストした人のIDを入れている）。
"""

from __future__ import annotations

import os
import re

import discord

COMMUNITY_CATEGORY_NAME = os.getenv("COMMUNITY_CATEGORY_NAME", "コミュニティ")
REQUEST_ADMIN_CHANNEL_NAME = os.getenv("FORUM_REQUEST_CHANNEL_NAME", "投稿リクエスト")
NOTICE_CHANNEL_NAME = os.getenv("COMMUNITY_NOTICE_CHANNEL_NAME", "チャンネル紹介")
PREMIUM_ROLE_NAME = os.getenv("PREMIUM_ROLE_NAME", "").strip()
ADMIN_ROLE_NAME = os.getenv("ADMIN_ROLE_NAME", "管理者")
MEMBER_ROLE_NAME = os.getenv("MEMBER_ROLE_NAME", "会員")
UNVERIFIED_ROLE_NAME = os.getenv("UNVERIFIED_ROLE_NAME", "未登録")
WEBHOOK_NAME = "枠リクエスト"

_STATUS_PENDING = "承認待ち"


# ---------------------------------------------------------------------------
# 共通
# ---------------------------------------------------------------------------

def community_forums(guild: discord.Guild) -> list[discord.ForumChannel]:
    return [
        f for f in guild.forums
        if f.category is not None and f.category.name == COMMUNITY_CATEGORY_NAME
    ]


def _is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or any(r.name == ADMIN_ROLE_NAME for r in member.roles)


def _can_request(member: discord.Member) -> bool:
    if _is_admin(member):
        return True
    return bool(PREMIUM_ROLE_NAME) and any(r.name == PREMIUM_ROLE_NAME for r in member.roles)


def _admin_channel(guild: discord.Guild) -> discord.TextChannel | None:
    return discord.utils.get(guild.text_channels, name=REQUEST_ADMIN_CHANNEL_NAME)


def _notice_channel(guild: discord.Guild) -> discord.TextChannel | None:
    return discord.utils.get(guild.text_channels, name=NOTICE_CHANNEL_NAME)


async def _report_error(interaction: discord.Interaction, error: Exception):
    import traceback

    traceback.print_exception(type(error), error, error.__traceback__)
    message = f"処理中にエラーが発生しました：{error}\nお手数ですが運営に連絡してください。"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


async def _has_pending_request(admin_channel: discord.TextChannel, user_id: int) -> bool:
    """管理者向けチャンネルに、この人の承認待ちのリクエストが残っているか。"""
    async for message in admin_channel.history(limit=200):
        if not message.embeds or message.author.id != admin_channel.guild.me.id:
            continue
        embed = message.embeds[0]
        if embed.footer and embed.footer.text == f"{_STATUS_PENDING}｜{user_id}":
            return True
    return False


# ---------------------------------------------------------------------------
# 会員側：リクエストを送る
# ---------------------------------------------------------------------------

class RequestPanel(discord.ui.View):
    """「📝 新しい枠をリクエスト」ボタン（再起動しても押せるよう custom_id を固定）。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="新しい枠をリクエスト",
        style=discord.ButtonStyle.primary,
        emoji="📝",
        custom_id="persistent_forum_request_button",
    )
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        if not isinstance(member, discord.Member) or not _can_request(member):
            await interaction.response.send_message(
                "新しい枠のリクエストは、コミュニティ作成プランの方だけが送れます。", ephemeral=True
            )
            return
        forums = community_forums(interaction.guild)
        if not forums:
            await interaction.response.send_message(
                f"「{COMMUNITY_CATEGORY_NAME}」カテゴリにフォーラムが見つかりません。運営に連絡してください。",
                ephemeral=True,
            )
            return
        admin_channel = _admin_channel(interaction.guild)
        if admin_channel is None:
            await interaction.response.send_message(
                f"「{REQUEST_ADMIN_CHANNEL_NAME}」チャンネルが見つかりません。運営に連絡してください。", ephemeral=True
            )
            return
        if await _has_pending_request(admin_channel, member.id):
            await interaction.response.send_message(
                "承認待ちのリクエストがすでにあります。結果が届くまでお待ちください。", ephemeral=True
            )
            return
        await interaction.response.send_modal(RequestModal(forums))

    async def on_error(self, interaction, error, item):
        await _report_error(interaction, error)


class RequestModal(discord.ui.Modal, title="新しい枠をリクエスト"):
    def __init__(self, forums: list[discord.ForumChannel]):
        super().__init__()
        self.forum_select = discord.ui.Select(
            placeholder="どのフォーラムに作りますか？",
            options=[discord.SelectOption(label=f.name[:100], value=str(f.id)) for f in forums[:25]],
            min_values=1,
            max_values=1,
        )
        self.name_input = discord.ui.TextInput(placeholder="例：ポーカー", max_length=80)
        self.body_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder="どんな話をする枠か、どんな人に来てほしいかなど",
            max_length=1500,
        )
        self.add_item(discord.ui.Label(text="フォーラム", component=self.forum_select))
        self.add_item(discord.ui.Label(text="枠の名前", component=self.name_input))
        self.add_item(
            discord.ui.Label(
                text="説明",
                description="承認されると、この文章が枠の最初の投稿になります",
                component=self.body_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        forum = guild.get_channel(int(self.forum_select.values[0])) if self.forum_select.values else None
        admin_channel = _admin_channel(guild)
        if not isinstance(forum, discord.ForumChannel) or admin_channel is None:
            await interaction.followup.send("送れませんでした。運営に連絡してください。", ephemeral=True)
            return

        name = " ".join(self.name_input.value.split())
        if any(t.name == name for t in forum.threads):
            await interaction.followup.send(
                f"{forum.mention} には、すでに「{name}」という枠があります。名前を変えて送ってください。", ephemeral=True
            )
            return

        user = interaction.user
        embed = discord.Embed(title=f"📝 新しい枠のリクエスト：{name}", description=self.body_input.value.strip())
        embed.add_field(name="フォーラム", value=forum.mention, inline=True)
        embed.add_field(name="リクエストした人", value=user.mention, inline=True)
        embed.add_field(name="枠の名前", value=name, inline=False)
        embed.set_footer(text=f"{_STATUS_PENDING}｜{user.id}")
        embed.set_thumbnail(url=user.display_avatar.url)

        await admin_channel.send(
            embed=embed,
            view=_decision_view(user.id, forum.id),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await interaction.followup.send(
            f"リクエストを送りました。運営が確認して、承認されると {forum.mention} に「{name}」が作られます。"
            "結果はDMでもお知らせします。",
            ephemeral=True,
        )

    async def on_error(self, interaction, error):
        await _report_error(interaction, error)


# ---------------------------------------------------------------------------
# 管理者側：承認・却下
# ---------------------------------------------------------------------------

def _decision_view(user_id: int, forum_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    approve = ApproveRequestButton(user_id, forum_id)
    reject = RejectRequestButton(user_id, forum_id)
    for item in (approve, reject):
        item.item.disabled = disabled
        view.add_item(item)
    return view


def _parse_request(message: discord.Message) -> tuple[str, str] | None:
    """管理者向けメッセージの埋め込みから (枠の名前, 説明) を取り出す。"""
    if not message.embeds:
        return None
    embed = message.embeds[0]
    name = next((f.value for f in embed.fields if f.name == "枠の名前"), None)
    if not name:
        return None
    return name, embed.description or ""


async def _close_request(message: discord.Message, result: str, user_id: int, forum_id: int):
    embed = message.embeds[0]
    embed.set_footer(text=f"{result}｜{user_id}")
    embed.colour = discord.Colour.green() if result.startswith("承認") else discord.Colour.red()
    await message.edit(embed=embed, view=_decision_view(user_id, forum_id, disabled=True))


async def _forum_webhook(forum: discord.ForumChannel) -> discord.Webhook:
    for webhook in await forum.webhooks():
        if webhook.name == WEBHOOK_NAME and webhook.token:
            return webhook
    return await forum.create_webhook(name=WEBHOOK_NAME)


def _webhook_username(name: str) -> str:
    for banned in ("discord", "Discord", "DISCORD", "clyde", "Clyde", "CLYDE"):
        name = name.replace(banned, banned[0] + "​" + banned[1:])
    return (name or "（名前なし）")[:80]


class ApproveRequestButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"forum_req_ok:(?P<uid>\d+):(?P<fid>\d+)"
):
    def __init__(self, user_id: int, forum_id: int):
        super().__init__(
            discord.ui.Button(
                label="承認して作成", style=discord.ButtonStyle.success, custom_id=f"forum_req_ok:{user_id}:{forum_id}"
            )
        )
        self.user_id = user_id
        self.forum_id = forum_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["uid"]), int(match["fid"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
            await interaction.response.send_message("管理者だけが操作できます。", ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        message = interaction.message
        parsed = _parse_request(message)
        if parsed is None or not message.embeds[0].footer.text.startswith(_STATUS_PENDING):
            await interaction.response.send_message("このリクエストはすでに対応済みです。", ephemeral=True)
            return
        await interaction.response.defer()
        name, body = parsed
        guild = interaction.guild
        forum = guild.get_channel(self.forum_id)
        if not isinstance(forum, discord.ForumChannel):
            await interaction.followup.send("フォーラムが見つかりませんでした（削除された可能性があります）。", ephemeral=True)
            return
        requester = guild.get_member(self.user_id)
        if requester is None:
            try:
                requester = await guild.fetch_member(self.user_id)
            except discord.HTTPException:
                await interaction.followup.send("リクエストした人がサーバーにいないため、作成しませんでした。", ephemeral=True)
                return

        # リクエストした人の名前とアイコンで、フォーラムに枠（投稿）を作る
        webhook = await _forum_webhook(forum)
        starter = await webhook.send(
            content=body or name,
            thread_name=name[:100],
            username=_webhook_username(requester.display_name),
            avatar_url=requester.display_avatar.url,
            allowed_mentions=discord.AllowedMentions.none(),
            wait=True,
        )
        # フォーラムの投稿は、最初のメッセージのIDとスレッドのIDが同じ
        thread = guild.get_thread(starter.id)
        if thread is None:
            try:
                thread = await guild.fetch_channel(starter.id)
            except discord.HTTPException:
                thread = None
        thread_text = thread.mention if thread is not None else f"「{name}」"
        if thread is not None:
            try:
                await thread.add_user(requester)
            except discord.HTTPException:
                pass

        await _close_request(message, f"承認（{interaction.user.display_name}）", self.user_id, self.forum_id)

        notice = _notice_channel(guild)
        if notice is not None:
            await notice.send(
                f"🎉 {requester.mention} さんが、{forum.mention} に新しい枠を作りました！\n"
                f"「{thread_text}」\n\n**概要**\n{body[:1500]}",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        try:
            await requester.send(
                f"リクエストした枠「{name}」が承認され、{forum.mention} に作られました！ {thread_text}"
            )
        except discord.HTTPException:
            pass
        await interaction.followup.send(f"✅ 「{name}」を作成しました：{thread_text}", ephemeral=True)


class RejectRequestButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"forum_req_ng:(?P<uid>\d+):(?P<fid>\d+)"
):
    def __init__(self, user_id: int, forum_id: int):
        super().__init__(
            discord.ui.Button(
                label="却下", style=discord.ButtonStyle.danger, custom_id=f"forum_req_ng:{user_id}:{forum_id}"
            )
        )
        self.user_id = user_id
        self.forum_id = forum_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["uid"]), int(match["fid"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
            await interaction.response.send_message("管理者だけが操作できます。", ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        message = interaction.message
        if _parse_request(message) is None or not message.embeds[0].footer.text.startswith(_STATUS_PENDING):
            await interaction.response.send_message("このリクエストはすでに対応済みです。", ephemeral=True)
            return
        await interaction.response.send_modal(RejectReasonModal(message, self.user_id, self.forum_id))


class RejectReasonModal(discord.ui.Modal, title="リクエストを却下"):
    reason = discord.ui.TextInput(
        label="理由（任意。リクエストした人にDMで伝わります）",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
        placeholder="例：似た枠「〇〇」がすでにあるため、そちらをご利用ください",
    )

    def __init__(self, message: discord.Message, user_id: int, forum_id: int):
        super().__init__()
        self.message = message
        self.user_id = user_id
        self.forum_id = forum_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        parsed = _parse_request(self.message)
        name = parsed[0] if parsed else "（不明）"
        await _close_request(self.message, f"却下（{interaction.user.display_name}）", self.user_id, self.forum_id)
        member = interaction.guild.get_member(self.user_id)
        if member is not None:
            text = f"リクエストした枠「{name}」は、今回は作成を見送らせていただきました。"
            if self.reason.value.strip():
                text += f"\n運営より：{self.reason.value.strip()}"
            try:
                await member.send(text)
            except discord.HTTPException:
                pass
        await interaction.followup.send(f"「{name}」のリクエストを却下しました。", ephemeral=True)

    async def on_error(self, interaction, error):
        await _report_error(interaction, error)


DYNAMIC_ITEMS = (ApproveRequestButton, RejectRequestButton)


# ---------------------------------------------------------------------------
# 設定（管理者コマンドから呼ぶ）
# ---------------------------------------------------------------------------

async def restrict_forum_posts(guild: discord.Guild) -> tuple[list[str], list[str]]:
    """
    コミュニティのフォーラムで、会員が新しい投稿（枠）を作れないようにする。
    投稿の中への返信はこれまでどおりできる。botには投稿を作る権限（Webhook）を付ける。
    戻り値は (設定したフォーラム, 失敗したもの)。
    """
    roles = [
        r for r in (
            guild.default_role,
            discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME),
            discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME),
            discord.utils.get(guild.roles, name=PREMIUM_ROLE_NAME) if PREMIUM_ROLE_NAME else None,
        ) if r is not None
    ]
    done, failed = [], []
    for forum in community_forums(guild):
        try:
            bot_overwrite = forum.overwrites_for(guild.me)
            bot_overwrite.update(
                view_channel=True, send_messages=True, send_messages_in_threads=True,
                manage_threads=True, manage_webhooks=True, read_message_history=True,
            )
            await forum.set_permissions(guild.me, overwrite=bot_overwrite)
            for role in roles:
                overwrite = forum.overwrites_for(role)
                # フォーラムの「メッセージを送信」＝新しい投稿を作る権限
                overwrite.update(send_messages=False, send_messages_in_threads=True)
                await forum.set_permissions(role, overwrite=overwrite)
            done.append(forum.mention)
        except discord.HTTPException as error:
            failed.append(f"{forum.mention}（{error}）")
    return done, failed


PANEL_TEXT = (
    "📝 **新しい枠のリクエスト**\n"
    "コミュニティに新しい枠（例：「ポーカー」）を作りたいときは、下のボタンからリクエストしてください。\n"
    "運営が確認して承認すると、あなたの名前で枠が作られ、チャンネル紹介でお知らせされます。\n"
    "※リクエストできるのは、コミュニティ作成プランの方です"
)
