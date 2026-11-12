"""
コミュニティ・発信チャンネルを、「リクエスト → 管理者が承認」で作る仕組み。

2つのカテゴリで、作るものが違う。
- 「コミュニティ」カテゴリ（COMMUNITY_CATEGORY_NAME）
    「趣味」「在宅ワーク」などのフォーラムの中に、新しい「枠」（投稿）を作る。
    リクエストでは、どのフォーラムに作るかを選び、枠の名前と説明を入力する。
    承認すると、リクエストした人の名前とアイコンで枠が作られ、同じカテゴリの「チャンネル紹介」で告知する。
- 「発信チャンネル」カテゴリ（OWNER_FORUM_CATEGORY_NAME）
    リクエストした人が主になる、新しいフォーラムチャンネルを作る。
    リクエストでは、チャンネルの名前と説明（投稿ガイドラインになる）を入力する。
    承認すると、カテゴリの中にフォーラムができ、主（リクエストした人）だけが投稿を作れる。
    告知は、既存の仕組み（概要と主がそろったら「チャンネル紹介」に自動で告知）がそのまま行う。

リクエストを送れるのは、4000円プランのロール（PREMIUM_ROLE_NAME、標準は「⭐️」）の人と管理者だけ。
承認待ちのリクエストは1人1件まで。リクエストは、管理者だけが見られる「リクエスト」チャンネルに届く。
リクエストの中身は管理者向けメッセージに書いておくので、botが再起動しても承認・却下ボタンはそのまま使える。
"""

from __future__ import annotations

import os
import re

import discord

COMMUNITY_CATEGORY_NAME = os.getenv("COMMUNITY_CATEGORY_NAME", "コミュニティ")
OWNER_FORUM_CATEGORY_NAME = os.getenv("OWNER_FORUM_CATEGORY_NAME", "発信チャンネル")
REQUEST_ADMIN_CHANNEL_NAME = os.getenv("FORUM_REQUEST_CHANNEL_NAME", "リクエスト")
NOTICE_CHANNEL_NAME = os.getenv("COMMUNITY_NOTICE_CHANNEL_NAME", "チャンネル紹介")
PREMIUM_ROLE_NAME = os.getenv("PREMIUM_ROLE_NAME", "⭐️").strip()
ADMIN_ROLE_NAME = os.getenv("ADMIN_ROLE_NAME", "管理者")
MEMBER_ROLE_NAME = os.getenv("MEMBER_ROLE_NAME", "会員")
UNVERIFIED_ROLE_NAME = os.getenv("UNVERIFIED_ROLE_NAME", "未登録")
WEBHOOK_NAME = "枠リクエスト"

KIND_POST = "p"     # コミュニティのフォーラムの中に「枠」（投稿）を作る
KIND_CHANNEL = "c"  # 発信チャンネルのカテゴリに、新しいフォーラムチャンネルを作る
_KIND_LABEL = {KIND_POST: "コミュニティの枠", KIND_CHANNEL: "発信チャンネル"}

_STATUS_PENDING = "承認待ち"


# ---------------------------------------------------------------------------
# 共通
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    # 絵文字の「⭐」と「⭐️」（見えない文字 U+FE0F の有無）を同じものとして扱う
    return str(text or "").replace("️", "").strip()


def _category(guild: discord.Guild, name: str) -> discord.CategoryChannel | None:
    return next((c for c in guild.categories if _norm(c.name) == _norm(name)), None)


def kind_for_category(category: discord.CategoryChannel | None) -> str | None:
    if category is None:
        return None
    if _norm(category.name) == _norm(COMMUNITY_CATEGORY_NAME):
        return KIND_POST
    if _norm(category.name) == _norm(OWNER_FORUM_CATEGORY_NAME):
        return KIND_CHANNEL
    return None


