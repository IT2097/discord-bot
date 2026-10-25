"""
「問い合わせ」チャンネルの仕組み。

チャンネルに「📩 運営に問い合わせる」ボタンを1つ置いておく。
押すとフォーム（件名・内容）が開き、送ると「問い合わせた会員」と「運営（サーバーオーナー）」だけが
入れるプライベートスレッドができる。そのスレッドの中で、そのままやり取りを続けられる。

「✅ 解決済みにする」を押すと、スレッド名の頭に ✅ を付けてアーカイブ・ロックする。
スレッドは削除しないので、運営はあとから「スレッド一覧 → アーカイブ済み」で見返せる。

運営として追加する人は、標準ではサーバーオーナー。
環境変数 INQUIRY_STAFF_IDS に DiscordID をカンマ区切りで入れると、その人たちに変えられる。
"""

from __future__ import annotations

import os

import discord

INQUIRY_CHANNEL_NAME = os.getenv("INQUIRY_CHANNEL_NAME", "問い合わせ")
_STAFF_IDS = [s.strip() for s in os.getenv("INQUIRY_STAFF_IDS", "").split(",") if s.strip()]

# 問い合わせチャンネルでbotに必要な権限（プライベートスレッドを作ってアーカイブするため）
BOT_PERMISSIONS = dict(
    view_channel=True,
    send_messages=True,
    read_message_history=True,
    embed_links=True,
    manage_messages=True,
    create_private_threads=True,
    send_messages_in_threads=True,
    manage_threads=True,
)


async def _staff_members(guild: discord.Guild) -> list[discord.Member]:
    """スレッドに追加する運営（標準はサーバーオーナー）。"""
    ids = _STAFF_IDS or [str(guild.owner_id)]
    members = []
    for staff_id in ids:
        try:
            member = guild.get_member(int(staff_id)) or await guild.fetch_member(int(staff_id))
        except (ValueError, discord.HTTPException):
            continue
        members.append(member)
    return members


async def setup_inquiry_channel(channel: discord.TextChannel, member_role: discord.Role | None):
    """
    問い合わせチャンネルの権限を設定する。
    - @everyone：見えない
    - 会員ロール：チャンネルは見えるが、チャンネルへの投稿やスレッドの作成はできない。
      自分が入っているスレッド（自分の問い合わせ）の中では返信できる
    """
    guild = channel.guild
    await channel.set_permissions(guild.me, **BOT_PERMISSIONS)
    await channel.set_permissions(guild.default_role, view_channel=False)
    if member_role is not None:
        await channel.set_permissions(
            member_role,
            view_channel=True,
            read_message_history=True,
            send_messages=False,
            add_reactions=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=True,
            attach_files=True,
        )


async def _report_error(interaction: discord.Interaction, error: Exception):
    import traceback

    traceback.print_exception(type(error), error, error.__traceback__)
    message = f"処理中にエラーが発生しました：{error}\nお手数ですが運営に直接連絡してください。"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------------
# チャンネルに置くボタンとフォーム
# ---------------------------------------------------------------------------

class InquiryPanel(discord.ui.View):
    """問い合わせチャンネルに置くボタン（再起動しても押せるよう custom_id を固定）。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="運営に問い合わせる",
        style=discord.ButtonStyle.primary,
        emoji="📩",
        custom_id="persistent_inquiry_open_button",
    )
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(InquiryModal())

    async def on_error(self, interaction, error, item):
        await _report_error(interaction, error)


class InquiryModal(discord.ui.Modal, title="運営に問い合わせる"):
    subject = discord.ui.TextInput(label="件名", placeholder="例：マッチング申請について", max_length=50)
    body = discord.ui.TextInput(
        label="内容",
        style=discord.TextStyle.paragraph,
        placeholder="問い合わせたい内容を書いてください",
        max_length=1500,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = interaction.channel
        if isinstance(channel, discord.Thread):
            channel = channel.parent
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("問い合わせチャンネルから送ってください。", ephemeral=True)
            return

        user = interaction.user
        subject = " ".join(self.subject.value.split())
        try:
            thread = await channel.create_thread(
                name=f"📩 {getattr(user, 'display_name', user.name)}：{subject}"[:100],
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=10080,  # 7日間やり取りがなければ自動で一覧から隠れる（返信すれば戻る）
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "botにスレッドを作る権限がないため、送れませんでした。運営に直接連絡してください。",
                ephemeral=True,
            )
            return

        await thread.add_user(user)
        staff = await _staff_members(interaction.guild)
        for member in staff:
            try:
                await thread.add_user(member)
            except discord.HTTPException:
                pass

        staff_mentions = " ".join(m.mention for m in staff)
        await thread.send(
            f"{user.mention} {staff_mentions}\n"
            f"📩 **問い合わせ：{subject}**\n\n{self.body.value.strip()}\n\n"
            "─────────────\n"
            "このスレッドは、問い合わせた方と運営だけに表示されています。\n"
            "このままスレッドの中でやり取りできます（画像も送れます）。\n"
            "解決したら「✅ 解決済みにする」を押してください。",
            view=InquiryResolveView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )

        await interaction.followup.send(
            f"問い合わせを送りました。運営からの返事は {thread.mention} に届きます。",
            ephemeral=True,
        )

    async def on_error(self, interaction, error):
        await _report_error(interaction, error)


class InquiryResolveView(discord.ui.View):
    """問い合わせスレッドの「解決済みにする」ボタン（どのスレッドでも同じボタンを使い回す）。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="解決済みにする",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="persistent_inquiry_resolve_button",
    )
    async def resolve(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message("問い合わせスレッドの中で押してください。", ephemeral=True)
            return

        button.disabled = True
        await interaction.response.edit_message(view=self)
        await thread.send(
            f"✅ {interaction.user.mention} さんが解決済みにしました。このスレッドは一覧から非表示になります。\n"
            "（記録として残っています。また問い合わせたいときは、もう一度ボタンから送ってください）",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        name = thread.name
        if not name.startswith("✅"):
            name = ("✅ " + name.removeprefix("📩 "))[:100]
        try:
            # アーカイブ＝一覧から非表示、ロック＝運営以外は書き込めない（削除はしない）
            await thread.edit(name=name, archived=True, locked=True)
        except discord.HTTPException:
            try:
                await thread.edit(archived=True)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction, error, item):
        await _report_error(interaction, error)


PANEL_TEXT = (
    "📩 **運営への問い合わせ**\n"
    "下のボタンを押して、件名と内容を書いて送ってください。\n"
    "あなたと運営だけが見られる専用スレッドが作られ、そこで運営とやり取りできます。\n"
    "ほかの会員には、問い合わせの内容は見えません。"
)
