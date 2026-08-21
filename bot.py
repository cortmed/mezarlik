"""
Mezarlik Bot
============
Discord'da bir anma bolumunu yasatan kucuk bot.

Ozellikler
----------
1. Gun sayaci      : bir ses kanalinin adini her gun gunceller -> "⏳ 412 gündür yok"
2. Hayalet webhook : arsivden rastgele bir eski mesajini, onun ismi ve avatariyla atar
3. Mum sayaci      : /mum komutu, kitabedeki mum sayisini artirir
4. Son gorulme     : cevrimici olduysa kanal aciklamasini gunceller
5. (opsiyonel) Dirilis alarmi: uzun sessizlikten sonra mesaj atarsa herkesi etiketler

Veri saklama
------------
Railway/Render'in ucretsiz katmaninda disk kalici degildir. Bu yuzden bot butun
durumunu (mum sayisi, soz arsivi, son gorulme) gizli bir Discord kanalindaki tek
bir mesajin ekinde JSON olarak tutar. Bot yeniden baslayinca oradan okur.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Europe/Istanbul")
except Exception:  # pragma: no cover - cok eski Python
    TZ = timezone(timedelta(hours=3))


# ---------------------------------------------------------------- ayarlar


def _env(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    if required and not value:
        raise SystemExit(
            f"Eksik ayar: {name}\n"
            f"Railway > Variables kismina {name} degerini ekleyin."
        )
    return value


def _env_int(name: str, *, required: bool = True) -> Optional[int]:
    raw = _env(name, required=required)
    if not raw:
        return None
    if not raw.isdigit():
        raise SystemExit(f"{name} bir Discord ID'si olmali (sadece rakam). Gelen: {raw!r}")
    return int(raw)


TOKEN = _env("DISCORD_TOKEN")
GUILD_ID = _env_int("GUILD_ID")
TARGET_USER_ID = _env_int("TARGET_USER_ID")

COUNTER_CHANNEL_ID = _env_int("COUNTER_CHANNEL_ID")      # ses kanali: gun sayaci
GHOST_CHANNEL_ID = _env_int("GHOST_CHANNEL_ID")          # hayaletin yazacagi kanal
KITABE_CHANNEL_ID = _env_int("KITABE_CHANNEL_ID")        # mum sayacinin durdugu kanal
LASTSEEN_CHANNEL_ID = _env_int("LASTSEEN_CHANNEL_ID")    # aciklamasi guncellenen kanal
DATA_CHANNEL_ID = _env_int("DATA_CHANNEL_ID")            # gizli veri kanali

# opsiyonel
DIRILIS_CHANNEL_ID = _env_int("DIRILIS_CHANNEL_ID", required=False)
FALLBACK_LAST_MESSAGE = _env("FALLBACK_LAST_MESSAGE", required=False)  # "2025-03-14"
COUNTER_TEMPLATE = _env("COUNTER_TEMPLATE", required=False, default="⏳ {gun} gündür yok")
GHOST_INTERVAL_HOURS = int(_env("GHOST_INTERVAL_HOURS", required=False, default="168"))
DIRILIS_ESIK_GUN = int(_env("DIRILIS_ESIK_GUN", required=False, default="30"))
SCAN_LIMIT = int(_env("SCAN_LIMIT_PER_CHANNEL", required=False, default="5000"))

# Gorunum
# MUM_EMOJI: animasyonlu ozel emoji icin <a:isim:ID> yapistir, bos birakirsan 🕯️ kullanilir
MUM_EMOJI = _env("MUM_EMOJI", required=False, default="🕯️")
KITABE_GIF_URL = _env("KITABE_GIF_URL", required=False)   # elle GIF vermek istersen


def emoji_gorsel(raw: str) -> Optional[str]:
    """<a:mum:123> biciminde bir emojiyi CDN gorsel adresine cevirir.

    Emoji metnin icinde kaldigi surece kucucuk gorunur; ayni dosyayi embed'e
    gorsel olarak gomunce gercek boyutunda, iri ve oynar halde cikar.
    """
    eslesme = re.fullmatch(r"<(a?):([A-Za-z0-9_]+):(\d+)>", raw.strip())
    if not eslesme:
        return None
    uzanti = "gif" if eslesme.group(1) == "a" else "png"
    return f"https://cdn.discordapp.com/emojis/{eslesme.group(3)}.{uzanti}"


# Once elle verilen GIF, yoksa MUM_EMOJI'den turetilen gorsel
MUM_GORSEL = KITABE_GIF_URL or emoji_gorsel(MUM_EMOJI)

# Muzik tetigi (opsiyonel). Ucu de doldurulmazsa ozellik kapali kalir.
MUZIK_SES_KANALI_ID = _env_int("MUZIK_SES_KANALI_ID", required=False)   # izlenecek ses kanali
MUZIK_KOMUT_KANALI_ID = _env_int("MUZIK_KOMUT_KANALI_ID", required=False)  # komutun yazilacagi yazi kanali
MUZIK_KOMUT = _env("MUZIK_KOMUT", required=False)  # ornek: m!play https://youtu.be/xxxx
MUZIK_BEKLEME_SN = int(_env("MUZIK_BEKLEME_SN", required=False, default="300"))

STATE_FILENAME = "mezarlik-state.json"
MAX_QUOTES = 500


# ---------------------------------------------------------------- durum


class State:
    """Bot durumu. Gizli bir Discord kanalindaki mesajin ekinde saklanir."""

    def __init__(self) -> None:
        self.candles: int = 0
        self.candle_lighters: list[int] = []
        self.quotes: list[str] = []
        self.used_quotes: list[int] = []
        self.last_message_at: Optional[str] = None   # sayac bunu kullanir
        self.last_online_at: Optional[str] = None    # son gorulme bunu kullanir
        self.kitabe_message_id: Optional[int] = None
        self.ghost_webhook_url: Optional[str] = None

        self._message: Optional[discord.Message] = None

    # --- serilestirme

    def to_dict(self) -> dict[str, Any]:
        return {
            "candles": self.candles,
            "candle_lighters": self.candle_lighters,
            "quotes": self.quotes,
            "used_quotes": self.used_quotes,
            "last_message_at": self.last_message_at,
            "last_online_at": self.last_online_at,
            "kitabe_message_id": self.kitabe_message_id,
            "ghost_webhook_url": self.ghost_webhook_url,
        }

    def from_dict(self, data: dict[str, Any]) -> None:
        self.candles = int(data.get("candles", 0))
        self.candle_lighters = list(data.get("candle_lighters", []))
        self.quotes = list(data.get("quotes", []))
        self.used_quotes = list(data.get("used_quotes", []))
        self.last_message_at = data.get("last_message_at")
        self.last_online_at = data.get("last_online_at")
        self.kitabe_message_id = data.get("kitabe_message_id")
        self.ghost_webhook_url = data.get("ghost_webhook_url")

    # --- disk yerine Discord

    async def load(self, channel: discord.TextChannel) -> None:
        async for message in channel.history(limit=50):
            if message.author.id != channel.guild.me.id:
                continue
            for attachment in message.attachments:
                if attachment.filename != STATE_FILENAME:
                    continue
                raw = await attachment.read()
                self.from_dict(json.loads(raw.decode("utf-8")))
                self._message = message
                print(f"[durum] yuklendi — {len(self.quotes)} soz, {self.candles} mum")
                return
        print("[durum] kayit bulunamadi, sifirdan basliyoruz")

    async def save(self, channel: discord.TextChannel) -> None:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        file = discord.File(io.BytesIO(payload.encode("utf-8")), filename=STATE_FILENAME)
        try:
            if self._message is not None:
                await self._message.edit(attachments=[file])
            else:
                self._message = await channel.send(
                    "🪦 Mezarlik botunun hafizasi. **Bu mesaji silmeyin.**",
                    file=file,
                )
        except discord.HTTPException as exc:
            print(f"[durum] kaydedilemedi: {exc}")


state = State()


# ---------------------------------------------------------------- yardimcilar


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def days_since(moment: Optional[datetime]) -> Optional[int]:
    if moment is None:
        return None
    return max((now() - moment).days, 0)


def reference_moment() -> Optional[datetime]:
    """Sayacin saydigi baslangic: son mesaji, yoksa elle girilen tarih."""
    moment = parse_iso(state.last_message_at)
    if moment:
        return moment
    return parse_iso(FALLBACK_LAST_MESSAGE)


def human_date(moment: datetime) -> str:
    aylar = [
        "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
        "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
    ]
    local = moment.astimezone(TZ)
    return f"{local.day} {aylar[local.month - 1]} {local.year}, {local:%H:%M}"


LINK_RE = re.compile(r"https?://\S+")
MENTION_RE = re.compile(r"<[@#!&:a-zA-Z0-9_]+?>")
EMOJI_ONLY_RE = re.compile(r"^[\W\d_]+$", re.UNICODE)


def clean_quote(content: str) -> Optional[str]:
    """Bir mesaji hayaletin agzina yakisir hale getirir, uygun degilse None doner."""
    text = MENTION_RE.sub("", LINK_RE.sub("", content)).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) < 12 or len(text) > 280:
        return None
    if EMOJI_ONLY_RE.match(text):
        return None
    if text.startswith(("!", "/", ".", "?", "-", "+")):
        return None
    return text


# ---------------------------------------------------------------- bot


intents = discord.Intents.default()
intents.members = True          # Developer Portal'da acilmali
intents.presences = True        # Developer Portal'da acilmali
intents.message_content = True  # Developer Portal'da acilmali

bot = commands.Bot(command_prefix="!mezarlik ", intents=intents, help_command=None)
GUILD = discord.Object(id=GUILD_ID)


def get_channel(channel_id: Optional[int]) -> Optional[discord.abc.GuildChannel]:
    if channel_id is None:
        return None
    return bot.get_channel(channel_id)


async def persist() -> None:
    channel = get_channel(DATA_CHANNEL_ID)
    if isinstance(channel, discord.TextChannel):
        await state.save(channel)


# ---------------------------------------------------------------- 1) gun sayaci


@tasks.loop(hours=6)
async def gun_sayaci() -> None:
    """Ses kanalinin adini gunceller.

    Discord kanal adi degistirmeyi 10 dakikada 2 ile sinirliyor; 6 saatte bir
    calismak fazlasiyla guvenli.
    """
    channel = get_channel(COUNTER_CHANNEL_ID)
    if channel is None:
        print("[sayac] COUNTER_CHANNEL_ID bulunamadi — ID dogru mu?")
        return

    gun = days_since(reference_moment())
    if gun is None:
        print("[sayac] baslangic tarihi yok, FALLBACK_LAST_MESSAGE ayarlayin")
        return

    yeni_ad = COUNTER_TEMPLATE.format(gun=gun)
    if channel.name == yeni_ad:
        return

    try:
        await channel.edit(name=yeni_ad, reason="Mezarlik gun sayaci")
        print(f"[sayac] kanal adi guncellendi: {yeni_ad}")
    except discord.Forbidden:
        print("[sayac] yetki yok — bota 'Kanalları Yönet' izni verin")
    except discord.HTTPException as exc:
        print(f"[sayac] guncellenemedi: {exc}")


@gun_sayaci.before_loop
async def _before_sayac() -> None:
    await bot.wait_until_ready()


# ---------------------------------------------------------------- 2) hayalet webhook


async def ghost_webhook() -> Optional[discord.Webhook]:
    """Hayaletin konustugu webhook'u bulur, yoksa olusturur."""
    if state.ghost_webhook_url:
        try:
            return discord.Webhook.from_url(state.ghost_webhook_url, client=bot)
        except ValueError:
            state.ghost_webhook_url = None

    channel = get_channel(GHOST_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return None

    try:
        hook = await channel.create_webhook(name="hayalet")
    except discord.Forbidden:
        print("[hayalet] yetki yok — bota 'Webhookları Yönet' izni verin")
        return None

    state.ghost_webhook_url = hook.url
    await persist()
    return hook


def pick_quote() -> Optional[tuple[int, str]]:
    """Once hic kullanilmamis sozlerden secer; hepsi bitince listeyi tazeler."""
    if not state.quotes:
        return None
    kalan = [i for i in range(len(state.quotes)) if i not in state.used_quotes]
    if not kalan:
        state.used_quotes = []
        kalan = list(range(len(state.quotes)))
    index = random.choice(kalan)
    return index, state.quotes[index]


@tasks.loop(hours=GHOST_INTERVAL_HOURS)
async def hayalet() -> None:
    secim = pick_quote()
    if secim is None:
        print("[hayalet] arsiv bos — once /arsiv-tara calistirin")
        return

    index, quote = secim
    hook = await ghost_webhook()
    if hook is None:
        return

    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(TARGET_USER_ID) if guild else None
    isim = member.display_name if member else "hayalet"
    avatar = member.display_avatar.url if member else None

    try:
        await hook.send(content=quote, username=isim[:80], avatar_url=avatar)
        state.used_quotes.append(index)
        await persist()
        print(f"[hayalet] konustu: {quote[:60]}")
    except discord.HTTPException as exc:
        print(f"[hayalet] gonderilemedi: {exc}")


@hayalet.before_loop
async def _before_hayalet() -> None:
    await bot.wait_until_ready()


# ---------------------------------------------------------------- 3) mum sayaci


def kitabe_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🕯️ Anma Mumları",
        description=(
            f"Bugüne kadar **{state.candles}** mum yakıldı.\n"
            f"**{len(state.candle_lighters)}** kişi uğradı."
        ),
        colour=discord.Colour.from_str("#C9A35A"),
    )
    moment = reference_moment()
    if moment:
        embed.add_field(name="Son iz", value=human_date(moment), inline=True)
        embed.add_field(name="Geçen süre", value=f"{days_since(moment)} gün", inline=True)
    if MUM_GORSEL:
        embed.set_image(url=MUM_GORSEL)
    embed.set_footer(text="Mum yakmak için  /mum")
    return embed