def community_forums(guild: discord.Guild) -> list[discord.ForumChannel]:
    """「コミュニティ」カテゴリのフォーラム（趣味・在宅ワークなど、枠を入れる側）。"""
    category = _category(guild, COMMUNITY_CATEGORY_NAME)
    return [c for c in category.channels if isinstance(c, discord.ForumChannel)] if category else []


def owner_forums(guild: discord.Guild) -> list[discord.ForumChannel]:
    """「発信チャンネル」カテゴリのフォーラム（主がいるチャンネル）。"""
    category = _category(guild, OWNER_FORUM_CATEGORY_NAME)
    return [c for c in category.channels if isinstance(c, discord.ForumChannel)] if category else []


def _is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or any(r.name == ADMIN_ROLE_NAME for r in member.roles)


def _premium_role(guild: discord.Guild) -> discord.Role | None:
    return next((r for r in guild.roles if _norm(r.name) == _norm(PREMIUM_ROLE_NAME)), None)


def _can_request(member: discord.Member) -> bool:
    if _is_admin(member):
        return True
    return any(_norm(r.name) == _norm(PREMIUM_ROLE_NAME) for r in member.roles)


def _admin_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """管理者だけが見られる「リクエスト」チャンネル（後ろに絵文字が付いていても見つける）。"""
    exact = discord.utils.get(guild.text_channels, name=REQUEST_ADMIN_CHANNEL_NAME)
    if exact is not None:
        return exact
    return next((c for c in guild.text_channels if c.name.startswith(REQUEST_ADMIN_CHANNEL_NAME)), None)


def _admin_mention(guild: discord.Guild) -> tuple[str | None, discord.AllowedMentions]:
    """リクエストが届いたことを知らせる、管理者ロールへのメンション（ロールが無ければメンションなし）。"""
    role = discord.utils.find(lambda r: _norm(r.name) == _norm(ADMIN_ROLE_NAME), guild.roles)
    if role is None:
        print(f"「{ADMIN_ROLE_NAME}」という名前のロールが見つからないため、リクエストの通知でメンションできませんでした。")
        return None, discord.AllowedMentions.none()
    return f"{role.mention} 新しいリクエストが届きました。ご確認ください。", discord.AllowedMentions(
        everyone=False, users=False, roles=[role]
    )


def _notice_channel(category: discord.CategoryChannel | None) -> discord.TextChannel | None:
    """カテゴリの中の「チャンネル紹介」。"""
    if category is None:
        return None
    return next((c for c in category.text_channels if c.name.startswith(NOTICE_CHANNEL_NAME)), None)


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
        footer = message.embeds[0].footer.text if message.embeds[0].footer else ""
        if footer == f"{_STATUS_PENDING}｜{user_id}":
            return True
    return False


# ---------------------------------------------------------------------------
# 会員側：リクエストを送る
# ---------------------------------------------------------------------------

async def _check_can_open(interaction: discord.Interaction) -> discord.TextChannel | None:
    member = interaction.user
    if not isinstance(member, discord.Member) or not _can_request(member):
        await interaction.response.send_message(
            "作成のリクエストは、コミュニティ作成プラン（⭐️）の方だけが送れます。", ephemeral=True
        )
        return None
    admin_channel = _admin_channel(interaction.guild)
    if admin_channel is None:
        await interaction.response.send_message(
            f"「{REQUEST_ADMIN_CHANNEL_NAME}」チャンネルが見つかりません。運営に連絡してください。", ephemeral=True
        )
        return None
    if await _has_pending_request(admin_channel, member.id):
        await interaction.response.send_message(
            "承認待ちのリクエストがすでにあります。結果が届くまでお待ちください。", ephemeral=True
        )
        return None
    return admin_channel


