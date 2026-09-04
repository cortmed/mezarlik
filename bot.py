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

# Muzik (opsiyonel). Ikisi de doldurulmazsa ozellik kapali kalir.
MUZIK_SES_KANALI_ID = _env_int("MUZIK_SES_KANALI_ID", required=False)  # izlenecek ses kanali
MUZIK_URL = _env("MUZIK_URL", required=False)  # YouTube linki ya da dogrudan ses dosyasi adresi
# Discord'a yuklenmis bir ses dosyasindan calmak icin (en saglam yol):
# dosyayi bir kanala at, mesaja sag tik -> "Mesaj Bagini Kopyala", sondaki
# iki sayiyi buraya yaz. Discord'un dosya linkleri sureli oldugu icin bot
# adresi her calmadan once mesajdan yeniden okur, boylece hic bayatlamaz.
MUZIK_MESAJ_KANALI_ID = _env_int("MUZIK_MESAJ_KANALI_ID", required=False)
MUZIK_MESAJ_ID = _env_int("MUZIK_MESAJ_ID", required=False)
MUZIK_DONGU = _env("MUZIK_DONGU", required=False, default="1") != "0"  # bitince bastan alsin mi
MUZIK_SES_SEVIYESI = float(_env("MUZIK_SES_SEVIYESI", required=False, default="0.5"))
# Yasin sorusunun yazilacagi kanal. Bos birakilirsa ses kanalinin kendi sohbetine yazar.
YASIN_KANALI_ID = _env_int("YASIN_KANALI_ID", required=False)
YASIN_SORU_SN = int(_env("YASIN_SORU_SN", required=False, default="600"))

STATE_FILENAME = "mezarlik-state.json"
MAX_QUOTES = 500


# ---------------------------------------------------------------- durum


class State:
    """Bot durumu. Gizli bir Discord kanalindaki mesajin ekinde saklanir."""

    def __init__(self) -> None:
        self.candles: int = 0
        self.candle_counts: dict[str, int] = {}   # kullanici id -> yaktigi mum
        self.yasin: int = 0
        self.yasin_counts: dict[str, int] = {}   # kimin hayrina kac yasin okundu
        self.hayir_counts: dict[str, int] = {}   # kim adina kac hayir islendi
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
            "candle_counts": self.candle_counts,
            "yasin": self.yasin,
            "yasin_counts": self.yasin_counts,
            "hayir_counts": self.hayir_counts,
            "quotes": self.quotes,
            "used_quotes": self.used_quotes,
            "last_message_at": self.last_message_at,
            "last_online_at": self.last_online_at,
            "kitabe_message_id": self.kitabe_message_id,
            "ghost_webhook_url": self.ghost_webhook_url,
        }

    def from_dict(self, data: dict[str, Any]) -> None:
        self.candles = int(data.get("candles", 0))
        self.candle_counts = {str(k): int(v) for k, v in data.get("candle_counts", {}).items()}
        # Eski kayitlarda sadece "kim ugradi" listesi vardi; herkese bir mum yazarak tasi
        if not self.candle_counts:
            for eski in data.get("candle_lighters", []):
                self.candle_counts[str(eski)] = 1
        self.yasin = int(data.get("yasin", 0))
        self.yasin_counts = {str(k): int(v) for k, v in data.get("yasin_counts", {}).items()}
        self.hayir_counts = {str(k): int(v) for k, v in data.get("hayir_counts", {}).items()}
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

    def _dosya(self) -> discord.File:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        return discord.File(io.BytesIO(payload.encode("utf-8")), filename=STATE_FILENAME)

    async def save(self, channel: discord.TextChannel) -> None:
        """Durumu veri kanalindaki mesaja yazar.

        Discord, bir saatten eski mesajlarin duzenlenmesine kota koyuyor
        (hata 30046). Kotaya carparsak eski mesaji birakip yenisini aciyoruz;
        `load` her zaman en yeni kaydi okudugu icin veri kaybolmuyor.
        """
        if self._message is not None:
            try:
                await self._message.edit(attachments=[self._dosya()])
                return
            except discord.HTTPException as exc:
                if getattr(exc, "code", None) == 30046:
                    print("[durum] duzenleme kotasi doldu, yeni kayit mesaji aciliyor")
                    self._message = None
                else:
                    print(f"[durum] kaydedilemedi: {exc}")
                    return

        try:
            self._message = await channel.send(
                "🪦 Mezarlik botunun hafizasi. **Bu mesaji silmeyin.**",
                file=self._dosya(),
            )
        except discord.HTTPException as exc:
            print(f"[durum] yeni kayit mesaji acilamadi: {exc}")


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
            f"**{len(state.candle_counts)}** kişi uğradı."
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
    anahtar = str(interaction.user.id)
    ilk_kez = anahtar not in state.candle_counts
    state.candle_counts[anahtar] = state.candle_counts.get(anahtar, 0) + 1

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