async def refresh_kitabe() -> None:
    channel = get_channel(KITABE_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return

    embed = kitabe_embed()
    if state.kitabe_message_id:
        try:
            message = await channel.fetch_message(state.kitabe_message_id)
            await message.edit(embed=embed)
            return
        except (discord.NotFound, discord.Forbidden):
            state.kitabe_message_id = None

    try:
        message = await channel.send(embed=embed)
        await message.pin(reason="Mezarlik kitabesi")
        state.kitabe_message_id = message.id
        await persist()
    except discord.HTTPException as exc:
        print(f"[kitabe] olusturulamadi: {exc}")


@bot.tree.command(name="mum", description="Oğuzhan için bir mum yak", guild=GUILD)
async def mum(interaction: discord.Interaction) -> None:
    state.candles += 1
    ilk_kez = interaction.user.id not in state.candle_lighters
    if ilk_kez:
        state.candle_lighters.append(interaction.user.id)

    await persist()
    await refresh_kitabe()

    # 1. kare: mum yakiliyor
    yakiliyor = discord.Embed(
        description=f"{MUM_EMOJI}  {interaction.user.mention} bir mum yakıyor…",
        colour=discord.Colour.from_str("#4B5058"),
    )
    # ephemeral yok: mesaj kanala dusuyor, herkes goruyor
    await interaction.response.send_message(embed=yakiliyor)

    # 2. kare: duvar aciliyor. Tek bir edit, rate limit derdi yok.
    await asyncio.sleep(1.4)

    acildi = discord.Embed(
        description=(
            f"{interaction.user.mention} bir mum yaktı.\n"
            f"Mezarlıkta yanan mum sayısı: **{state.candles}**"
        ),
        colour=discord.Colour.from_str("#C9A35A"),
    )
    if MUM_GORSEL:
        acildi.set_image(url=MUM_GORSEL)
    if ilk_kez:
        acildi.set_footer(text="Mezarlığa ilk gelişi.")

    try:
        await interaction.edit_original_response(embed=acildi)
    except discord.HTTPException as exc:
        print(f"[mum] animasyon tamamlanamadi: {exc}")


# ---------------------------------------------------------------- muzik tetigi


_son_muzik_tetigi: Optional[datetime] = None


async def muzik_baslat(sebep: str) -> bool:
    """Jockie'ye komutu yazar. Gonderebildiyse True doner."""
    global _son_muzik_tetigi

    kanal = get_channel(MUZIK_KOMUT_KANALI_ID)
    if not isinstance(kanal, discord.TextChannel):
        print("[muzik] MUZIK_KOMUT_KANALI_ID bir yazi kanali degil")
        return False

    try:
        await kanal.send(MUZIK_KOMUT)
        _son_muzik_tetigi = now()
        print(f"[muzik] komut gonderildi ({sebep}): {MUZIK_KOMUT}")
        return True
    except discord.HTTPException as exc:
        print(f"[muzik] gonderilemedi: {exc}")
        return False


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if not (MUZIK_SES_KANALI_ID and MUZIK_KOMUT_KANALI_ID and MUZIK_KOMUT):
        return

    # Jockie'nin kendi girisi tekrar tetiklemesin
    if member.bot:
        return

    # Hedef kanala giris mi?
    if after.channel is None or after.channel.id != MUZIK_SES_KANALI_ID:
        return

    # Ayni kanaldaysa sadece mikrofon/kamera degismistir, giris degil
    if before.channel is not None and before.channel.id == after.channel.id:
        return

    # Sadece odayi ilk acan kisi tetiklesin; ikinci kisi girince muzik zaten caliyordur
    insanlar = [uye for uye in after.channel.members if not uye.bot]
    if len(insanlar) != 1:
        return

    # Kisa araliklarla girip cikmalar Jockie'yi bogmasin
    if _son_muzik_tetigi is not None:
        gecen = (now() - _son_muzik_tetigi).total_seconds()
        if gecen < MUZIK_BEKLEME_SN:
            print(f"[muzik] bekleme suresi doldurulmadi ({int(gecen)}s)")
            return

    await muzik_baslat(f"{member.display_name} odaya girdi")


@bot.tree.command(
    name="muzik-dene",
    description="Müzik komutunu hemen gönderir, Jockie tepki veriyor mu diye bakar (yönetici)",
    guild=GUILD,
)
@app_commands.checks.has_permissions(manage_guild=True)
async def muzik_dene(interaction: discord.Interaction) -> None:
    if not (MUZIK_KOMUT_KANALI_ID and MUZIK_KOMUT):
        await interaction.response.send_message(
            "Müzik tetiği kapalı. Railway'de `MUZIK_KOMUT_KANALI_ID` ve `MUZIK_KOMUT` "
            "değişkenlerini doldurman lazım.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    ok = await muzik_baslat("elle test")
    await interaction.followup.send(
        "📨 Komut gönderildi. Kanala bak — Jockie cevap verdiyse çalışıyor, "
        "hiç tepki vermediyse bot komutlarını görmezden geliyor demektir."
        if ok
        else "Gönderilemedi, Deploy Logs'taki `[muzik]` satırına bak.",
        ephemeral=True,
    )


# ---------------------------------------------------------------- 4) son gorulme


async def update_lastseen_topic() -> None:
    channel = get_channel(LASTSEEN_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return

    parcalar = []
    online = parse_iso(state.last_online_at)
    if online:
        parcalar.append(f"Son çevrimiçi: {human_date(online)}")
    mesaj = reference_moment()
    if mesaj:
        parcalar.append(f"Son mesaj: {human_date(mesaj)} ({days_since(mesaj)} gün önce)")
    if not parcalar:
        return

    topic = "🥀 " + "  •  ".join(parcalar)
    if channel.topic == topic:
        return

    try:
        await channel.edit(topic=topic[:1024], reason="Son gorulme takibi")
        print("[son gorulme] aciklama guncellendi")
    except discord.Forbidden:
        print("[son gorulme] yetki yok — bota 'Kanalları Yönet' izni verin")
    except discord.HTTPException as exc:
        print(f"[son gorulme] guncellenemedi: {exc}")


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member) -> None:
    if after.id != TARGET_USER_ID:
        return
    if after.status is discord.Status.offline:
        return

    state.last_online_at = now().isoformat()
    await persist()
    await update_lastseen_topic()


# ---------------------------------------------------------------- 5) dirilis


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.id != TARGET_USER_ID or message.guild is None:
        return

    onceki = reference_moment()
    gecen = days_since(onceki)

    state.last_message_at = message.created_at.isoformat()
    state.last_online_at = message.created_at.isoformat()
    await persist()

    if DIRILIS_CHANNEL_ID and gecen is not None and gecen >= DIRILIS_ESIK_GUN:
        channel = get_channel(DIRILIS_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            embed = discord.Embed(
                title="🔔 ÇAN ÇALDI",
                description=(
                    f"{message.author.mention} **{gecen} gün** sonra geri döndü.\n"
                    f"Mezarlık bugünlük kapalıdır."
                ),
                colour=discord.Colour.from_str("#C9A35A"),
            )
            try:
                await channel.send(content="@everyone", embed=embed)
            except discord.HTTPException as exc:
                print(f"[dirilis] gonderilemedi: {exc}")

    await update_lastseen_topic()
    await gun_sayaci()


# ---------------------------------------------------------------- arsiv tarama


@bot.tree.command(
    name="arsiv-tara",
    description="Eski mesajlarını tarayıp hayaletin söz arşivini doldurur (yönetici)",
    guild=GUILD,
)
@app_commands.describe(kanal_basina="Kanal başına taranacak mesaj sayısı (varsayılan 5000)")
@app_commands.checks.has_permissions(manage_guild=True)
async def arsiv_tara(interaction: discord.Interaction, kanal_basina: int = 0) -> None:
    limit = kanal_basina or SCAN_LIMIT
    await interaction.response.send_message(
        f"🔦 Arşiv taranıyor — kanal başına {limit} mesaj. Bu birkaç dakika sürebilir.",
        ephemeral=True,
    )

    guild = interaction.guild
    assert guild is not None

    bulunan: set[str] = set(state.quotes)
    taranan_kanal = 0
    en_eski: Optional[datetime] = None
    en_yeni: Optional[datetime] = None

    for channel in guild.text_channels:
        izin = channel.permissions_for(guild.me)
        if not (izin.read_message_history and izin.view_channel):
            continue
        taranan_kanal += 1
        try:
            async for message in channel.history(limit=limit, oldest_first=False):
                if message.author.id != TARGET_USER_ID:
                    continue
                if en_yeni is None or message.created_at > en_yeni:
                    en_yeni = message.created_at
                if en_eski is None or message.created_at < en_eski:
                    en_eski = message.created_at
                quote = clean_quote(message.content)
                if quote:
                    bulunan.add(quote)
        except discord.HTTPException:
            continue

    state.quotes = sorted(bulunan, key=len, reverse=True)[:MAX_QUOTES]
    state.used_quotes = []
    if en_yeni and not state.last_message_at:
        state.last_message_at = en_yeni.isoformat()

    await persist()
    await refresh_kitabe()
    await update_lastseen_topic()
    await gun_sayaci()

    ozet = [
        f"✅ **{taranan_kanal}** kanal tarandı.",
        f"🗣️ **{len(state.quotes)}** söz arşive girdi.",
    ]
    if en_yeni:
        ozet.append(f"🥀 Bulunan son mesaj: {human_date(en_yeni)}")
    else:
        ozet.append("⚠️ Hiç mesajı bulunamadı — gün sayacı çalışmaz. "
                    "`FALLBACK_LAST_MESSAGE` değişkenine elle bir tarih girin.")
    await interaction.followup.send("\n".join(ozet), ephemeral=True)


@bot.tree.command(name="mezarlik", description="Mezarlığın durumu", guild=GUILD)
async def mezarlik(interaction: discord.Interaction) -> None:
    embed = kitabe_embed()
    embed.title = "🪦 Mezarlık"
    embed.add_field(name="Arşivdeki söz", value=str(len(state.quotes)), inline=True)
    embed.add_field(
        name="Hayalet",
        value=f"{GHOST_INTERVAL_HOURS} saatte bir konuşur",
        inline=True,
    )
    # ephemeral yok: durumu herkes gorsun
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="hayalet-simdi", description="Hayaleti hemen konuştur (yönetici)", guild=GUILD)
@app_commands.checks.has_permissions(manage_guild=True)
async def hayalet_simdi(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    await hayalet()
    await interaction.followup.send("👻 Denendi — kanala bak.", ephemeral=True)


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        mesaj = "Bu komut sadece yöneticiler için."
    else:
        mesaj = f"Bir şeyler ters gitti: `{error}`"
        print(f"[hata] {error!r}")
    if interaction.response.is_done():
        await interaction.followup.send(mesaj, ephemeral=True)
    else:
        await interaction.response.send_message(mesaj, ephemeral=True)


# ---------------------------------------------------------------- baslangic


@bot.event
async def on_ready() -> None:
    print(f"[bot] giris yapildi: {bot.user}")

    data_channel = get_channel(DATA_CHANNEL_ID)
    if isinstance(data_channel, discord.TextChannel):
        await state.load(data_channel)
    else:
        print("[bot] DATA_CHANNEL_ID bulunamadi — hafiza calismayacak")

    await bot.tree.sync(guild=GUILD)
    print("[bot] komutlar senkronize edildi")

    if not gun_sayaci.is_running():
        gun_sayaci.start()
    if not hayalet.is_running():
        hayalet.start()

    await refresh_kitabe()
    await update_lastseen_topic()

    await bot.change_presence(
        status=discord.Status.idle,
        activity=discord.Activity(type=discord.ActivityType.watching, name="mezarlığı 🪦"),
    )


if __name__ == "__main__":
    bot.run(TOKEN)
