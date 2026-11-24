"""
紹介制度の Discord 側の画面（「紹介リンクを発行・確認」ボタンと、メールアドレスの入力欄）。
仕組みそのものは referral.py。
"""

from __future__ import annotations

import asyncio

import discord

import referral

PANEL_TEXT = (
    "🎁 **リジェコミュニティ 紹介制度**\n"
    "下のボタンから、あなた専用の紹介リンクを発行できます。\n"
    "紹介リンクからリジェコミュニティに入会した方がいると、その方が続けている間、毎月の紹介特典があります。\n\n"
    "**🎁 紹介特典（アフィリエイトプラン〈月額3,300円〉・プレミアムプラン〈月額4,400円〉の方のみ）**\n"
    "・スタンダードプラン（2,200円）の方を紹介：月500円\n"
    "・アフィリエイトプラン（3,300円）の方を紹介：月750円\n"
    "・プレミアムプラン（4,400円）の方を紹介：月1,000円\n"
    "特典は、紹介した方がお支払いした月の翌月末に、あなたの会費から差し引く形でお渡しします。\n"
    "会費（1回分）を超える分は、運営から月に1回まとめて口座に振り込みます。\n"
    "（スタンダードプランの方も紹介リンクは発行できますが、紹介特典は発生しません）\n\n"
    f"くわしくは紹介制度規約をご覧ください：https://resense-inc.com/legal/referral.html"
)


def _member_text(row: dict, summary: dict) -> str:
    if referral.is_exempt(row):
        email = "（会費免除のため不要。紹介特典は全額を口座に振り込みます）"
    else:
        email = row.get("Stripeのメールアドレス") or "（未登録：特典を受け取るには登録してください）"
    return (
        "🎁 **あなたの紹介リンク**（あなたにだけ表示されています）\n"
        f"{referral.referral_link(row['紹介コード'])}\n"
        f"紹介コード：`{row['紹介コード']}`\n\n"
        f"・特典を受け取るStripeのメールアドレス：{email}\n"
        f"・紹介して入会した方：{summary['referred']}人\n"
        f"・お渡し済みの特典：{summary['granted']:,}円"
        f"（会費から差し引き {summary['credit']:,}円／口座への振込 {summary['transfer']:,}円）\n"
        f"・お渡し予定の特典：{summary['scheduled']:,}円\n\n"
        "このリンクを、紹介したい方に送ってください。リンクから入会すると、あなたの紹介として記録されます。"
    )


class EmailModal(discord.ui.Modal, title="紹介リンクの発行"):
    def __init__(self, current_email: str = ""):
        super().__init__()
        self.email = discord.ui.TextInput(
            label="リジェの会費を支払っているStripeのメールアドレス",
            placeholder="例：taro@example.com",
            default=current_email or None,
            required=False,
            max_length=200,
        )
        self.add_item(self.email)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await asyncio.to_thread(
                referral.issue_code, str(interaction.user.id), interaction.user.display_name, self.email.value
            )
            summary = await asyncio.to_thread(referral.member_summary, str(interaction.user.id))
        except referral.ReferralError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        await interaction.followup.send(_member_text(row, summary), ephemeral=True, view=ChangeEmailView(row))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await _report_error(interaction, error)


class ChangeEmailView(discord.ui.View):
    """発行済みの人向けの「メールアドレスを変更」ボタン。"""

    def __init__(self, row: dict | None):
        super().__init__(timeout=600)
        self.row = row
        if row is None:
            self.change.label = "紹介リンクを発行する"
            self.change.style = discord.ButtonStyle.primary

    @discord.ui.button(label="メールアドレスを変更", style=discord.ButtonStyle.secondary)
    async def change(self, interaction: discord.Interaction, button: discord.ui.Button):
        current = self.row.get("Stripeのメールアドレス", "") if self.row else ""
        await interaction.response.send_modal(EmailModal(current))