# ---------------------------------------------------------------- muzik


YTDL_AYARLARI = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",   # bazi aglarda IPv6 yuzunden takiliyor
}

FFMPEG_AYARLARI = {
    # Akis koparsa ffmpeg kendi kendine yeniden baglansin
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


DOGRUDAN_UZANTILAR = (".mp3", ".ogg", ".wav", ".m4a", ".opus", ".flac", ".webm", ".mp4")


def _dogrudan_medya_mi(url: str) -> bool:
    """Adres zaten bir ses dosyasiysa yt_dlp'ye hic ugramaya gerek yok."""
    yol = url.split("?", 1)[0].lower()
    return yol.endswith(DOGRUDAN_UZANTILAR)


async def _mesajdaki_dosya() -> Optional[tuple[str, str]]:
    """Belirtilen mesajin ekindeki ses dosyasinin taze adresini dondurur."""
    if not MUZIK_MESAJ_ID:
        return None

    kanal = get_channel(MUZIK_MESAJ_KANALI_ID or DATA_CHANNEL_ID)
    if not isinstance(kanal, discord.TextChannel):
        print("[muzik] MUZIK_MESAJ_KANALI_ID bir yazi kanali degil")
        return None

    try:
        mesaj = await kanal.fetch_message(MUZIK_MESAJ_ID)
    except discord.HTTPException as exc:
        print(f"[muzik] mesaj okunamadi: {exc}")
        return None

    if not mesaj.attachments:
        print("[muzik] o mesajda dosya eki yok")
        return None

    ek = mesaj.attachments[0]
    return ek.url, ek.filename


def _ses_kaynagi(url: str) -> tuple[str, str]:
    """YouTube linkinden dogrudan ses akisi adresini cikarir.

    yt_dlp aglar uzerinden is yaptigi icin bloke edici; cagiran taraf bunu
    executor'da calistiriyor ki botun geri kalani donmasin.
    """
    import yt_dlp

    with yt_dlp.YoutubeDL(YTDL_AYARLARI) as ydl:
        bilgi = ydl.extract_info(url, download=False)
    if "entries" in bilgi:
        bilgi = bilgi["entries"][0]
    return bilgi["url"], bilgi.get("title", "bilinmeyen parca")


def _kanaldaki_insanlar(kanal: discord.VoiceChannel) -> int:
    return len([uye for uye in kanal.members if not uye.bot])


async def muzik_cal(kanal: discord.VoiceChannel, sebep: str) -> bool:
    """Bota kanala girip parcayi caldirir."""
    ses = kanal.guild.voice_client
    if ses is not None and ses.is_playing():
        print("[muzik] zaten caliyor")
        return False

    # 1) Discord'a yuklenmis dosya (en saglam) 2) dogrudan ses adresi 3) YouTube
    kaynak_bilgi = await _mesajdaki_dosya()

    if kaynak_bilgi is None:
        if not MUZIK_URL:
            print("[muzik] ne MUZIK_MESAJ_ID ne MUZIK_URL dolu")
            return False
        if _dogrudan_medya_mi(MUZIK_URL):
            kaynak_bilgi = (MUZIK_URL, MUZIK_URL.rsplit("/", 1)[-1].split("?")[0])
        else:
            try:
                kaynak_bilgi = await asyncio.get_running_loop().run_in_executor(
                    None, _ses_kaynagi, MUZIK_URL
                )
            except Exception as exc:
                print(f"[muzik] kaynak cozulemedi: {exc}")
                return False

    akis, baslik = kaynak_bilgi

    try:
        if ses is None:
            ses = await kanal.connect()
        elif ses.channel != kanal:
            await ses.move_to(kanal)
    except Exception as exc:
        print(f"[muzik] kanala baglanilamadi: {exc}")
        return False

    def bitince(hata: Optional[Exception]) -> None:
        if hata:
            print(f"[muzik] calma hatasi: {hata}")
        # Dongu aciksa sessizce bastan al, kapaliysa odadakilere sor
        if MUZIK_DONGU and _kanaldaki_insanlar(kanal) > 0:
            bot.loop.create_task(muzik_cal(kanal, "dongu"))
        else:
            bot.loop.create_task(yasin_bitti(kanal))

    kaynak = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(akis, **FFMPEG_AYARLARI),
        volume=MUZIK_SES_SEVIYESI,
    )
    ses.play(kaynak, after=bitince)
    print(f"[muzik] caliyor ({sebep}): {baslik}")
    return True