class PostRequestPanel(discord.ui.View):
    """「コミュニティ」の作成リクエストのボタン（再起動しても押せるよう custom_id を固定）。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="コミュニティの枠をリクエスト",
        style=discord.ButtonStyle.primary,
        emoji="📝",
        custom_id="persistent_request_community_post",
    )
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await _check_can_open(interaction) is None:
            return
        forums = community_forums(interaction.guild)
        if not forums:
            await interaction.response.send_message(
                f"「{COMMUNITY_CATEGORY_NAME}」カテゴリにフォーラムが見つかりません。運営に連絡してください。",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(PostRequestModal(forums))

    async def on_error(self, interaction, error, item):
        await _report_error(interaction, error)


class ChannelRequestPanel(discord.ui.View):
    """「発信チャンネル」の作成リクエストのボタン。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="発信チャンネルをリクエスト",
        style=discord.ButtonStyle.primary,
        emoji="📝",
        custom_id="persistent_request_owner_channel",
    )
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await _check_can_open(interaction) is None:
            return
        if _category(interaction.guild, OWNER_FORUM_CATEGORY_NAME) is None:
            await interaction.response.send_message(
                f"「{OWNER_FORUM_CATEGORY_NAME}」カテゴリが見つかりません。運営に連絡してください。", ephemeral=True
            )
            return
        await interaction.response.send_modal(ChannelRequestModal())

    async def on_error(self, interaction, error, item):
        await _report_error(interaction, error)


def _request_embed(kind: str, user: discord.abc.User, name: str, body: str, where: str) -> discord.Embed:
    embed = discord.Embed(title=f"📝 {_KIND_LABEL[kind]}の作成リクエスト：{name}", description=body)
    embed.add_field(name="作る場所", value=where, inline=True)
    embed.add_field(name="リクエストした人", value=user.mention, inline=True)
    embed.add_field(name="名前", value=name, inline=False)
    embed.set_footer(text=f"{_STATUS_PENDING}｜{user.id}")
    embed.set_thumbnail(url=user.display_avatar.url)
    return embed