class ReferralPanel(discord.ui.View):
    """チャンネルに置く「紹介リンクを発行・確認」ボタン（再起動しても押せるよう custom_id を固定）。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="紹介リンクを発行・確認", emoji="🎁", style=discord.ButtonStyle.primary, custom_id="referral_panel")
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await asyncio.to_thread(referral.get_code_for_member, str(interaction.user.id))
        if row is None:
            await interaction.followup.send(
                "まだ紹介リンクを発行していません。\n"
                "下のボタンを押して、リジェの会費を支払っているStripeのメールアドレスを入力すると発行できます。\n"
                "（紹介特典を会費から差し引くために使います）",
                ephemeral=True,
                view=ChangeEmailView(None),
            )
            return
        summary = await asyncio.to_thread(referral.member_summary, str(interaction.user.id))
        await interaction.followup.send(_member_text(row, summary), ephemeral=True, view=ChangeEmailView(row))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        await _report_error(interaction, error)


PLAN_TEXT = (
    "📋 **リジェコミュニティ プラン一覧**\n\n"
    "**スタンダードプラン**　月額2,200円（税込）\n"
    "・コミュニティへの参加（会員同士のマッチング・交流）\n"
    "・勉強会・イベントへの参加\n"
    "・限定情報の受け取り\n\n"
    "**⭐️ アフィリエイトプラン**　月額3,300円（税込）\n"
    "・スタンダードプランのすべての内容\n"
    "・紹介特典の受け取り（紹介した方が続けている間、毎月500〜1,000円）\n\n"
    "**🌟 プレミアムプラン**　月額4,400円（税込）\n"
    "・アフィリエイトプランのすべての内容\n"
    "・専用チャンネル（コミュニティの枠・発信チャンネル）の作成・運営\n"
    "・コミュニティ内での発信・集客\n\n"
    "下の「プランを変更する」ボタンから、プランの変更（アップグレード・ダウングレード）ができます。\n"
    "変更すると、Discordのロール（⭐️・🌟）も自動で切り替わります。\n"
    "変更後の料金（日割りなど）は、変更の画面に表示されます。"
)


class PlanChangePanel(discord.ui.View):
    """「プラン変更」チャンネルに置くボタン（再起動しても押せるよう custom_id を固定）。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="プランを変更する", emoji="🔄", style=discord.ButtonStyle.primary, custom_id="plan_change_panel")
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not referral.is_configured():
            await interaction.followup.send("ただいま準備中です。運営にお問い合わせください。", ephemeral=True)
            return
        guild_id = interaction.guild.id if interaction.guild else ""
        return_url = f"https://discord.com/channels/{guild_id}/{interaction.channel_id}"
        url = await asyncio.to_thread(referral.portal_url_for, str(interaction.user.id), return_url)
        view = discord.ui.View()
        if url:
            view.add_item(discord.ui.Button(label="プラン変更の画面を開く", url=url, emoji="🔄"))
            await interaction.followup.send(
                "下のボタンから、プラン変更の画面（Stripe）を開けます。\n"
                "（このボタンは数分で使えなくなります。使えないときは、もう一度「プランを変更する」を押してください）",
                ephemeral=True,
                view=view,
            )
            return
        if referral.PORTAL_LOGIN_URL:
            view.add_item(discord.ui.Button(label="会員さま専用ページを開く", url=referral.PORTAL_LOGIN_URL))
            await interaction.followup.send(
                "お申し込みとDiscordアカウントの結び付けが見つかりませんでした。\n"
                "下の「会員さま専用ページ」で、お申し込みに使ったメールアドレスを入力すると、"
                "届いたメールからプランを変更できます。\n"
                "（この方法で変更した場合、Discordのロールは運営が切り替えます）",
                ephemeral=True,
                view=view,
            )
            return
        await interaction.followup.send(
            "お申し込みとDiscordアカウントの結び付けが見つかりませんでした。お手数ですが運営にお問い合わせください。",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        await _report_error(interaction, error)


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