async def muzik_dur(guild: discord.Guild) -> None:
    ses = guild.voice_client
    if ses is None:
        return
    try:
        if ses.is_playing():
            ses.stop()
        await ses.disconnect(force=True)
        print("[muzik] durduruldu, kanaldan cikildi")
    except Exception as exc:
        print(f"[muzik] cikilamadi: {exc}")


# ---------------------------------------------------------------- yasin


class YasinSorusu(discord.ui.View):
    """Okuyus bitince cikan iki dugmeli soru."""

    def __init__(self, kanal: discord.VoiceChannel) -> None:
        super().__init__(timeout=YASIN_SORU_SN)
        self.kanal = kanal
        self.mesaj: Optional[discord.Message] = None

    async def _kapat(self, not_: str) -> None:
        for dugme in self.children:
            dugme.disabled = True
        if self.mesaj is not None:
            try:
                await self.mesaj.edit(content=f"{self.mesaj.content}\n\n_{not_}_", view=self)
            except discord.HTTPException:
                pass
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Odada olmayan biri uzaktan tetiklemesin
        if interaction.user in self.kanal.members:
            return True
        await interaction.response.send_message(
            f"Bunun için {self.kanal.mention} kanalında olman lazım.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Evet, bir tane daha", style=discord.ButtonStyle.success, emoji="🕯️")
    async def evet(self, interaction: discord.Interaction, dugme: discord.ui.Button) -> None:
        await interaction.response.defer()
        await self._kapat(f"{interaction.user.display_name} bir tane daha istedi.")
        await muzik_cal(self.kanal, f"{interaction.user.display_name} yasin istedi")

    @discord.ui.button(label="Yeter", style=discord.ButtonStyle.secondary, emoji="🤲")
    async def hayir(self, interaction: discord.Interaction, dugme: discord.ui.Button) -> None:
        await interaction.response.defer()
        await self._kapat("Mezarlık sessizliğe bırakıldı.")
        await muzik_dur(self.kanal.guild)

    async def on_timeout(self) -> None:
        await self._kapat("Cevap gelmedi, mezarlık sessizliğe bırakıldı.")
        await muzik_dur(self.kanal.guild)


async def yasin_bitti(kanal: discord.VoiceChannel) -> None:
    """Bir okuyus bitti: sayaci artir, odadakilere sor."""
    state.yasin += 1
    await persist()

    insanlar = [uye for uye in kanal.members if not uye.bot]
    if not insanlar:
        # Kimse kalmamis, soracak kimse yok
        await muzik_dur(kanal.guild)
        return

    hedef = get_channel(YASIN_KANALI_ID) if YASIN_KANALI_ID else kanal
    if not isinstance(hedef, discord.abc.Messageable):
        hedef = kanal

    # Okunan yasin, o an kanalda bulunanlarin hayrina yazilir
    for uye in insanlar:
        anahtar = str(uye.id)
        state.yasin_counts[anahtar] = state.yasin_counts.get(anahtar, 0) + 1
    await persist()

    etiketler = " ".join(uye.mention for uye in insanlar)
    metin = (
        "🕯️ Bir adet Yasin okundu.\n\n"
        f"**Hayrına yazılanlar:** {etiketler}\n"
        f"_Merkezde bugüne kadar okunan: {state.yasin}_\n\n"
        "Allah kabul etsin. Bir tane daha okumamı ister misin?"
    )

    gorunum = YasinSorusu(kanal)
    try:
        gorunum.mesaj = await hedef.send(metin, view=gorunum)
    except discord.HTTPException as exc:
        print(f"[yasin] soru gonderilemedi: {exc}")
        await muzik_dur(kanal.guild)
        return

    print(f"[yasin] {state.yasin}. okuyus bitti, {len(insanlar)} kisiye soruldu")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if not (MUZIK_SES_KANALI_ID and (MUZIK_URL or MUZIK_MESAJ_ID)):
        return
    if member.bot:
        return

    kanal = get_channel(MUZIK_SES_KANALI_ID)
    if not isinstance(kanal, discord.VoiceChannel):
        print("[muzik] MUZIK_SES_KANALI_ID bir ses kanali degil")
        return

    girdi = after.channel is not None and after.channel.id == MUZIK_SES_KANALI_ID
    ayni_kanalda_kaldi = (
        before.channel is not None
        and after.channel is not None
        and before.channel.id == after.channel.id
    )

    # Odaya ilk giren muzigi baslatir
    if girdi and not ayni_kanalda_kaldi:
        if _kanaldaki_insanlar(kanal) == 1:
            await muzik_cal(kanal, f"{member.display_name} odaya girdi")
        return

    # Son kisi de ciktiysa bot bos odada calmaya devam etmesin
    ciktti = before.channel is not None and before.channel.id == MUZIK_SES_KANALI_ID
    if ciktti and not girdi and _kanaldaki_insanlar(kanal) == 0:
        await muzik_dur(kanal.guild)