class PostRequestModal(discord.ui.Modal, title="コミュニティの枠をリクエスト"):
    def __init__(self, forums: list[discord.ForumChannel]):
        super().__init__()
        self.forum_select = discord.ui.Select(
            placeholder="どこに作りますか？（例：趣味・在宅ワーク）",
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
        self.add_item(discord.ui.Label(text="作る場所", component=self.forum_select))
        self.add_item(discord.ui.Label(text="枠の名前", component=self.name_input))
        self.add_item(
            discord.ui.Label(
                text="説明", description="承認されると、この文章が枠の最初の投稿になります", component=self.body_input
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
        content, mentions = _admin_mention(guild)
        await admin_channel.send(
            content=content,
            embed=_request_embed(KIND_POST, interaction.user, name, self.body_input.value.strip(), forum.mention),
            view=_decision_view(KIND_POST, interaction.user.id, forum.id),
            allowed_mentions=mentions,
        )
        await interaction.followup.send(
            f"リクエストを送りました。運営が承認すると、{forum.mention} に「{name}」が作られます。結果はDMでもお知らせします。",
            ephemeral=True,
        )

    async def on_error(self, interaction, error):
        await _report_error(interaction, error)


class ChannelRequestModal(discord.ui.Modal, title="発信チャンネルをリクエスト"):
    name_input = discord.ui.TextInput(label="チャンネルの名前", placeholder="例：筋トレ", max_length=80)
    body_input = discord.ui.TextInput(
        label="説明（チャンネルの投稿ガイドラインになります）",
        style=discord.TextStyle.paragraph,
        placeholder="どんな発信をするチャンネルか、どんな人に見てほしいかなど",
        max_length=1500,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        category = _category(guild, OWNER_FORUM_CATEGORY_NAME)
        admin_channel = _admin_channel(guild)
        if category is None or admin_channel is None:
            await interaction.followup.send("送れませんでした。運営に連絡してください。", ephemeral=True)
            return
        name = " ".join(self.name_input.value.split())
        if any(c.name == name for c in category.channels):
            await interaction.followup.send(
                f"「{name}」という名前のチャンネルがすでにあります。名前を変えて送ってください。", ephemeral=True
            )
            return
        content, mentions = _admin_mention(guild)
        await admin_channel.send(
            content=content,
            embed=_request_embed(
                KIND_CHANNEL, interaction.user, name, self.body_input.value.strip(), f"「{category.name}」カテゴリ"
            ),
            view=_decision_view(KIND_CHANNEL, interaction.user.id, category.id),
            allowed_mentions=mentions,
        )
        await interaction.followup.send(
            f"リクエストを送りました。運営が承認すると、「{category.name}」に「{name}」チャンネルが作られます。"
            "結果はDMでもお知らせします。",
            ephemeral=True,
        )

    async def on_error(self, interaction, error):
        await _report_error(interaction, error)


# ---------------------------------------------------------------------------
# 管理者側：承認・却下
# ---------------------------------------------------------------------------

def _decision_view(kind: str, user_id: int, target_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for item in (ApproveRequestButton(kind, user_id, target_id), RejectRequestButton(kind, user_id, target_id)):
        item.item.disabled = disabled
        view.add_item(item)
    return view


def _parse_request(message: discord.Message) -> tuple[str, str] | None:
    """管理者向けメッセージの埋め込みから (名前, 説明) を取り出す。承認待ちでなければ None。"""
    if not message.embeds:
        return None
    embed = message.embeds[0]
    if not embed.footer or not str(embed.footer.text).startswith(_STATUS_PENDING):
        return None
    name = next((f.value for f in embed.fields if f.name == "名前"), None)
    if not name:
        return None
    return name, embed.description or ""


async def _close_request(message: discord.Message, result: str, kind: str, user_id: int, target_id: int):
    embed = message.embeds[0]
    embed.set_footer(text=f"{result}｜{user_id}")
    embed.colour = discord.Colour.green() if result.startswith("承認") else discord.Colour.red()
    await message.edit(embed=embed, view=_decision_view(kind, user_id, target_id, disabled=True))


async def _forum_webhook(forum: discord.ForumChannel) -> discord.Webhook:
    for webhook in await forum.webhooks():
        if webhook.name == WEBHOOK_NAME and webhook.token:
            return webhook
    return await forum.create_webhook(name=WEBHOOK_NAME)


def _webhook_username(name: str) -> str:
    for banned in ("discord", "Discord", "DISCORD", "clyde", "Clyde", "CLYDE"):
        name = name.replace(banned, banned[0] + "​" + banned[1:])
    return (name or "（名前なし）")[:80]


async def _create_post(guild, forum: discord.ForumChannel, requester: discord.Member, name: str, body: str) -> str:
    """コミュニティのフォーラムに、リクエストした人の名前とアイコンで枠（投稿）を作る。リンクの文字を返す。"""
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
    if thread is not None:
        try:
            await thread.add_user(requester)
        except discord.HTTPException:
            pass
    link = thread.mention if thread is not None else f"「{name}」"

    notice = _notice_channel(forum.category)
    if notice is not None:
        await notice.send(
            f"🎉 {requester.mention} さんが、{forum.mention} に新しい枠を作りました！\n"
            f"「{link}」\n\n**概要**\n{body[:1500]}",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
    return link


async def _create_owner_forum(
    guild: discord.Guild, category: discord.CategoryChannel, requester: discord.Member, name: str, body: str
) -> str:
    """
    発信チャンネルのカテゴリに、リクエストした人が主のフォーラムを作る。
    - 会員など：見る・返信はできるが、新しい投稿は作れない
    - 主：新しい投稿を作れて、自分のフォーラムの投稿を管理できる。チャンネル名・説明なども変えられる（チャンネル管理）
    説明は投稿ガイドライン（トピック）に入る。告知は既存の仕組みが「チャンネル紹介」で自動で行う。
    """
    overwrites = dict(category.overwrites)  # カテゴリの見える・見えないの設定を引き継ぐ
    for role in (
        guild.default_role,
        discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME),
        discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME),
        _premium_role(guild),
    ):
        if role is None:
            continue
        overwrite = overwrites.get(role, discord.PermissionOverwrite())
        overwrite.update(send_messages=False, send_messages_in_threads=True)
        overwrites[role] = overwrite
    overwrites[guild.me] = discord.PermissionOverwrite(
        view_channel=True, send_messages=True, send_messages_in_threads=True,
        manage_threads=True, manage_messages=True, manage_channels=True, read_message_history=True,
    )
    overwrites[requester] = discord.PermissionOverwrite(
        view_channel=True, send_messages=True, send_messages_in_threads=True,
        manage_threads=True, manage_messages=True, attach_files=True, embed_links=True, read_message_history=True,
        manage_channels=True,  # 主が自分でチャンネル名や投稿ガイドラインを変えられるようにする
    )
    forum = await category.create_forum(name=name[:100], topic=body[:4096] or None, overwrites=overwrites)
    return forum.mention


class ApproveRequestButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"req_ok:(?P<kind>[pc]):(?P<uid>\d+):(?P<tid>\d+)",
):
    def __init__(self, kind: str, user_id: int, target_id: int):
        super().__init__(
            discord.ui.Button(
                label="承認して作成",
                style=discord.ButtonStyle.success,
                custom_id=f"req_ok:{kind}:{user_id}:{target_id}",
            )
        )
        self.kind = kind
        self.user_id = user_id
        self.target_id = target_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["kind"], int(match["uid"]), int(match["tid"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
            await interaction.response.send_message("管理者だけが操作できます。", ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        message = interaction.message
        parsed = _parse_request(message)
        if parsed is None:
            await interaction.response.send_message("このリクエストはすでに対応済みです。", ephemeral=True)
            return
        await interaction.response.defer()
        name, body = parsed
        guild = interaction.guild
        requester = guild.get_member(self.user_id)
        if requester is None:
            try:
                requester = await guild.fetch_member(self.user_id)
            except discord.HTTPException:
                await interaction.followup.send("リクエストした人がサーバーにいないため、作成しませんでした。", ephemeral=True)
                return

        target = guild.get_channel(self.target_id)
        try:
            if self.kind == KIND_POST:
                if not isinstance(target, discord.ForumChannel):
                    raise RuntimeError("作る場所のフォーラムが見つかりません（削除された可能性があります）")
                link = await _create_post(guild, target, requester, name, body)
                where = target.mention
            else:
                if not isinstance(target, discord.CategoryChannel):
                    raise RuntimeError("発信チャンネルのカテゴリが見つかりません")
                link = await _create_owner_forum(guild, target, requester, name, body)
                where = f"「{target.name}」"
        except (RuntimeError, discord.HTTPException) as error:
            await interaction.followup.send(f"作成できませんでした：{error}", ephemeral=True)
            return

        await _close_request(message, f"承認（{interaction.user.display_name}）", self.kind, self.user_id, self.target_id)
        try:
            await requester.send(f"リクエストした「{name}」が承認され、{where} に作られました！ {link}")
        except discord.HTTPException:
            pass
        await interaction.followup.send(f"✅ 「{name}」を作成しました：{link}", ephemeral=True)


class RejectRequestButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"req_ng:(?P<kind>[pc]):(?P<uid>\d+):(?P<tid>\d+)",
):
    def __init__(self, kind: str, user_id: int, target_id: int):
        super().__init__(
            discord.ui.Button(
                label="却下", style=discord.ButtonStyle.danger, custom_id=f"req_ng:{kind}:{user_id}:{target_id}"
            )
        )
        self.kind = kind
        self.user_id = user_id
        self.target_id = target_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["kind"], int(match["uid"]), int(match["tid"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
            await interaction.response.send_message("管理者だけが操作できます。", ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        if _parse_request(interaction.message) is None:
            await interaction.response.send_message("このリクエストはすでに対応済みです。", ephemeral=True)
            return
        await interaction.response.send_modal(
            RejectReasonModal(interaction.message, self.kind, self.user_id, self.target_id)
        )


class RejectReasonModal(discord.ui.Modal, title="リクエストを却下"):
    reason = discord.ui.TextInput(
        label="理由（任意。リクエストした人にDMで伝わります）",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
        placeholder="例：似た枠「〇〇」がすでにあるため、そちらをご利用ください",
    )

    def __init__(self, message: discord.Message, kind: str, user_id: int, target_id: int):
        super().__init__()
        self.message = message
        self.kind = kind
        self.user_id = user_id
        self.target_id = target_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        parsed = _parse_request(self.message)
        name = parsed[0] if parsed else "（不明）"
        await _close_request(self.message, f"却下（{interaction.user.display_name}）", self.kind, self.user_id, self.target_id)
        member = interaction.guild.get_member(self.user_id)
        if member is not None:
            text = f"リクエストした「{name}」は、今回は作成を見送らせていただきました。"
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
PERSISTENT_VIEWS = (PostRequestPanel, ChannelRequestPanel)


# ---------------------------------------------------------------------------
# 設定（管理者コマンドから呼ぶ）
# ---------------------------------------------------------------------------

async def grant_owner_manage_channels(guild: discord.Guild) -> tuple[list[str], list[str]]:
    """
    すでにある発信チャンネルの主に「チャンネル管理」を付ける（チャンネル名などを自分で変えられるようにする）。
    主は「個人に投稿の管理（manage_threads）が付いているメンバー」として見分ける。
    """
    done, failed = [], []
    for forum in owner_forums(guild):
        owners = [
            target for target, overwrite in forum.overwrites.items()
            if isinstance(target, discord.Member) and target.id != guild.me.id and overwrite.manage_threads
        ]
        for owner in owners:
            overwrite = forum.overwrites_for(owner)
            if overwrite.manage_channels:
                continue
            overwrite.update(manage_channels=True)
            try:
                await forum.set_permissions(owner, overwrite=overwrite, reason="発信チャンネルの主にチャンネル管理を付与")
                done.append(f"{forum.mention}（{owner.display_name}）")
            except discord.HTTPException:
                failed.append(f"{forum.mention}（{owner.display_name}）")
    return done, failed


async def restrict_new_posts(guild: discord.Guild, forums: list[discord.ForumChannel]) -> tuple[list[str], list[str]]:
    """
    フォーラムで、会員などが新しい投稿を作れないようにする（返信はできる）。
    発信チャンネルの主は、個人に付いている許可が優先されるので、これまでどおり投稿を作れる。
    """
    roles = [
        r for r in (
            guild.default_role,
            discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME),
            discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME),
            _premium_role(guild),
        ) if r is not None
    ]
    done, failed = [], []
    for forum in forums:
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


POST_PANEL_TEXT = (
    "📝 **コミュニティの枠の作成リクエスト**\n"
    "「趣味」「在宅ワーク」などに新しい枠（例：「ポーカー」）を作りたいときは、下のボタンからリクエストしてください。\n"
    "作る場所・枠の名前・説明を入力して送ると、運営が確認します。承認されると、あなたの名前で枠が作られ、チャンネル紹介でお知らせします。\n"
    "※リクエストできるのは、コミュニティ作成プラン（⭐️）の方です"
)
CHANNEL_PANEL_TEXT = (
    "📝 **発信チャンネルの作成リクエスト**\n"
    "あなたが主になって発信するチャンネルを作りたいときは、下のボタンからリクエストしてください。\n"
    "チャンネルの名前と説明を入力して送ると、運営が確認します。承認されると、あなたが主のチャンネルが作られ、チャンネル紹介でお知らせします。\n"
    "※リクエストできるのは、コミュニティ作成プラン（⭐️）の方です"
)