@bot.tree.command(
    name="muzik-dene",
    description="Müziği hemen başlatır (yönetici)",
    guild=GUILD,
)
@app_commands.checks.has_permissions(manage_guild=True)
async def muzik_dene(interaction: discord.Interaction) -> None:
    kanal = get_channel(MUZIK_SES_KANALI_ID)
    if not isinstance(kanal, discord.VoiceChannel) or not (MUZIK_URL or MUZIK_MESAJ_ID):
        await interaction.response.send_message(
            "Müzik kapalı. `MUZIK_SES_KANALI_ID` ve (`MUZIK_MESAJ_ID` veya `MUZIK_URL`) "
            "doldurulmalı.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    ok = await muzik_cal(kanal, "elle test")
    await interaction.followup.send(
        f"🎵 Başladı — **{kanal.name}** kanalına bak."
        if ok
        else "Başlatılamadı, Deploy Logs'taki `[muzik]` satırına bak.",
        ephemeral=True,
    )


@bot.tree.command(name="muzik-dur", description="Müziği durdurur (yönetici)", guild=GUILD)
@app_commands.checks.has_permissions(manage_guild=True)
async def muzik_dur_komut(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    await muzik_dur(interaction.guild)
    await interaction.response.send_message("⏹️ Durduruldu.", ephemeral=True)


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


# ---------------------------------------------------------------- eglence


OLUM_SEBEPLERI = [
    "3 saat AFK kaldı, kimse fark etmedi",
    "\"5 dk sonra geliyorum\" dedi",
    "Sesli kanala girdi, kimse yoktu",
    "Bildirimleri kapattı, geri açmayı unuttu",
    "Son maç dedi, sabah oldu",
    "Mikrofonu açık uyuyakaldı",
    "Güncelleme bekliyordu",
    "Wi-Fi'ye yenik düştü",
    "Ranked maçta son nefesini verdi",
    "Ekran paylaşımını kapatmayı unuttu, utancından gitti",
    "Yanlış kanala yazdı, bir daha yüzü tutmadı",
    "Şarj aleti uzaktaydı",
]

KITABE_SOZLERI = [
    "Buralarda bir yerlerde, hâlâ yükleniyor.",
    "Sessizliğe karıştı.",
    "Ping'i sonsuza kadar 999.",
    "Toprağı bol, pingi düşük olsun.",
    "Bir daha çevrimiçi görünmedi.",
    "Çevrimdışı, ama unutulmadı.",
    "Son görülme: çok oldu.",
]

AGITLAR = [
    "Kanallar boş, sesler kısık,\nBir isim eksik listede — hep eksik.",
    "Girdi çıktı bu sunucuya nice yiğit,\nAma hiçbiri senin kadar sessiz gitmedi.",
    "Sabah olur, akşam olur, bildirim gelmez,\nO yeşil nokta bir daha yanmaz.",
    "Ne bir mesaj, ne bir tepki, ne bir ses,\nMezarlıkta yalnız rüzgâr eser.",
    "Herkes bir gün gider derler,\nAma sen gitmedin — sadece çevrimdışı oldun.",
    "Oyunlar oynandı sensiz, maçlar kaybedildi,\nHer yenilgide adın anıldı.",
    "Bir zamanlar bu kanallar senin sesinle dolardı,\nŞimdi sadece yankısı var.",
    "Toprak ağır değil, internet yavaş sadece.\nBekliyoruz, hâlâ bekliyoruz.",
]


def _kisiye_ozel_rastgele(kisi_id: int, tohum: str) -> random.Random:
    """Ayni kisiye hep ayni sonucu vermek icin sabit tohumlu uretec."""
    return random.Random(f"{tohum}:{kisi_id}")


@bot.tree.command(name="mezar-kaz", description="Toprağı kaz, arşivden bir söz çıkar", guild=GUILD)
async def mezar_kaz(interaction: discord.Interaction) -> None:
    if not state.quotes:
        await interaction.response.send_message(
            "Toprak boş — arşiv henüz doldurulmamış.", ephemeral=True
        )
        return

    soz = random.choice(state.quotes)
    embed = discord.Embed(
        title="⛏️ Toprağı kazdın",
        description=f"Çıkan şey:\n\n> {soz}",
        colour=discord.Colour.from_str("#7FA587"),
    )
    embed.set_footer(text=f"Arşivde {len(state.quotes)} söz gömülü")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="sirala", description="En çok mum yakanlar", guild=GUILD)
async def sirala(interaction: discord.Interaction) -> None:
    if not state.candle_counts:
        await interaction.response.send_message(
            "Henüz kimse mum yakmadı.", ephemeral=True
        )
        return

    siralama = sorted(state.candle_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    madalya = ["🥇", "🥈", "🥉"]

    satirlar = []
    for sira, (uye_id, adet) in enumerate(siralama):
        isaret = madalya[sira] if sira < 3 else f"`{sira + 1}.`"
        satirlar.append(f"{isaret} <@{uye_id}> — **{adet}** mum")

    embed = discord.Embed(
        title="🕯️ Mezarlığın Sadıkları",
        description="\n".join(satirlar),
        colour=discord.Colour.from_str("#C9A35A"),
    )
    embed.set_footer(text=f"Toplam {state.candles} mum · {len(state.candle_counts)} ziyaretçi")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="mezar-tasi", description="Birine mezar taşı diker", guild=GUILD)
@app_commands.describe(kisi="Mezar taşı dikilecek kişi")
async def mezar_tasi(interaction: discord.Interaction, kisi: discord.Member) -> None:
    # Kisiye ozel ama sabit: ayni kisinin mezar tasi hep ayni kalsin
    uretec = _kisiye_ozel_rastgele(kisi.id, "mezar-tasi")
    sebep = uretec.choice(OLUM_SEBEPLERI)
    soz = uretec.choice(KITABE_SOZLERI)

    embed = discord.Embed(
        title="🪦 Burada Yatıyor",
        description=(
            f"### {kisi.display_name}\n"
            f"_{soz}_\n\n"
            f"**Ölüm sebebi:** {sebep}"
        ),
        colour=discord.Colour.from_str("#4B5058"),
    )
    embed.set_thumbnail(url=kisi.display_avatar.url)
    embed.set_footer(text=f"{interaction.user.display_name} tarafından dikildi")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="agit", description="Oğuzhan için bir ağıt yakar", guild=GUILD)
async def agit(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        description=f"🥀\n\n*{random.choice(AGITLAR)}*",
        colour=discord.Colour.from_str("#8C4A44"),
    )
    embed.set_footer(text=f"— {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


YORICK_SOZLERI = [
    "Vah zavallı Yorick! Onu tanırdım, sonsuz nükte sahibi bir adamdı.",
    "Bir kafatası tuttum elimde, bana baktı ve hiçbir şey demedi. Tam bir sohbet arkadaşı.",
    "Şu kafatası bir zamanlar sesli kanalda en çok konuşandı.",
    "Herkes bir gün kafatası olur, mesele kimin elinde olacağın.",
    "Yorick sustu, ama sunucu susmadı.",
    "Bu kafatası hiç bildirim açmadı, huzuru öyle buldu.",
    "Ölüm, ping'in sonsuza kadar sabitlenmesidir.",
]

ANIT_YAZILARI = [
    "adına dikilmiştir, sebebi kimse hatırlamıyor",
    "anısına — henüz ölmedi ama hazırlık iyidir",
    "onuruna dikildi, kendisine sorulmadı",
    "için yapıldı, masraflar ortak kasadan karşılandı",
    "adına — bir gün gerekir",
]

HAYIR_ISLERI = [
    "bir sokak kedisi doyuruldu",
    "bir yaşlıya yol tarif edildi",
    "kimseye küfredilmedi (bugünlük)",
    "bir arkadaşın mesajı görmezden gelinmedi",
    "sesli kanalda mikrofon kapatıldı",
    "bir ranked maç sonunda kimse suçlanmadı",
    "birine 'geçmiş olsun' yazıldı",
    "bir mesaj atılmadan önce iki kere düşünüldü",
    "spoiler verilmedi",
    "bir tartışma büyümeden bitirildi",
]


@bot.tree.command(name="kafatasi", description="Yorick'i eline al", guild=GUILD)
async def kafatasi(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="💀",
        description=f"*{random.choice(YORICK_SOZLERI)}*",
        colour=discord.Colour.from_str("#DCD7CB"),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="anit", description="Bugün anıt kimin adına dikili?", guild=GUILD)
async def anit(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return

    adaylar = [uye for uye in guild.members if not uye.bot]
    if not adaylar:
        await interaction.response.send_message("Aday yok.", ephemeral=True)
        return

    # Gune gore sabit: anit gun boyunca ayni kisinin, ertesi gun degisir
    bugun = now().astimezone(TZ).strftime("%Y-%m-%d")
    uretec = random.Random(f"anit:{bugun}:{guild.id}")
    secilen = uretec.choice(adaylar)
    yazi = uretec.choice(ANIT_YAZILARI)

    embed = discord.Embed(
        title="🗿 İsimsiz Anıt",
        description=f"Bugün bu anıt **{secilen.display_name}** {yazi}.",
        colour=discord.Colour.from_str("#8E8B81"),
    )
    embed.set_thumbnail(url=secilen.display_avatar.url)
    embed.set_footer(text="Yarın başkasının olacak")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="hayir", description="Birinin adına bir hayır işle", guild=GUILD)
@app_commands.describe(kisi="Hayrın kimin adına yazılacağı")
async def hayir(interaction: discord.Interaction, kisi: discord.Member) -> None:
    anahtar = str(kisi.id)
    state.hayir_counts[anahtar] = state.hayir_counts.get(anahtar, 0) + 1
    await persist()

    embed = discord.Embed(
        title="🤲 Hayır İşlendi",
        description=(
            f"**{kisi.display_name}** adına {random.choice(HAYIR_ISLERI)}.\n\n"
            f"Toplam hayrı: **{state.hayir_counts[anahtar]}**"
        ),
        colour=discord.Colour.from_str("#7FA587"),
    )
    embed.set_footer(text=f"{interaction.user.display_name} vesile oldu")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="karne", description="Merkezdeki sicilini gösterir", guild=GUILD)
@app_commands.describe(kisi="Kimin karnesi (boş bırakırsan senin)")
async def karne(interaction: discord.Interaction, kisi: Optional[discord.Member] = None) -> None:
    hedef = kisi or interaction.user
    anahtar = str(hedef.id)

    okunan = state.yasin_counts.get(anahtar, 0)
    hayirlar = state.hayir_counts.get(anahtar, 0)
    mumlar = state.candle_counts.get(anahtar, 0)

    if okunan == 0 and hayirlar == 0 and mumlar == 0:
        durum = "Sicili tertemiz. Ya çok iyi biri, ya da hiç uğramamış."
    elif okunan > hayirlar:
        durum = "Hakkında okunan, adına işlenenden fazla. Endişe verici."
    else:
        durum = "Durumu iyi görünüyor."

    embed = discord.Embed(
        title=f"📋 {hedef.display_name} — Sicil",
        description=f"_{durum}_",
        colour=discord.Colour.from_str("#4E6B56"),
    )
    embed.add_field(name="Hayrına okunan Yasin", value=str(okunan), inline=True)
    embed.add_field(name="Adına işlenen hayır", value=str(hayirlar), inline=True)
    embed.add_field(name="Yaktığı mum", value=str(mumlar), inline=True)
    embed.set_thumbnail(url=hedef.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="mezarlik", description="Mezarlığın durumu", guild=GUILD)
async def mezarlik(interaction: discord.Interaction) -> None:
    embed = kitabe_embed()
    embed.title = "🪦 Mezarlık"
    embed.add_field(name="Okunan Yasin", value=str(state.yasin), inline=True)
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
