"""
╔══════════════════════════════════════════════════════════════════════════╗
║                            JINRŌ — Bot Shoen                             ║
║   Loup-Garou hybride condensé. Univers japonais. Made by nemesis.        ║
║   Cycle nuit/jour avec actions en DM, débats publics, votes anonymes.    ║
╚══════════════════════════════════════════════════════════════════════════╝
"""
import discord
from discord.ext import commands, tasks
import os
import sys
import sqlite3
import json
import random
import asyncio
import logging
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ========================= CONFIG =========================
BOT_TOKEN = os.environ.get("TOKEN_JINRO") or os.environ.get("TOKEN")
if not BOT_TOKEN:
    print("[ERREUR CRITIQUE] Aucune variable d'environnement TOKEN_JINRO ni TOKEN trouvée.")
    print("Définis-la avant de lancer le bot (ex: export TOKEN_JINRO=xxx).")
    sys.exit(1)

PARIS_TZ = ZoneInfo("Europe/Paris")
DEFAULT_BUYER_IDS = [625004459491065856]
DEFAULT_PREFIX = "!"

DATA_DIR = os.environ.get("DATA_DIR")
if not DATA_DIR:
    print("[ERREUR CRITIQUE] DATA_DIR non défini. Configure DATA_DIR=/data dans Railway.")
    sys.exit(1)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "jinro.db")

# Timings (en secondes) — modifiables ici
# Timings par défaut (modifiables via !settime, persistants en DB)
DEFAULT_TIMINGS = {
    "recruit":      600,   # recrutement
    "night":         60,   # actions de nuit (voyante, salvateur, petite fille, cupidon, loups en parallèle)
    "witch":         25,   # phase sorcière (après les loups)
    "debate":        90,   # débat de jour
    "vote_day":      45,   # vote du village
    "captain_vote":  45,   # élection capitaine (jour 1)
    "hunter":        30,   # tir du chasseur à sa mort
    "successor":     30,   # capitaine désigne successeur
}
MAX_NIGHTS = 20             # garde-fou anti-boucle infinie

MIN_PLAYERS = 6
MAX_PLAYERS = 24

# Logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%d/%m/%Y %H:%M:%S",
)
log = logging.getLogger("jinro")

stats_lock = asyncio.Lock()
_prefix_cache = {"value": None}


# ========================= DATABASE =========================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS ranks (user_id TEXT PRIMARY KEY, rank INTEGER NOT NULL)")
    c.execute("""CREATE TABLE IF NOT EXISTS bot_bans (
        user_id TEXT PRIMARY KEY, banned_by TEXT, banned_at TEXT
    )""")
    c.execute("CREATE TABLE IF NOT EXISTS log_channels (guild_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL)")
    c.execute("""CREATE TABLE IF NOT EXISTS allowed_channels (
        guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
        added_by TEXT, added_at TEXT,
        PRIMARY KEY (guild_id, channel_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS active_messages (
        guild_id TEXT NOT NULL, user_id TEXT NOT NULL, timestamp TEXT,
        PRIMARY KEY (guild_id, user_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS player_stats (
        user_id TEXT PRIMARY KEY,
        xp INTEGER DEFAULT 0,
        level INTEGER DEFAULT 0,
        games_played INTEGER DEFAULT 0,
        games_won INTEGER DEFAULT 0,
        times_wolf INTEGER DEFAULT 0,
        wolf_wins INTEGER DEFAULT 0,
        times_seer INTEGER DEFAULT 0,
        correct_votes INTEGER DEFAULT 0,
        wrong_votes INTEGER DEFAULT 0,
        times_captain INTEGER DEFAULT 0,
        kills_as_hunter INTEGER DEFAULT 0,
        favorite_role TEXT,
        last_played TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS role_counts (
        user_id TEXT NOT NULL, role_key TEXT NOT NULL, count INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, role_key)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS badges (
        user_id TEXT NOT NULL, badge_key TEXT NOT NULL, unlocked_at TEXT,
        PRIMARY KEY (user_id, badge_key)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS game_history (
        game_id TEXT PRIMARY KEY,
        guild_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        host_id TEXT,
        size_mode TEXT,
        player_count INTEGER,
        winning_camp TEXT,
        nights_played INTEGER,
        started_at TEXT,
        ended_at TEXT,
        participants_json TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS cooldowns (
        user_id TEXT NOT NULL, key TEXT NOT NULL, until TEXT NOT NULL,
        PRIMARY KEY (user_id, key)
    )""")

    c.execute("INSERT OR IGNORE INTO config VALUES ('prefix', ?)", (DEFAULT_PREFIX,))
    c.execute(
        "INSERT OR IGNORE INTO config VALUES ('buyer_ids', ?)",
        (json.dumps([str(i) for i in DEFAULT_BUYER_IDS]),)
    )
    conn.commit()
    conn.close()


# ---- Config ----

def get_config(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def set_config(key, value):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO config VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()
    if key == "prefix":
        _prefix_cache["value"] = str(value)


def get_prefix_cached():
    if _prefix_cache["value"] is None:
        _prefix_cache["value"] = get_config("prefix") or DEFAULT_PREFIX
    return _prefix_cache["value"]


# ---- Timings (dynamiques, persistants) ----

def get_timing(key):
    """Retourne la durée en secondes pour une phase donnée."""
    if key not in DEFAULT_TIMINGS:
        return 60
    val = get_config(f"timing_{key}")
    if val is None:
        return DEFAULT_TIMINGS[key]
    try:
        return max(5, min(3600, int(val)))
    except ValueError:
        return DEFAULT_TIMINGS[key]


def set_timing(key, value):
    if key not in DEFAULT_TIMINGS:
        raise ValueError(f"Phase inconnue : {key}")
    set_config(f"timing_{key}", int(value))


def reset_timing(key):
    if key not in DEFAULT_TIMINGS:
        raise ValueError(f"Phase inconnue : {key}")
    conn = get_db()
    conn.execute("DELETE FROM config WHERE key = ?", (f"timing_{key}",))
    conn.commit()
    conn.close()


# ---- Rangs ----

def get_rank_db(user_id):
    buyer_ids_raw = get_config("buyer_ids")
    if buyer_ids_raw:
        buyer_ids = json.loads(buyer_ids_raw)
        if str(user_id) in buyer_ids:
            return 4
    conn = get_db()
    row = conn.execute("SELECT rank FROM ranks WHERE user_id = ?", (str(user_id),)).fetchone()
    conn.close()
    return row["rank"] if row else 0


def set_rank_db(user_id, rank):
    conn = get_db()
    if rank == 0:
        conn.execute("DELETE FROM ranks WHERE user_id = ?", (str(user_id),))
    else:
        conn.execute("INSERT OR REPLACE INTO ranks VALUES (?, ?)", (str(user_id), rank))
    conn.commit()
    conn.close()


def get_ranks_by_level(level):
    conn = get_db()
    rows = conn.execute("SELECT user_id FROM ranks WHERE rank = ?", (level,)).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def has_min_rank(user_id, minimum):
    return get_rank_db(user_id) >= minimum


def rank_name(level):
    return {4: "Buyer", 3: "Sys", 2: "MJ", 1: "Joueur vérifié", 0: "Aucun"}[level]


# ---- Ban bot ----

def is_bot_banned(user_id):
    conn = get_db()
    row = conn.execute("SELECT 1 FROM bot_bans WHERE user_id = ?", (str(user_id),)).fetchone()
    conn.close()
    return row is not None


def add_bot_ban(user_id, banned_by):
    conn = get_db()
    now = datetime.now(PARIS_TZ).strftime("%d/%m/%Y %Hh%M")
    conn.execute("INSERT OR REPLACE INTO bot_bans VALUES (?, ?, ?)",
                 (str(user_id), str(banned_by), now))
    conn.commit()
    conn.close()


def remove_bot_ban(user_id):
    conn = get_db()
    conn.execute("DELETE FROM bot_bans WHERE user_id = ?", (str(user_id),))
    conn.commit()
    conn.close()


# ---- Log channels ----

def get_log_channel(guild_id):
    conn = get_db()
    row = conn.execute("SELECT channel_id FROM log_channels WHERE guild_id = ?", (str(guild_id),)).fetchone()
    conn.close()
    return row["channel_id"] if row else None


def set_log_channel(guild_id, channel_id):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO log_channels VALUES (?, ?)",
                 (str(guild_id), str(channel_id)))
    conn.commit()
    conn.close()


# ---- Allowed channels ----

def add_allowed_channel(guild_id, channel_id, added_by):
    conn = get_db()
    now = datetime.now(PARIS_TZ).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO allowed_channels (guild_id, channel_id, added_by, added_at) VALUES (?, ?, ?, ?)",
        (str(guild_id), str(channel_id), str(added_by), now)
    )
    conn.commit()
    conn.close()


def remove_allowed_channel(guild_id, channel_id):
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM allowed_channels WHERE guild_id = ? AND channel_id = ?",
        (str(guild_id), str(channel_id))
    )
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted > 0


def get_allowed_channels(guild_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT channel_id FROM allowed_channels WHERE guild_id = ?",
        (str(guild_id),)
    ).fetchall()
    conn.close()
    return [r["channel_id"] for r in rows]


def is_channel_allowed(guild_id, channel_id):
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM allowed_channels WHERE guild_id = ? AND channel_id = ? LIMIT 1",
        (str(guild_id), str(channel_id))
    ).fetchone()
    conn.close()
    return row is not None


# ---- Active messages tracking ----

_active_msg_buffer = {}  # (guild_id, user_id) → timestamp (mémoire, flush périodique)


def track_message(guild_id, user_id):
    """Buffer en mémoire ; flush en background. Évite 1 INSERT par message."""
    _active_msg_buffer[(str(guild_id), str(user_id))] = datetime.now(PARIS_TZ).isoformat()


async def flush_active_messages():
    """Tâche périodique : flush le buffer dans la DB."""
    if not _active_msg_buffer:
        return
    items = list(_active_msg_buffer.items())
    _active_msg_buffer.clear()
    conn = get_db()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO active_messages (guild_id, user_id, timestamp) VALUES (?, ?, ?)",
            [(g, u, ts) for (g, u), ts in items]
        )
        conn.commit()
    except sqlite3.Error as e:
        log.error(f"flush_active_messages: {e}")
    finally:
        conn.close()


# ---- Player stats ----

def get_player_stats(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM player_stats WHERE user_id = ?", (str(user_id),)).fetchone()
    if not row:
        conn.execute("INSERT OR IGNORE INTO player_stats (user_id) VALUES (?)", (str(user_id),))
        conn.commit()
        row = conn.execute("SELECT * FROM player_stats WHERE user_id = ?", (str(user_id),)).fetchone()
    conn.close()
    return dict(row)


_ALLOWED_STAT_FIELDS = {
    "xp", "level", "games_played", "games_won", "times_wolf", "wolf_wins",
    "times_seer", "correct_votes", "wrong_votes", "times_captain",
    "kills_as_hunter", "favorite_role", "last_played"
}


def update_player_stats(user_id, **kwargs):
    for k in kwargs:
        if k not in _ALLOWED_STAT_FIELDS:
            raise ValueError(f"Champ stat invalide : {k}")
    get_player_stats(user_id)
    if not kwargs:
        return
    set_clauses = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [str(user_id)]
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f"UPDATE player_stats SET {set_clauses} WHERE user_id = ?", values)
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        log.error(f"update_player_stats failed: {e}")
    finally:
        conn.close()


def increment_player_stat(user_id, field, delta=1):
    if field not in _ALLOWED_STAT_FIELDS:
        raise ValueError(f"Champ stat invalide : {field}")
    get_player_stats(user_id)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"UPDATE player_stats SET {field} = {field} + ? WHERE user_id = ?",
            (delta, str(user_id))
        )
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        log.error(f"increment_player_stat failed: {e}")
    finally:
        conn.close()


def increment_role_count(user_id, role_key):
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""INSERT INTO role_counts (user_id, role_key, count)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, role_key) DO UPDATE SET count = count + 1""",
            (str(user_id), role_key))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        log.error(f"increment_role_count failed: {e}")
    finally:
        conn.close()


def get_role_counts(user_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT role_key, count FROM role_counts WHERE user_id = ? ORDER BY count DESC",
        (str(user_id),)
    ).fetchall()
    conn.close()
    return [(r["role_key"], r["count"]) for r in rows]


# ---- Badges ----

def unlock_badge(user_id, badge_key):
    conn = get_db()
    now = datetime.now(PARIS_TZ).isoformat()
    cur = conn.execute(
        "INSERT OR IGNORE INTO badges (user_id, badge_key, unlocked_at) VALUES (?, ?, ?)",
        (str(user_id), badge_key, now)
    )
    inserted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return inserted


def get_user_badges(user_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT badge_key, unlocked_at FROM badges WHERE user_id = ? ORDER BY unlocked_at DESC",
        (str(user_id),)
    ).fetchall()
    conn.close()
    return [(r["badge_key"], r["unlocked_at"]) for r in rows]


# ---- Leaderboard ----

def get_leaderboard(metric="xp", limit=10):
    allowed = {"xp", "games_won", "correct_votes", "wolf_wins", "games_played", "kills_as_hunter"}
    if metric not in allowed:
        metric = "xp"
    conn = get_db()
    rows = conn.execute(
        f"SELECT user_id, {metric} as value FROM player_stats "
        f"WHERE {metric} > 0 ORDER BY {metric} DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [(r["user_id"], r["value"]) for r in rows]


# ---- Game history ----

def save_game_history(game_id, guild_id, channel_id, host_id, size_mode,
                      player_count, winning_camp, nights_played, started_at, ended_at,
                      participants):
    conn = get_db()
    conn.execute("""INSERT OR REPLACE INTO game_history VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (game_id, str(guild_id), str(channel_id), str(host_id), size_mode,
         player_count, winning_camp, nights_played,
         started_at, ended_at, json.dumps(participants)))
    conn.commit()
    conn.close()


def get_recent_games(guild_id, limit=10):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM game_history WHERE guild_id = ? ORDER BY ended_at DESC LIMIT ?",
        (str(guild_id), limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- Cooldowns ----

def get_cooldown(user_id, key):
    conn = get_db()
    row = conn.execute(
        "SELECT until FROM cooldowns WHERE user_id = ? AND key = ?",
        (str(user_id), key)
    ).fetchone()
    conn.close()
    if not row:
        return None
    until = datetime.fromisoformat(row["until"])
    if until <= datetime.now(PARIS_TZ):
        return None
    return until


def set_cooldown(user_id, key, seconds):
    conn = get_db()
    until = (datetime.now(PARIS_TZ) + timedelta(seconds=seconds)).isoformat()
    conn.execute("INSERT OR REPLACE INTO cooldowns VALUES (?, ?, ?)",
                 (str(user_id), key, until))
    conn.commit()
    conn.close()


# ========================= HELPERS =========================

FOOTER_TEXT = "Jinrō ・ Shoen ・ made by nemesis"


def embed_color():
    return 0x2b2d31


def success_embed(title, desc=""):
    em = discord.Embed(title=title, description=desc, color=0x43b581)
    em.set_footer(text=FOOTER_TEXT)
    return em


def error_embed(title, desc=""):
    em = discord.Embed(title=title, description=desc, color=0xf04747)
    em.set_footer(text=FOOTER_TEXT)
    return em


def info_embed(title, desc=""):
    em = discord.Embed(title=title, description=desc, color=embed_color())
    em.set_footer(text=FOOTER_TEXT)
    return em


def get_french_time():
    now = datetime.now(PARIS_TZ)
    JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin",
               "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
    return f"{JOURS_FR[now.weekday()]} {now.day} {MOIS_FR[now.month - 1]} {now.year} — {now.strftime('%Hh%M')}"


async def resolve_member(ctx, user_input):
    if not user_input:
        return None
    try:
        member_id = int(user_input.strip("<@!>"))
        m = ctx.guild.get_member(member_id)
        if m:
            return m
    except (ValueError, AttributeError):
        pass
    try:
        return await commands.MemberConverter().convert(ctx, user_input)
    except commands.CommandError:
        return None


async def resolve_user_or_id(ctx, user_input):
    if not user_input:
        return None, None
    raw = user_input.strip()
    cleaned = raw.strip("<@!>")
    try:
        user_id = int(cleaned)
    except ValueError:
        try:
            m = await commands.MemberConverter().convert(ctx, raw)
            return m, m.id
        except commands.CommandError:
            pass
        try:
            u = await commands.UserConverter().convert(ctx, raw)
            return u, u.id
        except commands.CommandError:
            return None, None
    if ctx.guild:
        member = ctx.guild.get_member(user_id)
        if member:
            return member, user_id
    try:
        user = await bot.fetch_user(user_id)
        return user, user_id
    except discord.NotFound:
        return None, user_id
    except discord.HTTPException as e:
        log.warning(f"resolve_user_or_id: fetch_user({user_id}) a échoué : {e}")
        return None, user_id


def format_user_display(display_obj, user_id):
    if display_obj is not None:
        return f"{display_obj.mention} (`{display_obj.id}`)"
    return f"<@{user_id}> (`{user_id}`) *(hors serveur)*"


async def check_ban(ctx):
    if is_bot_banned(ctx.author.id):
        em = error_embed(
            "⛔ Accès refusé",
            "Tu as été **banni du bot Jinrō**.\n"
            "Si tu penses que c'est une erreur, contacte un MJ ou un Sys."
        )
        await ctx.send(embed=em)
        return True
    return False


# ========================= INDICES VOYANTE =========================
# Descriptions narratives par camp. 70% chance d'indice correct, 30% trompeur.

SEER_HINTS = {
    "wolf": [
        "Tu sens une **aura prédatrice** et l'odeur ferreuse du sang.",
        "Ses yeux semblent briller dans la nuit comme ceux d'une bête affamée.",
        "Une **fureur ancienne** sommeille en cette personne.",
        "Tu perçois un grondement sourd, presque animal, sous sa peau.",
        "Son ombre te paraît plus longue que les autres.",
        "L'aura qui l'entoure est **sombre, lourde, dangereuse**.",
        "Quelque chose en elle te fait frissonner — un appel sauvage.",
        "Tu vois des **crocs** dans son sourire fantôme.",
    ],
    "village": [
        "Une **aura paisible** émane d'elle.",
        "Tu sens un cœur simple et droit, sans malice.",
        "Aucune ombre ne te trouble dans son âme.",
        "Sa lumière est faible mais **pure**.",
        "Tu ressens la **tiédeur d'un foyer** près d'elle.",
        "Rien dans son aura ne trahit la bête.",
        "Tu perçois la **fatigue honnête** d'un travailleur.",
        "Son souffle est calme, son esprit serein.",
    ],
    "solo": [  # Tenshi
        "Une **présence céleste**, ni loup ni mortel ordinaire.",
        "Tu ressens une mélancolie surnaturelle, presque divine.",
        "Quelque chose en elle **n'appartient pas à ce monde**.",
        "Une plume invisible te frôle quand tu la regardes.",
    ],
}


def get_seer_hint(true_camp: str):
    """Retourne un indice narratif. 70% juste, 30% trompeur (sans le dire)."""
    rng = random.random()
    if rng < 0.30:
        # Trompeur : indice du camp opposé (loups/village uniquement, Tenshi reste solo si pris)
        if true_camp == "wolf":
            fake = "village"
        elif true_camp == "village":
            fake = "wolf"
        else:  # solo → balancé entre wolf et village
            fake = random.choice(["wolf", "village"])
        return random.choice(SEER_HINTS[fake])
    return random.choice(SEER_HINTS[true_camp])


# ========================= NARRATION DE NUIT =========================
# Messages d'ambiance envoyés dans le salon principal pendant la nuit.

NIGHT_NARRATIONS = {
    "fall":      ("🌑", "**La nuit tombe sur Shoen.** Les villageois s'enferment chez eux, le souffle court..."),
    "miko":      ("🔮", "**La Miko ouvre ses yeux d'aigle.** Elle scrute les âmes, cherchant la noirceur..."),
    "mamori":    ("🛡️", "**Le Mamori veille en silence.** Sa lanterne brûle au-dessus d'un foyer endormi..."),
    "enmusubi":  ("💞", "**L'Enmusubi tisse le fil rouge.** Deux destins se trouvent à jamais liés..."),
    "shojo":     ("👧", "**Une petite porte s'entrouvre dans la pénombre.** Des yeux d'enfant épient les ombres..."),
    "okami":     ("🐺", "**Les Ōkami se rassemblent dans la forêt.** Leurs grognements emplissent la nuit..."),
    "okami_pick": ("🩸", "**La meute a choisi sa proie.** Des griffes raclent une porte de bois..."),
    "majo":      ("🧪", "**La Majo se réveille à son tour.** Elle tend la main vers ses fioles de verre..."),
    "dawn":      ("🌅", "**L'aube se lève sur Shoen.** Le village ose enfin ouvrir les yeux..."),
}


async def narrate(channel, key: str):
    emoji, text = NIGHT_NARRATIONS.get(key, ("", ""))
    if not text:
        return
    em = discord.Embed(description=f"{emoji}  *{text}*", color=0x1f2733)
    em.set_footer(text=FOOTER_TEXT)
    try:
        await channel.send(embed=em)
    except discord.HTTPException as e:
        log.warning(f"narrate({key}): {e}")


# ========================= PSEUDOS DES MORTS =========================

async def mark_dead_nickname(member: discord.Member):
    """Renomme le membre avec '(MORT)' (ou juste 'MORT' si trop long).
    Retourne le nick original (None si pas de nick custom) ou False si échec."""
    if member.guild.me.top_role <= member.top_role and member.id != member.guild.owner_id:
        # Le bot ne peut pas modifier ce membre (rôle supérieur ou owner)
        pass  # tentera quand même
    original_nick = member.nick  # None si pas de nick custom
    base_name = original_nick or member.display_name
    new_nick = f"{base_name} (MORT)"
    if len(new_nick) > 32:
        new_nick = "MORT"
    try:
        await member.edit(nick=new_nick, reason="Jinrō : joueur mort")
        return original_nick
    except discord.Forbidden:
        log.info(f"Pas de perm pour renommer {member} (rôle trop bas ou owner)")
        return False
    except discord.HTTPException as e:
        log.warning(f"Échec rename {member}: {e}")
        return False


async def restore_nickname(member: discord.Member, original_nick):
    """Restaure le nick d'origine. original_nick peut être None (= pas de nick custom)."""
    if original_nick is False:
        return  # on n'a jamais pu le renommer
    try:
        await member.edit(nick=original_nick, reason="Jinrō : fin de partie")
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(f"Échec restore nick {member}: {e}")


# ========================= MUTE DES MORTS =========================

async def mute_dead_player(game, member: discord.Member):
    """Permission override sur les salons autorisés + salon de partie + salon loups + mute vocal."""
    guild = game.guild
    allowed_ids = get_allowed_channels(guild.id)
    # Toujours muter au moins dans le salon de la partie
    target_channels = set(allowed_ids)
    target_channels.add(str(game.channel.id))
    # Et dans le salon des loups si on y a accès (un loup mort ne doit plus pouvoir y poster)
    if game.wolf_channel:
        target_channels.add(str(game.wolf_channel.id))

    for cid in target_channels:
        ch = guild.get_channel(int(cid))
        if not ch:
            continue
        try:
            await ch.set_permissions(
                member,
                send_messages=False,
                add_reactions=False,
                send_messages_in_threads=False,
                reason="Jinrō : joueur mort",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"mute texte échec {member} dans {ch}: {e}")

    # Mute vocal serveur si en vocal
    if member.voice and member.voice.channel:
        try:
            await member.edit(mute=True, reason="Jinrō : joueur mort")
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"mute vocal échec {member}: {e}")


async def unmute_player(game, member: discord.Member):
    """Retire les override sur les salons et démute en vocal."""
    guild = game.guild
    allowed_ids = get_allowed_channels(guild.id)
    target_channels = set(allowed_ids)
    target_channels.add(str(game.channel.id))
    if game.wolf_channel:
        target_channels.add(str(game.wolf_channel.id))

    for cid in target_channels:
        ch = guild.get_channel(int(cid))
        if not ch:
            continue
        try:
            await ch.set_permissions(member, overwrite=None, reason="Jinrō : fin de partie")
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"unmute texte échec {member} dans {ch}: {e}")

    if member.voice and member.voice.channel:
        try:
            await member.edit(mute=False, reason="Jinrō : fin de partie")
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"unmute vocal échec {member}: {e}")


# ========================= SALON PRIVÉ LOUPS =========================

async def create_wolf_channel(game):
    """Crée un salon texte privé pour les loups dans la même catégorie que la partie."""
    guild = game.guild
    wolves = game.alive_wolves()
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True, embed_links=True
        ),
    }
    for w in wolves:
        overwrites[w] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, add_reactions=True
        )
    try:
        channel = await guild.create_text_channel(
            name=f"🐺-meute-{game.game_id[-6:]}",
            overwrites=overwrites,
            category=game.channel.category,
            topic="Salon privé de la meute — discussion et vote",
            reason=f"Jinrō : meute partie {game.game_id}",
        )
        game.wolf_channel = channel
        intro = discord.Embed(
            title="🐺 Bienvenue dans la tanière",
            description=(
                "Vous êtes les **Ōkami** de cette partie.\n"
                "Discutez librement ici — les villageois ne voient rien.\n\n"
                "Chaque nuit, un message de vote apparaîtra. **C'est la majorité qui désigne la victime.**"
            ),
            color=0xe74c3c,
        ).set_footer(text=FOOTER_TEXT)
        await channel.send(content=" ".join(w.mention for w in wolves), embed=intro)
        return channel
    except (discord.Forbidden, discord.HTTPException) as e:
        log.error(f"Création salon loups échec: {e}")
        try:
            await game.channel.send(embed=error_embed(
                "⚠️ Impossible de créer le salon des loups",
                "Le bot manque de la permission **Gérer les salons**. Les loups voteront en DM."
            ))
        except discord.HTTPException:
            pass
        return None


async def add_to_wolf_channel(game, member: discord.Member):
    """Ajoute un joueur infecté au salon des loups."""
    if not game.wolf_channel:
        return
    try:
        await game.wolf_channel.set_permissions(
            member,
            view_channel=True, send_messages=True, read_message_history=True, add_reactions=True,
            reason="Jinrō : infection",
        )
        await game.wolf_channel.send(embed=discord.Embed(
            title="🩸 Un nouveau loup rejoint la meute",
            description=f"{member.mention} a été infecté(e) cette nuit.",
            color=0xe74c3c,
        ).set_footer(text=FOOTER_TEXT))
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(f"add_to_wolf_channel: {e}")


async def delete_wolf_channel(game):
    if not game.wolf_channel:
        return
    try:
        await game.wolf_channel.delete(reason="Jinrō : fin de partie")
    except (discord.Forbidden, discord.HTTPException, discord.NotFound) as e:
        log.warning(f"Suppression salon loups : {e}")
    game.wolf_channel = None


# ========================= XP / CLASSES =========================

def xp_for_level(level):
    return int(100 * (level ** 1.5))


CLASS_TITLES = [
    (0,   "Hyakushō"),       # 百姓 — paysan
    (3,   "Shōnin"),          # 商人 — marchand
    (7,   "Yajū-kari"),       # 野獣狩り — chasseur de bêtes
    (12,  "Inōshishi"),       # 猪 — sanglier (déterminé)
    (18,  "Bushi"),           # 武士 — guerrier
    (25,  "Samurai"),         # 侍
    (35,  "Daimyō"),          # 大名 — seigneur féodal
    (50,  "Ōkami-kari"),      # 狼狩り — chasseur de loups
    (75,  "Tsukimi"),         # 月見 — observateur de la lune
    (100, "Tsukuyomi"),       # 月読 — kami de la lune
]


def class_for_level(level):
    current = CLASS_TITLES[0][1]
    for min_lvl, title in CLASS_TITLES:
        if level >= min_lvl:
            current = title
        else:
            break
    return current


def next_class_info(level):
    for min_lvl, title in CLASS_TITLES:
        if level < min_lvl:
            return title, min_lvl
    return None, None


async def award_xp(user_id, amount):
    """Ajoute XP et gère level-up. Retourne (new_level, leveled_up, new_class|None)."""
    async with stats_lock:
        stats = get_player_stats(user_id)
        new_xp = stats["xp"] + amount
        current_level = stats["level"]
        old_class = class_for_level(current_level)
        new_level = current_level
        while new_level < 100 and new_xp >= xp_for_level(new_level + 1):
            new_level += 1
        new_class = class_for_level(new_level)
        update_player_stats(user_id, xp=new_xp, level=new_level)
    leveled_up = new_level > current_level
    class_changed = (new_class != old_class) and leveled_up
    return new_level, leveled_up, (new_class if class_changed else None)


# ========================= RÔLES =========================
# Camps : "wolf", "village", "solo"
# action_when : "night1" (1re nuit uniquement), "night" (toutes les nuits), "day" (jour),
#               "on_death" (déclenché à la mort), None (passif)

ROLES = {
    # ── CAMP LOUP ─────────────────────────────────────────────
    "okami": {
        "name": "Ōkami",
        "name_fr": "Loup-Garou",
        "emoji": "🐺",
        "camp": "wolf",
        "description": (
            "Tu es un **Ōkami**, un loup-garou. Chaque nuit, avec les autres loups, "
            "vous désignez une victime. Le jour, fais-toi passer pour un villageois."
        ),
        "action_when": "night",
        "action_desc": "Vote pour la victime du soir avec les autres loups.",
    },
    "oyaokami": {
        "name": "Oyaōkami",
        "name_fr": "Infect Père des Loups",
        "emoji": "🩸",
        "camp": "wolf",
        "description": (
            "Tu es l'**Oyaōkami**, le patriarche de la meute. "
            "Une fois dans la partie, à la place de l'attaque, tu peux **infecter** la victime : "
            "elle survit mais devient un loup pour le reste de la partie."
        ),
        "action_when": "night",
        "action_desc": "Infecte la victime de cette nuit (1x partie). Elle rejoint la meute.",
    },

    # ── CAMP VILLAGE ─────────────────────────────────────────
    "murabito": {
        "name": "Murabito",
        "name_fr": "Villageois",
        "emoji": "👤",
        "camp": "village",
        "description": (
            "Tu es un **Murabito**, simple villageois. Pas de pouvoir, "
            "mais ton vote et ta voix dans le débat sont essentiels."
        ),
        "action_when": None,
    },
    "miko": {
        "name": "Miko",
        "name_fr": "Voyante",
        "emoji": "🔮",
        "camp": "village",
        "description": (
            "Tu es la **Miko**, prêtresse aux yeux qui percent l'âme. "
            "Chaque nuit, tu peux scruter un joueur et apprendre son camp."
        ),
        "action_when": "night",
        "action_desc": "Sonde un joueur : tu connaîtras son camp.",
    },
    "majo": {
        "name": "Majo",
        "name_fr": "Sorcière",
        "emoji": "🧪",
        "camp": "village",
        "description": (
            "Tu es la **Majo**. Tu possèdes deux potions pour la partie entière :\n"
            "🍵 **Potion de vie** : sauve la victime des loups cette nuit.\n"
            "☠️ **Potion de mort** : élimine un joueur de ton choix cette nuit.\n"
            "Tu peux n'en utiliser aucune, l'une ou l'autre, ou les deux la même nuit."
        ),
        "action_when": "night",
        "action_desc": "Choisis d'utiliser une de tes deux potions, ou de passer.",
    },
    "karyudo": {
        "name": "Karyūdo",
        "name_fr": "Chasseur",
        "emoji": "🏹",
        "camp": "village",
        "description": (
            "Tu es le **Karyūdo**. Si tu meurs (loups ou vote), tu emportes "
            "quelqu'un avec toi : choisis-le immédiatement."
        ),
        "action_when": "on_death",
        "action_desc": "À ta mort, tu désignes un joueur qui meurt avec toi.",
    },
    "enmusubi": {
        "name": "Enmusubi",
        "name_fr": "Cupidon",
        "emoji": "💞",
        "camp": "village",
        "description": (
            "Tu es l'**Enmusubi**, faiseur de liens. La **première nuit**, tu lies deux joueurs "
            "par un fil rouge. Si l'un meurt, l'autre le suit. Tu peux te choisir parmi eux."
        ),
        "action_when": "night1",
        "action_desc": "Choisis 2 joueurs qui deviendront amoureux. Ils meurent ensemble.",
    },
    "mamori": {
        "name": "Mamori",
        "name_fr": "Salvateur",
        "emoji": "🛡️",
        "camp": "village",
        "description": (
            "Tu es le **Mamori**, gardien des innocents. Chaque nuit, tu protèges un joueur "
            "des loups (mais **pas deux nuits de suite la même personne**). Tu peux te protéger toi."
        ),
        "action_when": "night",
        "action_desc": "Protège un joueur de l'attaque des loups (interdit : la même cible 2 nuits d'affilée).",
    },
    "shojo": {
        "name": "Shōjo",
        "name_fr": "Petite Fille",
        "emoji": "👧",
        "camp": "village",
        "description": (
            "Tu es la **Shōjo**. Tu peux entrouvrir la porte la nuit pour épier les loups : "
            "tu vois leur discussion. Mais **35% de chance d'être repérée** — auquel cas "
            "tu deviens automatiquement la victime de cette nuit."
        ),
        "action_when": "night",
        "action_desc": "Espionne les loups cette nuit (risqué).",
    },
    "soncho": {
        "name": "Sonchō",
        "name_fr": "Capitaine",
        "emoji": "👑",
        "camp": "village",
        "description": (
            "Tu es le **Sonchō**, élu par les villageois le **premier jour**. "
            "Ton vote compte **double**. En cas d'égalité, tu tranches. "
            "Si tu meurs, tu désignes ton successeur."
        ),
        "action_when": None,
    },

    # ── CAMP SOLO ─────────────────────────────────────────────
    "tenshi": {
        "name": "Tenshi",
        "name_fr": "Ange",
        "emoji": "😇",
        "camp": "solo",
        "description": (
            "Tu es le **Tenshi**, un esprit déchu. Ton seul objectif : "
            "te faire **éliminer au vote du premier jour**. "
            "Si tu réussis, tu gagnes seul. Si tu rates, tu deviens un simple villageois."
        ),
        "action_when": None,
    },
}


# Le Sonchō n'est pas distribué : il est élu au jour 1. On le retire de la pool de distribution.
ASSIGNABLE_ROLES = [k for k in ROLES.keys() if k != "soncho"]


# ========================= COMPOSITION DES RÔLES =========================
# 6 joueurs : 1 LG + 1 Voyante + 1 Sorcière + 3 villageois (variantes)
# 8 joueurs : 2 LG + Voyante + Sorcière + Chasseur + Cupidon + 2 villageois
# 10 joueurs : 2 LG + Voyante + Sorcière + Chasseur + Cupidon + Salvateur + Ange + 2 villageois
# 12 joueurs : 3 LG + Voyante + Sorcière + Chasseur + Cupidon + Salvateur + Petite Fille + 3 villageois
# 14+ : ajoute Oyaōkami à la place d'un LG normal, plus de villageois

def build_role_composition(player_count):
    """Retourne une liste de clés de rôles. La taille = player_count exactement.
    Le Sonchō n'est pas distribué (élu)."""
    if player_count < MIN_PLAYERS:
        raise ValueError(f"Minimum {MIN_PLAYERS} joueurs.")
    if player_count > MAX_PLAYERS:
        raise ValueError(f"Maximum {MAX_PLAYERS} joueurs.")

    # Nombre de loups : ~ 1/4 des joueurs, mini 1
    wolf_count = max(1, player_count // 4)

    roles = []
    # Loups (1 Oyaōkami à partir de 10 joueurs, puis loups standards)
    if player_count >= 10 and wolf_count >= 2:
        roles.append("oyaokami")
        wolf_count -= 1
    for _ in range(wolf_count):
        roles.append("okami")

    # Village — pouvoirs spéciaux dans l'ordre d'importance
    pool = []
    pool.append("miko")             # Voyante : indispensable
    pool.append("majo")             # Sorcière : indispensable
    if player_count >= 7:
        pool.append("karyudo")      # Chasseur
    if player_count >= 8:
        pool.append("enmusubi")     # Cupidon
    if player_count >= 9:
        pool.append("mamori")       # Salvateur
    if player_count >= 10:
        pool.append("tenshi")       # Ange (solo)
    if player_count >= 12:
        pool.append("shojo")        # Petite Fille

    for r in pool:
        if len(roles) < player_count:
            roles.append(r)

    # Complète avec des villageois
    while len(roles) < player_count:
        roles.append("murabito")

    return roles[:player_count]


# ========================= PRESETS DE TAILLE =========================

SIZE_PRESETS = {
    "small": {
        "emoji": "🌾",
        "label": "Petit village",
        "description": "6 à 8 joueurs — Partie courte, rôles de base",
        "min": 6, "max": 8, "default": 7,
    },
    "medium": {
        "emoji": "🏘️",
        "label": "Village moyen",
        "description": "8 à 12 joueurs — Format équilibré, rôles variés",
        "min": 8, "max": 12, "default": 10,
    },
    "large": {
        "emoji": "🏯",
        "label": "Grand village",
        "description": "12 à 18 joueurs — Tous les rôles + Oyaōkami",
        "min": 12, "max": 18, "default": 14,
    },
    "massive": {
        "emoji": "⛩️",
        "label": "Domaine entier",
        "description": "18 à 24 joueurs — Format event, plus de loups",
        "min": 18, "max": 24, "default": 20,
    },
    "custom": {
        "emoji": "✏️",
        "label": "Personnalisée",
        "description": f"Tu choisis le nombre exact ({MIN_PLAYERS} à {MAX_PLAYERS})",
        "min": MIN_PLAYERS, "max": MAX_PLAYERS, "default": 10,
    },
}


# ========================= BADGES =========================

BADGES = {
    # Progression
    "first_game":   {"emoji": "🌅", "name": "Première nuit",            "desc": "Participer à sa première partie"},
    "played_10":    {"emoji": "🏮", "name": "Visage connu",             "desc": "10 parties jouées"},
    "played_50":    {"emoji": "🏘️", "name": "Pilier du village",       "desc": "50 parties jouées"},
    "played_100":   {"emoji": "⛩️", "name": "Légende de Shoen",         "desc": "100 parties jouées"},

    # Victoires
    "first_win":    {"emoji": "🏆", "name": "Première lune",            "desc": "Gagner sa première partie"},
    "won_10":       {"emoji": "🥇", "name": "Survivant",                "desc": "10 victoires"},
    "won_25":       {"emoji": "💎", "name": "Triomphe répété",          "desc": "25 victoires"},

    # Village
    "first_catch":  {"emoji": "🎯", "name": "Première chasse",          "desc": "Voter contre un loup pour la première fois"},
    "sharp_eye":    {"emoji": "🔍", "name": "Œil affûté",               "desc": "10 votes justes contre un loup"},
    "inquisitor":   {"emoji": "🕵️", "name": "Inquisiteur",              "desc": "25 votes justes contre un loup"},

    # Loups
    "first_howl":   {"emoji": "🌙", "name": "Premier hurlement",        "desc": "Gagner pour la première fois en loup"},
    "wolfpack":     {"emoji": "🐺", "name": "Meute",                    "desc": "10 victoires en loup"},
    "alpha":        {"emoji": "🩸", "name": "Alpha",                    "desc": "25 victoires en loup"},

    # Spécifiques
    "captain_5":    {"emoji": "👑", "name": "Sonchō",                   "desc": "Être élu Capitaine 5 fois"},
    "hunter_kill":  {"emoji": "🏹", "name": "Ne meurt jamais seul",     "desc": "Emporter quelqu'un avec soi en Chasseur"},
    "role_collector": {"emoji": "🎲", "name": "Caméléon",               "desc": "Jouer au moins 5 rôles différents"},
    "samurai":      {"emoji": "🗡️", "name": "Samurai",                   "desc": "Atteindre le niveau 25"},
    "tsukuyomi":    {"emoji": "🌑", "name": "Tsukuyomi",                "desc": "Atteindre le niveau 100"},
    "never_wrong":  {"emoji": "🧠", "name": "Jamais tort",              "desc": "5 votes justes sans un seul faux"},
}


def check_and_award_badges(user_id):
    stats = get_player_stats(user_id)
    role_counts = dict(get_role_counts(user_id))
    owned = {b for b, _ in get_user_badges(user_id)}
    newly = []

    def try_unlock(key, condition):
        if condition and key not in owned and unlock_badge(user_id, key):
            newly.append(key)

    try_unlock("first_game", stats["games_played"] >= 1)
    try_unlock("played_10",  stats["games_played"] >= 10)
    try_unlock("played_50",  stats["games_played"] >= 50)
    try_unlock("played_100", stats["games_played"] >= 100)

    try_unlock("first_win",  stats["games_won"] >= 1)
    try_unlock("won_10",     stats["games_won"] >= 10)
    try_unlock("won_25",     stats["games_won"] >= 25)

    try_unlock("first_catch", stats["correct_votes"] >= 1)
    try_unlock("sharp_eye",   stats["correct_votes"] >= 10)
    try_unlock("inquisitor",  stats["correct_votes"] >= 25)

    try_unlock("first_howl",  stats["wolf_wins"] >= 1)
    try_unlock("wolfpack",    stats["wolf_wins"] >= 10)
    try_unlock("alpha",       stats["wolf_wins"] >= 25)

    try_unlock("captain_5",   stats["times_captain"] >= 5)
    try_unlock("hunter_kill", stats["kills_as_hunter"] >= 1)
    try_unlock("role_collector", len(role_counts) >= 5)

    try_unlock("samurai", stats["level"] >= 25)
    try_unlock("tsukuyomi", stats["level"] >= 100)

    try_unlock("never_wrong", stats["correct_votes"] >= 5 and stats["wrong_votes"] == 0)

    return newly


# ========================= BOT SETUP =========================

init_db()
intents = discord.Intents.default()
intents.members = True
intents.message_content = True


def get_prefix(bot, message):
    return get_prefix_cached()


bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)


# ========================= GLOBAL CHANNEL CHECK =========================

class ChannelNotAllowedError(commands.CheckFailure):
    pass


@bot.check
async def check_allowed_channel(ctx):
    if has_min_rank(ctx.author.id, 3):
        return True
    if ctx.guild is None:
        return True
    if is_channel_allowed(ctx.guild.id, ctx.channel.id):
        return True
    raise ChannelNotAllowedError("Salon non autorisé.")


# ========================= EVENTS =========================

@bot.event
async def on_ready():
    log.info(f"Jinrō connecté : {bot.user} ({bot.user.id})")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.playing, name="la nuit des loups à Shoen")
    )
    if not cleanup_loop.is_running():
        cleanup_loop.start()
    if not flush_loop.is_running():
        flush_loop.start()


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.guild:
        track_message(message.guild.id, message.author.id)
    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandInvokeError):
        error = error.original
    if isinstance(error, ChannelNotAllowedError):
        try:
            await ctx.message.add_reaction("❌")
        except discord.HTTPException:
            pass
        return
    if isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
        await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Impossible de trouver cet utilisateur."))
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=error_embed("❌ Argument manquant", f"Argument manquant : `{error.param.name}`."))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=error_embed("❌ Argument invalide", str(error)))
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(embed=error_embed("⏰ Cooldown", f"Reviens dans {int(error.retry_after)}s."))
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        log.error(
            f"Erreur non gérée '{ctx.command}' par {ctx.author} : {error}\n"
            + "".join(traceback.format_exception(type(error), error, error.__traceback__))
        )
        try:
            await ctx.send(embed=error_embed(
                "❌ Erreur interne",
                "Une erreur inattendue est survenue. Les logs ont été générés."
            ))
        except discord.HTTPException:
            pass


# ========================= TÂCHES PÉRIODIQUES =========================

@tasks.loop(minutes=1)
async def flush_loop():
    try:
        await flush_active_messages()
    except Exception as e:
        log.error(f"flush_loop: {e}")


@tasks.loop(hours=24)
async def cleanup_loop():
    """Nettoie les tables qui grossissent (cooldowns expirés, anciens messages)."""
    try:
        now_iso = datetime.now(PARIS_TZ).isoformat()
        cutoff = (datetime.now(PARIS_TZ) - timedelta(days=30)).isoformat()
        conn = get_db()
        conn.execute("DELETE FROM cooldowns WHERE until < ?", (now_iso,))
        conn.execute("DELETE FROM active_messages WHERE timestamp < ?", (cutoff,))
        conn.commit()
        conn.close()
        log.info("cleanup_loop: tables nettoyées")
    except Exception as e:
        log.error(f"cleanup_loop: {e}")


# ========================= LOG =========================

async def send_log(guild, action, author, target=None, desc=None, color=0x2b2d31):
    channel_id = get_log_channel(guild.id)
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if not channel:
        return
    em = discord.Embed(title=f"📋 {action}", color=color)
    em.add_field(name="Auteur", value=f"{author.mention} (`{author.id}`)", inline=True)
    if target:
        em.add_field(name="Cible", value=f"{target.mention} (`{target.id}`)", inline=True)
    if desc:
        em.add_field(name="Détail", value=desc, inline=False)
    em.set_footer(text=f"{FOOTER_TEXT} ・ {get_french_time()}")
    try:
        await channel.send(embed=em)
    except discord.HTTPException as e:
        log.warning(f"send_log: impossible d'envoyer dans {channel.id} : {e}")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                      PARTIE 2 — CŒUR DU JEU                              ║
# ║  Cycle : RECRUITING → ROLES → (NIGHT → DAY → VOTE)* → RESOLUTION         ║
# ╚══════════════════════════════════════════════════════════════════════════╝

active_games = {}


class Game:
    def __init__(self, ctx, size_mode, player_count):
        self.game_id = f"{ctx.guild.id}-{ctx.channel.id}-{int(datetime.now(PARIS_TZ).timestamp())}"
        self.ctx = ctx
        self.guild = ctx.guild
        self.channel = ctx.channel
        self.host = ctx.author
        self.size_mode = size_mode
        self.target_player_count = player_count

        # État
        self.phase = "SETUP"
        self.participants = []         # liste de discord.Member
        self.roles_assignment = {}     # user_id → role_key
        self.alive = set()             # user_ids vivants
        self.dead = []                 # liste ordonnée des morts {user_id, cause, night}
        self.lovers = set()            # user_ids des amoureux (paire)
        self.captain_id = None
        self.night_number = 0

        # Pouvoirs uniques / consommables
        self.majo_heal_used = False
        self.majo_poison_used = False
        self.oyaokami_infect_used = False
        self.last_mamori_target = None  # user_id protégé la nuit précédente

        # Actions de la nuit courante
        self.night_actions = {}        # user_id → {"action": ..., "target": ..., "data": ...}
        self.wolf_votes = {}           # wolf_id → target_id (vote des loups)
        self.current_wolf_target = None  # victime déterminée après vote des loups (pour la sorcière)

        # Modifs serveur à restaurer en fin de partie
        self.original_nicks = {}       # user_id → original_nick (None ou str ou False)
        self.muted_players = set()     # user_ids qui ont été mutés
        self.wolf_channel = None       # discord.TextChannel | None

        # Résolution
        self.winning_camp = None       # "wolf" / "village" / "lovers" / "tenshi"
        self.winners = set()           # user_ids gagnants

        # Messages
        self.recruiting_message = None
        self.wolf_vote_view = None     # WolfVoteView en cours (None si entre les nuits)

        # Timing
        self.started_at = datetime.now(PARIS_TZ)
        self.ended_at = None

    # ---- Helpers ----

    def get_member(self, user_id):
        for m in self.participants:
            if m.id == user_id:
                return m
        return None

    def get_role(self, user_id):
        key = self.roles_assignment.get(user_id)
        return ROLES[key] if key else None

    def role_key(self, user_id):
        return self.roles_assignment.get(user_id)

    def alive_players(self):
        return [m for m in self.participants if m.id in self.alive]

    def alive_by_camp(self, camp):
        return [m for m in self.alive_players() if self.get_role(m.id)["camp"] == camp]

    def alive_wolves(self):
        return [m for m in self.alive_players() if self.get_role(m.id)["camp"] == "wolf"]

    def alive_non_wolves(self):
        return [m for m in self.alive_players() if self.get_role(m.id)["camp"] != "wolf"]

    def is_wolf(self, user_id):
        role = self.get_role(user_id)
        return role and role["camp"] == "wolf"

    def add_death(self, user_id, cause, night=None):
        if user_id in self.alive:
            self.alive.discard(user_id)
            self.dead.append({
                "user_id": user_id,
                "cause": cause,
                "night": night if night is not None else self.night_number,
            })
            return True
        return False


# ========================= GAMEMANAGER =========================

class GameManager:

    @staticmethod
    async def start_game(ctx, size_mode, player_count):
        if ctx.channel.id in active_games:
            return await ctx.send(embed=error_embed(
                "❌ Partie en cours",
                "Une partie de Jinrō est déjà active dans ce salon."
            ))

        game = Game(ctx, size_mode, player_count)
        active_games[ctx.channel.id] = game
        try:
            await GameManager.run_recruiting(game)
            if game.phase == "ENDED":
                return
            await GameManager.run_roles_assignment(game)
            await create_wolf_channel(game)
            await GameManager.run_captain_election(game)
            await GameManager.run_game_loop(game)
            await GameManager.run_resolution(game)
        except Exception as e:
            log.error(f"Erreur partie {game.game_id}: {e}\n{traceback.format_exc()}")
            try:
                await ctx.send(embed=error_embed(
                    "❌ Partie interrompue",
                    "Une erreur est survenue. La partie est annulée."
                ))
            except discord.HTTPException:
                pass
        finally:
            # Cleanup garanti : restore pseudos, unmute, supprime le salon loups
            try:
                await GameManager.cleanup_game_state(game)
            except Exception as e:
                log.error(f"cleanup_game_state: {e}")
            active_games.pop(ctx.channel.id, None)

    @staticmethod
    async def cleanup_game_state(game):
        """Restaure tout ce qui a été modifié sur le serveur."""
        # Pseudos
        for uid, original in list(game.original_nicks.items()):
            member = game.guild.get_member(uid)
            if member:
                await restore_nickname(member, original)
        # Mutes
        for uid in list(game.muted_players):
            member = game.guild.get_member(uid)
            if member:
                await unmute_player(game, member)
        # Salon loups
        await delete_wolf_channel(game)

    # ─────────────────── PHASE 1 : RECRUTEMENT ────────────────────

    @staticmethod
    async def run_recruiting(game: Game):
        game.phase = "RECRUITING"
        view = RecruitingView(game, timeout=get_timing("recruit"))
        em = GameManager.build_recruiting_embed(game)
        msg = await game.channel.send(embed=em, view=view)
        game.recruiting_message = msg
        view.message = msg
        game.participants.append(game.host)
        await view.wait()

        if view.cancelled or len(game.participants) < MIN_PLAYERS:
            game.phase = "ENDED"
            try:
                await msg.edit(
                    embed=error_embed(
                        "❌ Partie annulée",
                        f"Pas assez de joueurs (minimum {MIN_PLAYERS}) ou partie annulée."
                    ),
                    view=None,
                )
            except discord.HTTPException:
                pass
            return

        game.target_player_count = len(game.participants)

    @staticmethod
    def build_recruiting_embed(game: Game):
        participants_list = "\n".join(
            f"• {m.mention}" + (" *(hôte)*" if m.id == game.host.id else "")
            for m in game.participants
        ) if game.participants else "*Aucun inscrit pour l'instant*"

        size_preset = SIZE_PRESETS.get(game.size_mode, SIZE_PRESETS["custom"])
        em = discord.Embed(
            title="🌙 Une nouvelle nuit tombe sur Shoen",
            description=(
                f"Le domaine est rongé par une menace ancienne. Des loups marchent parmi nous...\n\n"
                f"📋 Mode : {size_preset['emoji']} **{size_preset['label']}**\n"
                f"👥 Objectif : **{game.target_player_count}** joueurs ・ "
                f"Inscrits : **{len(game.participants)}**\n"
                f"👑 Hôte : {game.host.mention}\n\n"
                f"**Joueurs :**\n{participants_list}\n\n"
                f"*Clique sur **Rejoindre** pour entrer dans la partie.*"
            ),
            color=0x3498db,
        )
        em.set_footer(text=FOOTER_TEXT)
        return em

    # ─────────────────── PHASE 2 : ATTRIBUTION DES RÔLES ────────────────────

    @staticmethod
    async def run_roles_assignment(game: Game):
        game.phase = "ROLES"
        n = len(game.participants)
        role_keys = build_role_composition(n)
        random.shuffle(role_keys)
        players_shuffled = list(game.participants)
        random.shuffle(players_shuffled)

        for member, role_key in zip(players_shuffled, role_keys):
            game.roles_assignment[member.id] = role_key
            game.alive.add(member.id)

        # Annonce publique
        em = discord.Embed(
            title="🎴 Les rôles ont été distribués",
            description=(
                f"**{n}** joueurs vont recevoir leur rôle en message privé.\n\n"
                f"*Si tes DMs sont fermés, préviens immédiatement un MJ.*\n"
                f"*La première nuit tombe dans quelques secondes...*"
            ),
            color=0x9b59b6,
        )
        em.set_footer(text=FOOTER_TEXT)
        await game.channel.send(embed=em)

        # DM des rôles
        tasks_dm = [GameManager.dm_role(game, m, game.roles_assignment[m.id])
                    for m in game.participants]
        await asyncio.gather(*tasks_dm, return_exceptions=True)

        # Stats : incrément compteurs de rôles joués
        for uid, rkey in game.roles_assignment.items():
            increment_role_count(uid, rkey)
            if rkey in ("okami", "oyaokami"):
                increment_player_stat(uid, "times_wolf", 1)
            if rkey == "miko":
                increment_player_stat(uid, "times_seer", 1)

        await asyncio.sleep(6)

    @staticmethod
    async def dm_role(game: Game, member: discord.Member, role_key: str):
        role = ROLES[role_key]
        description = (
            f"🎴 Ton rôle : **{role['name']}** ({role['name_fr']})\n"
            f"🏯 Camp : **{GameManager.camp_display(role['camp'])}**\n\n"
            f"{role['description']}"
        )

        # Info spéciale pour les loups : voient leurs alliés
        if role["camp"] == "wolf":
            allies = [m for m in game.participants
                      if game.roles_assignment.get(m.id) in ("okami", "oyaokami") and m.id != member.id]
            if allies:
                description += "\n\n🐺 **Tes alliés loups :**\n"
                for a in allies:
                    r = ROLES[game.roles_assignment[a.id]]
                    description += f"• {a.display_name} — *{r['name_fr']}*\n"
            else:
                description += "\n\n🐺 *Tu es le seul loup.*"

        em = discord.Embed(
            title="🌙 Bienvenue à Shoen",
            description=description,
            color=0xe74c3c if role["camp"] == "wolf" else (0xf1c40f if role["camp"] == "solo" else 0x3498db),
        )
        em.set_footer(text=f"{FOOTER_TEXT} ・ Partie #{game.game_id[-8:]}")
        try:
            await member.send(embed=em)
        except discord.Forbidden:
            log.info(f"DM fermé pour {member} ({member.id}), rôle {role_key}")
            try:
                await game.channel.send(embed=error_embed(
                    "⚠️ DM fermé",
                    f"{member.mention}, tes DMs sont fermés. Demande à un MJ ton rôle en privé."
                ))
            except discord.HTTPException:
                pass
        except discord.HTTPException as e:
            log.warning(f"Échec DM rôle à {member}: {e}")

    @staticmethod
    def camp_display(camp):
        return {"wolf": "🐺 Loup", "village": "🏯 Village", "solo": "✨ Solo"}.get(camp, camp)

    # ─────────────────── PHASE 3 : ÉLECTION DU CAPITAINE ────────────────────

    @staticmethod
    async def run_captain_election(game: Game):
        captain_dur = get_timing("captain_vote")
        em = discord.Embed(
            title="👑 Élection du Sonchō",
            description=(
                f"Avant que la nuit ne tombe, le village doit choisir son **Sonchō (Capitaine)**.\n\n"
                f"Le Sonchō a un **vote qui compte double** et tranche en cas d'égalité.\n"
                f"S'il meurt, il désigne son successeur.\n\n"
                f"⏳ **{captain_dur} secondes** pour voter."
            ),
            color=0xf1c40f,
        )
        em.set_footer(text=FOOTER_TEXT)
        view = CaptainVoteView(game, timeout=captain_dur)
        msg = await game.channel.send(embed=em, view=view)
        view.message = msg
        await view.wait()

        # Comptage
        tally = {}
        for voter_id, target_id in view.votes.items():
            tally[target_id] = tally.get(target_id, 0) + 1

        if not tally:
            # Personne n'a voté : tire au sort
            chosen = random.choice(game.alive_players())
            game.captain_id = chosen.id
        else:
            max_v = max(tally.values())
            top = [uid for uid, v in tally.items() if v == max_v]
            game.captain_id = random.choice(top)

        increment_player_stat(game.captain_id, "times_captain", 1)
        captain_member = game.get_member(game.captain_id)
        em = discord.Embed(
            title="👑 Nouveau Sonchō",
            description=f"Le village a élu **{captain_member.display_name}** comme Sonchō.",
            color=0xf1c40f,
        )
        em.set_footer(text=FOOTER_TEXT)
        await game.channel.send(embed=em)
        await asyncio.sleep(3)

    # ─────────────────── PHASE 4 : BOUCLE DE JEU ────────────────────

    @staticmethod
    async def run_game_loop(game: Game):
        """Boucle nuit → jour → vote jusqu'à condition de victoire."""
        while game.night_number < MAX_NIGHTS:
            game.night_number += 1
            game.phase = "NIGHT"
            await GameManager.run_night(game)

            # Check victoire après les morts de la nuit
            if GameManager.check_win_conditions(game):
                return

            game.phase = "DAY"
            await GameManager.run_day(game)
            if GameManager.check_win_conditions(game):
                return

        # Garde-fou
        log.warning(f"Partie {game.game_id} a atteint MAX_NIGHTS={MAX_NIGHTS}")

    # ─────────────────── PHASE NUIT (séquencée + narration) ────────────────────

    @staticmethod
    async def run_night(game: Game):
        """Phase de nuit : séquencement narratif.
        Bloc 1 (en parallèle, ~night sec) : Voyante, Salvateur, Petite Fille, Cupidon (nuit 1), Loups (salon dédié)
        Annonce intermédiaire + cible des loups
        Bloc 2 (~witch sec) : Sorcière avec info de la victime
        Résolution finale + aube
        """
        game.night_actions = {}
        game.wolf_votes = {}
        game.current_wolf_target = None
        night_dur = get_timing("night")
        witch_dur = get_timing("witch")

        # === Annonce de la nuit ===
        em = discord.Embed(
            title=f"🌑 Nuit {game.night_number}",
            description=(
                "Le village s'endort. Les loups et les esprits s'éveillent...\n\n"
                f"⏳ La nuit dure environ **{night_dur + witch_dur} secondes**."
            ),
            color=0x1f2733,
        )
        em.set_footer(text=FOOTER_TEXT)
        await game.channel.send(embed=em)
        await asyncio.sleep(2)
        await narrate(game.channel, "fall")

        # === Lance les actions de nuit en parallèle (DM + salon loups) ===
        active_roles = set()
        dm_tasks = []
        for m in game.alive_players():
            rkey = game.roles_assignment[m.id]
            role = ROLES[rkey]
            when = role.get("action_when")

            if when == "night1" and game.night_number == 1:
                active_roles.add(rkey)
                if rkey == "enmusubi":
                    dm_tasks.append(GameManager.send_night_action_dm(game, m, rkey, night_dur))
            elif when == "night":
                # Les loups votent dans leur salon, pas en DM
                if rkey in ("okami", "oyaokami"):
                    active_roles.add(rkey)
                elif rkey == "majo":
                    active_roles.add(rkey)  # joue dans bloc 2
                else:
                    active_roles.add(rkey)
                    dm_tasks.append(GameManager.send_night_action_dm(game, m, rkey, night_dur))

        await asyncio.gather(*dm_tasks, return_exceptions=True)

        # Lance le vote des loups dans leur salon (parallèle au bloc 1)
        wolf_vote_task = None
        if any(r in active_roles for r in ("okami", "oyaokami")):
            wolf_vote_task = asyncio.create_task(GameManager.run_wolf_phase(game, night_dur))

        # === Narration échelonnée pendant le bloc 1 ===
        narration_task = asyncio.create_task(
            GameManager.narrate_night_block1(game, night_dur, active_roles)
        )

        # Attendre la durée du bloc 1
        await asyncio.sleep(night_dur)

        # Fin de phase : on attend la narration et on stoppe le vote loups
        try:
            await asyncio.wait_for(narration_task, timeout=3)
        except asyncio.TimeoutError:
            narration_task.cancel()

        if wolf_vote_task:
            if not wolf_vote_task.done():
                wolf_vote_task.cancel()
                try:
                    await wolf_vote_task
                except asyncio.CancelledError:
                    pass

        # Disable le message de vote des loups pour figer leur choix
        if game.wolf_vote_view:
            game.wolf_vote_view.stop()
            try:
                for item in game.wolf_vote_view.children:
                    item.disabled = True
                if game.wolf_vote_view.message:
                    await game.wolf_vote_view.message.edit(view=game.wolf_vote_view)
            except discord.HTTPException:
                pass

        # === Pré-calcul de la cible des loups (pour la sorcière) ===
        game.current_wolf_target = GameManager.compute_wolf_target(game)

        # === Bloc 2 : Sorcière (si vivante et pas encore résolue) ===
        majo_alive = next(
            (m for m in game.alive_players()
             if game.roles_assignment[m.id] == "majo"
             and (not game.majo_heal_used or not game.majo_poison_used)),
            None
        )
        if majo_alive:
            await narrate(game.channel, "majo")
            await GameManager.send_witch_dm(game, majo_alive, witch_dur)
            await asyncio.sleep(witch_dur)

        # === Aube ===
        await narrate(game.channel, "dawn")
        await asyncio.sleep(2)

        # === Résolution finale ===
        await GameManager.resolve_night(game)

    @staticmethod
    async def narrate_night_block1(game, total_duration, active_roles):
        """Diffuse les messages d'ambiance pendant la nuit (échelonnés sur total_duration)."""
        # On répartit les annonces sur les 80% premiers de la durée
        events = []
        if "miko" in active_roles:
            events.append("miko")
        if "mamori" in active_roles:
            events.append("mamori")
        if "enmusubi" in active_roles:
            events.append("enmusubi")
        if "shojo" in active_roles:
            events.append("shojo")
        if any(r in active_roles for r in ("okami", "oyaokami")):
            events.append("okami")

        if not events:
            return

        # Mélange l'ordre pour varier
        random.shuffle(events)
        # Premier message après 4-6s, puis espacement régulier
        usable = max(total_duration - 8, 10)
        step = max(usable // (len(events) + 1), 4)
        await asyncio.sleep(min(5, total_duration // 4))

        for ev in events:
            await narrate(game.channel, ev)
            await asyncio.sleep(step)

    @staticmethod
    async def run_wolf_phase(game, duration):
        """Affiche le panneau de vote des loups dans leur salon dédié."""
        if not game.wolf_channel:
            return
        targets = [m for m in game.alive_players() if not game.is_wolf(m.id)]
        if not targets:
            return
        em = discord.Embed(
            title=f"🐺 Vote de la meute — Nuit {game.night_number}",
            description=(
                "Choisissez votre victime. **La majorité l'emporte.**\n"
                f"⏳ Vous avez environ **{duration} secondes**.\n\n"
                f"*Loups vivants :* {', '.join(w.mention for w in game.alive_wolves())}"
            ),
            color=0xe74c3c,
        ).set_footer(text=FOOTER_TEXT)
        view = WolfVoteView(game, targets, timeout=duration)
        msg = await game.wolf_channel.send(embed=em, view=view)
        view.message = msg
        game.wolf_vote_view = view
        try:
            await view.wait()
        except asyncio.CancelledError:
            view.stop()
            raise

    @staticmethod
    def compute_wolf_target(game):
        """Détermine la victime des loups (majorité, départage aléatoire)."""
        if not game.wolf_votes:
            return None
        tally = {}
        for tgt in game.wolf_votes.values():
            tally[tgt] = tally.get(tgt, 0) + 1
        max_v = max(tally.values())
        top = [t for t, v in tally.items() if v == max_v]
        return random.choice(top)

    @staticmethod
    async def send_night_action_dm(game: Game, member: discord.Member, role_key: str, duration: int):
        """DM individuel pour les actions de nuit (sauf loups : salon dédié, et sorcière : bloc 2)."""
        role = ROLES[role_key]
        others_alive = [m for m in game.alive_players() if m.id != member.id]

        if role_key == "miko":
            view = SimpleActionView(game, member, role_key, others_alive, timeout=duration)
            em = discord.Embed(
                title="🔮 La Miko ouvre les yeux",
                description=(
                    f"Nuit {game.night_number}. Tu sondes l'âme d'un villageois.\n\n"
                    f"⚠️ *Tes visions sont parfois trompées par la peur ou les ombres.*\n\n"
                    f"Choisis ta cible."
                ),
                color=0x3498db,
            )

        elif role_key == "enmusubi":
            view = EnmusubiActionView(game, member, game.alive_players(), timeout=duration)
            em = discord.Embed(
                title="💞 L'Enmusubi tisse le fil rouge",
                description=(
                    "Choisis **deux joueurs** à lier d'amour pour l'éternité.\n"
                    "Tu peux te choisir parmi eux. Si l'un meurt, l'autre suit."
                ),
                color=0xe91e63,
            )

        elif role_key == "mamori":
            valid = list(others_alive) + [member]
            valid = [m for m in valid if m.id in game.alive]
            if game.last_mamori_target is not None:
                valid = [m for m in valid if m.id != game.last_mamori_target]
            if not valid:
                return
            view = SimpleActionView(game, member, role_key, valid, timeout=duration)
            em = discord.Embed(
                title="🛡️ Le Mamori veille",
                description=(
                    f"Nuit {game.night_number}. Tu protèges un foyer contre les loups.\n\n"
                    f"⚠️ Tu ne peux pas protéger la même personne deux nuits d'affilée.\n"
                    f"*(tu peux te protéger toi-même)*"
                ),
                color=0x3498db,
            )

        elif role_key == "shojo":
            view = ShojoActionView(game, member, timeout=duration)
            em = discord.Embed(
                title="👧 La porte entrouverte",
                description=(
                    f"Nuit {game.night_number}. Tu peux entrouvrir la porte pour épier la meute.\n\n"
                    f"⚠️ **35% de chance d'être repérée** — auquel cas tu meurs cette nuit."
                ),
                color=0x9b59b6,
            )
        else:
            return

        em.set_footer(text=f"{FOOTER_TEXT} ・ Partie #{game.game_id[-8:]}")
        try:
            await member.send(embed=em, view=view)
        except discord.Forbidden:
            log.info(f"DM fermé pour {member} ({role_key})")
            try:
                await game.channel.send(embed=error_embed(
                    "⚠️ DM fermé",
                    f"Un joueur a ses DMs fermés. Action perdue cette nuit."
                ))
            except discord.HTTPException:
                pass
        except discord.HTTPException as e:
            log.warning(f"Échec DM action à {member}: {e}")

    @staticmethod
    async def send_witch_dm(game, witch_member, duration):
        """Envoie en DM à la sorcière les infos sur la victime des loups + sa vue d'action."""
        target_id = game.current_wolf_target
        target_m = game.get_member(target_id) if target_id else None

        if target_m:
            victim_line = (
                f"🩸 **Cette nuit, les loups veulent dévorer : {target_m.display_name}**\n\n"
            )
        else:
            victim_line = "*Les loups n'ont désigné personne cette nuit.*\n\n"

        em = discord.Embed(
            title="🧪 La Majo se réveille",
            description=(
                f"Nuit {game.night_number}.\n\n"
                + victim_line +
                f"Tes potions :\n"
                f"🍵 Potion de vie : {'✅ disponible' if not game.majo_heal_used else '❌ utilisée'}\n"
                f"☠️ Potion de mort : {'✅ disponible' if not game.majo_poison_used else '❌ utilisée'}\n\n"
                f"⏳ **{duration} secondes** pour choisir."
            ),
            color=0x9b59b6,
        ).set_footer(text=f"{FOOTER_TEXT} ・ Partie #{game.game_id[-8:]}")
        view = MajoActionView(game, witch_member, timeout=duration)
        try:
            await witch_member.send(embed=em, view=view)
        except discord.Forbidden:
            log.info(f"DM fermé pour la sorcière {witch_member}")
        except discord.HTTPException as e:
            log.warning(f"Échec DM sorcière {witch_member}: {e}")

    @staticmethod
    async def resolve_night(game: Game):
        """Résolution finale : combine cible loups, salvateur, infection, sorcière, petite fille."""
        deaths_tonight = set()
        infected_tonight = None

        # 1. Miko : envoi de l'indice narratif (70/30)
        for uid, act in game.night_actions.items():
            if act.get("action") == "miko_inspect":
                target_id = act["target"]
                target_role = ROLES[game.roles_assignment[target_id]]
                inspector = game.get_member(uid)
                target_m = game.get_member(target_id)
                if inspector and target_m:
                    hint = get_seer_hint(target_role["camp"])
                    try:
                        em = discord.Embed(
                            title="🔮 Vision de la Miko",
                            description=(
                                f"Tu sondes **{target_m.display_name}**...\n\n"
                                f"> {hint}\n\n"
                                f"*À toi de juger ce que cela signifie.*"
                            ),
                            color=0x3498db,
                        )
                        em.set_footer(text=FOOTER_TEXT)
                        await inspector.send(embed=em)
                    except discord.HTTPException:
                        pass

        # 2. Petite Fille : repérage 35%
        shojo_spotted = False
        shojo_id = None
        for uid, act in game.night_actions.items():
            if act.get("action") == "shojo_peek":
                shojo_id = uid
                if random.random() < 0.35:
                    shojo_spotted = True
                else:
                    shojo_member = game.get_member(uid)
                    if shojo_member:
                        wolf_names = [game.get_member(w.id).display_name for w in game.alive_wolves()]
                        try:
                            em = discord.Embed(
                                title="👧 Tu as épié la meute...",
                                description=(
                                    f"**Loups identifiés cette nuit :**\n"
                                    + "\n".join(f"• 🐺 {n}" for n in wolf_names)
                                ),
                                color=0x9b59b6,
                            )
                            em.set_footer(text=FOOTER_TEXT)
                            await shojo_member.send(embed=em)
                        except discord.HTTPException:
                            pass

        # 3. Cible des loups (déjà calculée dans game.current_wolf_target)
        wolf_target_id = game.current_wolf_target

        # 4. Salvateur
        protected_id = None
        for uid, act in game.night_actions.items():
            if act.get("action") == "mamori_protect":
                protected_id = act["target"]
                game.last_mamori_target = protected_id
                break

        # 5. Oyaōkami infect
        oyaokami_infect_target = None
        for uid, act in game.night_actions.items():
            if act.get("action") == "oyaokami_infect" and not game.oyaokami_infect_used:
                oyaokami_infect_target = act["target"]

        if oyaokami_infect_target is not None and oyaokami_infect_target == wolf_target_id:
            infected_tonight = oyaokami_infect_target
            game.oyaokami_infect_used = True
            wolf_target_id = None
        elif wolf_target_id is not None and wolf_target_id == protected_id:
            wolf_target_id = None  # Mamori a bloqué

        if wolf_target_id is not None:
            deaths_tonight.add(wolf_target_id)

        # 6. Sorcière : potions (target=-1 pour heal signifie la victime des loups)
        majo_heal = False
        majo_poison_target = None
        for uid, act in game.night_actions.items():
            if act.get("action") == "majo_heal" and not game.majo_heal_used:
                majo_heal = True
                game.majo_heal_used = True
            elif act.get("action") == "majo_poison" and not game.majo_poison_used:
                majo_poison_target = act["target"]
                game.majo_poison_used = True

        # Heal annule la mort de la victime des loups
        if majo_heal and game.current_wolf_target is not None:
            deaths_tonight.discard(game.current_wolf_target)
        if majo_poison_target is not None:
            deaths_tonight.add(majo_poison_target)

        # 7. Petite Fille repérée → meurt
        if shojo_spotted and shojo_id is not None:
            deaths_tonight.add(shojo_id)

        # 8. Application des morts (chaînes amoureux/chasseur/capitaine)
        await GameManager.apply_deaths(game, deaths_tonight, cause="night")

        # 9. Infection (devient loup, rejoint le salon des loups)
        if infected_tonight is not None and infected_tonight in game.alive:
            game.roles_assignment[infected_tonight] = "okami"
            infected_member = game.get_member(infected_tonight)
            if infected_member:
                try:
                    em = discord.Embed(
                        title="🩸 Tu as été infecté",
                        description=(
                            "L'Oyaōkami t'a transmis sa malédiction. "
                            "Tu es maintenant un **Ōkami (Loup-Garou)**.\n\n"
                            "Ton ancien rôle est perdu. Tu rejoins la meute."
                        ),
                        color=0xe74c3c,
                    ).set_footer(text=FOOTER_TEXT)
                    await infected_member.send(embed=em)
                except discord.HTTPException:
                    pass
                await add_to_wolf_channel(game, infected_member)

        # 10. Annonce du matin
        await GameManager.announce_morning(game)

    @staticmethod
    async def apply_deaths(game: Game, death_ids, cause: str):
        """Applique les morts en chaîne : amoureux + chasseur + capitaine.
        Ajoute le suffixe '(MORT)' au pseudo + mute des morts."""
        to_process = list(death_ids)
        processed = set()

        while to_process:
            uid = to_process.pop(0)
            if uid in processed or uid not in game.alive:
                continue
            processed.add(uid)

            if not game.add_death(uid, cause):
                continue

            # Marque MORT (pseudo + mute)
            member = game.get_member(uid)
            if member:
                # Rename si pas déjà fait
                if uid not in game.original_nicks:
                    original = await mark_dead_nickname(member)
                    game.original_nicks[uid] = original
                # Mute
                await mute_dead_player(game, member)
                game.muted_players.add(uid)

            # Chaîne amoureux
            if uid in game.lovers:
                for partner_id in game.lovers:
                    if partner_id != uid and partner_id in game.alive and partner_id not in processed:
                        to_process.append(partner_id)
                        partner = game.get_member(partner_id)
                        if partner:
                            try:
                                await game.channel.send(embed=discord.Embed(
                                    title="💔 Chagrin d'amour",
                                    description=f"**{partner.display_name}**, le cœur brisé, suit son aimé(e) dans la mort.",
                                    color=0xe91e63,
                                ).set_footer(text=FOOTER_TEXT))
                            except discord.HTTPException:
                                pass

            # Chasseur : tir
            if game.role_key(uid) == "karyudo":
                hunter_target = await GameManager.ask_hunter_target(game, uid)
                if hunter_target is not None and hunter_target in game.alive:
                    increment_player_stat(uid, "kills_as_hunter", 1)
                    to_process.append(hunter_target)
                    target_m = game.get_member(hunter_target)
                    if target_m:
                        try:
                            await game.channel.send(embed=discord.Embed(
                                title="🏹 Le Karyūdo tire",
                                description=f"En tombant, le chasseur emporte **{target_m.display_name}** avec lui.",
                                color=0xe67e22,
                            ).set_footer(text=FOOTER_TEXT))
                        except discord.HTTPException:
                            pass

            # Capitaine mort → désigne successeur
            if uid == game.captain_id:
                new_captain = await GameManager.ask_new_captain(game, uid)
                if new_captain is not None and new_captain in game.alive:
                    game.captain_id = new_captain
                    increment_player_stat(new_captain, "times_captain", 1)
                    new_m = game.get_member(new_captain)
                    if new_m:
                        try:
                            await game.channel.send(embed=discord.Embed(
                                title="👑 Transmission du titre",
                                description=f"Avant de tomber, le Sonchō désigne **{new_m.display_name}** comme son successeur.",
                                color=0xf1c40f,
                            ).set_footer(text=FOOTER_TEXT))
                        except discord.HTTPException:
                            pass
                else:
                    game.captain_id = None

    @staticmethod
    async def ask_hunter_target(game: Game, hunter_id):
        hunter = game.get_member(hunter_id)
        if not hunter:
            return None
        candidates = [m for m in game.alive_players() if m.id != hunter_id]
        if not candidates:
            return None
        dur = get_timing("hunter")
        view = SimpleActionView(game, hunter, "karyudo_shoot", candidates, timeout=dur)
        try:
            em = discord.Embed(
                title="🏹 Dernier tir du Karyūdo",
                description=(
                    "Tu meurs, mais tu peux emporter quelqu'un avec toi.\n"
                    f"⏳ {dur} secondes pour choisir. Sans réponse, ton pouvoir est perdu."
                ),
                color=0xe67e22,
            ).set_footer(text=FOOTER_TEXT)
            await hunter.send(embed=em, view=view)
        except discord.HTTPException:
            return None
        await view.wait()
        return game.night_actions.get(hunter_id, {}).get("target")

    @staticmethod
    async def ask_new_captain(game: Game, dying_captain_id):
        captain = game.get_member(dying_captain_id)
        if not captain:
            return None
        candidates = [m for m in game.alive_players() if m.id != dying_captain_id]
        if not candidates:
            return None
        dur = get_timing("successor")
        view = SimpleActionView(game, captain, "captain_successor", candidates, timeout=dur)
        try:
            em = discord.Embed(
                title="👑 Désigne ton successeur",
                description=(
                    f"Tu es le Sonchō et tu vas mourir. Choisis ton successeur.\n"
                    f"⏳ {dur} secondes. Sans réponse, le titre est perdu."
                ),
                color=0xf1c40f,
            ).set_footer(text=FOOTER_TEXT)
            await captain.send(embed=em, view=view)
        except discord.HTTPException:
            return None
        await view.wait()
        return game.night_actions.get(dying_captain_id, {}).get("target")

    @staticmethod
    async def announce_morning(game: Game):
        last_night_deaths = [d for d in game.dead if d["night"] == game.night_number and d["cause"] == "night"]
        if not last_night_deaths:
            em = discord.Embed(
                title=f"☀️ Aube du jour {game.night_number}",
                description="*Cette nuit, personne n'a péri. Le village s'éveille, méfiant.*",
                color=0xf1c40f,
            )
        else:
            lines = []
            for d in last_night_deaths:
                m = game.get_member(d["user_id"])
                role = ROLES.get(game.roles_assignment.get(d["user_id"]), {})
                if m:
                    display = (m.nick or m.name).replace(" (MORT)", "").replace("MORT", "").strip() or m.name
                    lines.append(f"💀 **{display}** — *{role.get('name_fr', '?')}* ({role.get('emoji', '')})")
            em = discord.Embed(
                title=f"☀️ Aube du jour {game.night_number}",
                description="**Cette nuit, le village a perdu :**\n\n" + "\n".join(lines),
                color=0xe67e22,
            )
        em.set_footer(text=FOOTER_TEXT)
        await game.channel.send(embed=em)

    # ─────────────────── PHASE JOUR + VOTE ────────────────────

    @staticmethod
    async def run_day(game: Game):
        """Débat + vote du village (timings dynamiques)."""
        debate_dur = get_timing("debate")
        vote_dur = get_timing("vote_day")

        end_ts = int((datetime.now(PARIS_TZ) + timedelta(seconds=debate_dur)).timestamp())
        em = discord.Embed(
            title=f"🗣️ Débat — Jour {game.night_number}",
            description=(
                f"Le village se rassemble pour discuter et voter.\n\n"
                f"💬 **{debate_dur} secondes** de débat.\n"
                f"⏰ Vote dans : <t:{end_ts}:R>"
            ),
            color=0xe67e22,
        )
        em.set_footer(text=FOOTER_TEXT)
        await game.channel.send(embed=em)
        await asyncio.sleep(debate_dur // 2)
        try:
            await game.channel.send(embed=info_embed("⏳ Plus que la moitié", "Préparez vos accusations..."))
        except discord.HTTPException:
            pass
        await asyncio.sleep(debate_dur - (debate_dur // 2))

        # Vote
        em = discord.Embed(
            title=f"🗳️ Vote du Jour {game.night_number}",
            description=(
                "Désigne qui doit être éliminé. Le vote est **anonyme**.\n"
                "👑 Le Sonchō compte pour **2 voix**.\n\n"
                f"⏳ **{vote_dur} secondes**."
            ),
            color=0xe91e63,
        )
        em.set_footer(text=FOOTER_TEXT)
        view = DayVoteView(game, timeout=vote_dur)
        msg = await game.channel.send(embed=em, view=view)
        view.message = msg
        await view.wait()

        # Comptage avec capitaine x2
        tally = {}
        for voter_id, target_id in view.votes.items():
            weight = 2 if voter_id == game.captain_id else 1
            tally[target_id] = tally.get(target_id, 0) + weight

        if not tally:
            await game.channel.send(embed=info_embed(
                "😶 Aucun vote",
                "Le village est resté silencieux. Personne n'est éliminé."
            ))
            return

        max_v = max(tally.values())
        top = [uid for uid, v in tally.items() if v == max_v]

        # En cas d'égalité, le capitaine tranche (si en vie et n'est pas dans top)
        # Pour simplifier : random parmi top
        accused_id = random.choice(top)
        accused = game.get_member(accused_id)
        accused_role = ROLES[game.roles_assignment[accused_id]]

        # Cas spécial Tenshi au jour 1 : il gagne tout de suite
        if game.night_number == 1 and accused_role["camp"] == "solo" and game.roles_assignment[accused_id] == "tenshi":
            await game.channel.send(embed=discord.Embed(
                title="😇 Le Tenshi triomphe",
                description=(
                    f"**{accused.display_name}** était l'**Ange déchu** !\n\n"
                    f"Il a réussi sa mission : se faire éliminer au premier vote.\n"
                    f"Il gagne **seul** la partie."
                ),
                color=0xf1c40f,
            ).set_footer(text=FOOTER_TEXT))
            game.winning_camp = "tenshi"
            game.winners = {accused_id}
            # On le tue quand même pour la cohérence
            await GameManager.apply_deaths(game, {accused_id}, cause="vote")
            return

        # Tracking : qui a voté juste ?
        for voter_id, target_id in view.votes.items():
            target_role = ROLES[game.roles_assignment[target_id]]
            if target_role["camp"] == "wolf":
                increment_player_stat(voter_id, "correct_votes", 1)
            else:
                increment_player_stat(voter_id, "wrong_votes", 1)

        # Annonce de l'éliminé + reveal rôle
        await game.channel.send(embed=discord.Embed(
            title="⚖️ Verdict du village",
            description=(
                f"Avec {tally[accused_id]} voix, **{accused.display_name}** est condamné.\n\n"
                f"Il était : **{accused_role['name']}** ({accused_role['name_fr']}) {accused_role['emoji']}\n"
                f"Camp : {GameManager.camp_display(accused_role['camp'])}"
            ),
            color=0xe91e63,
        ).set_footer(text=FOOTER_TEXT))

        await GameManager.apply_deaths(game, {accused_id}, cause="vote")

        # Tenshi qui rate le jour 1 devient un villageois
        if game.night_number == 1:
            for uid, rkey in list(game.roles_assignment.items()):
                if rkey == "tenshi" and uid in game.alive:
                    game.roles_assignment[uid] = "murabito"
                    member = game.get_member(uid)
                    if member:
                        try:
                            await member.send(embed=discord.Embed(
                                title="😔 Tu as échoué",
                                description=(
                                    "Le premier jour est passé et tu es toujours vivant.\n"
                                    "Tu perds tes pouvoirs et deviens un simple **Murabito (Villageois)**."
                                ),
                                color=0x95a5a6,
                            ).set_footer(text=FOOTER_TEXT))
                        except discord.HTTPException:
                            pass

    # ─────────────────── CONDITIONS DE VICTOIRE ────────────────────

    @staticmethod
    def check_win_conditions(game: Game):
        """Retourne True si une condition de victoire est atteinte (game.winning_camp set)."""
        if game.winning_camp is not None:
            return True

        wolves = game.alive_wolves()
        non_wolves = game.alive_non_wolves()

        # Amoureux seuls vivants ?
        if game.lovers and len(game.alive) == 2 and set(game.alive) == game.lovers:
            # Vérifie qu'ils sont de camps différents (sinon c'est juste leur camp qui gagne)
            lover_ids = list(game.lovers)
            r1 = ROLES[game.roles_assignment[lover_ids[0]]]
            r2 = ROLES[game.roles_assignment[lover_ids[1]]]
            if r1["camp"] != r2["camp"]:
                game.winning_camp = "lovers"
                game.winners = set(game.lovers)
                return True

        # Loups gagnent
        if len(wolves) >= len(non_wolves) and len(wolves) > 0:
            game.winning_camp = "wolf"
            game.winners = {m.id for m in wolves}
            # Amoureux loup + village : si l'amoureux non-loup est encore en vie, il perd avec son camp
            return True

        # Village gagne
        if len(wolves) == 0:
            game.winning_camp = "village"
            game.winners = {m.id for m in game.alive_players()
                            if ROLES[game.roles_assignment[m.id]]["camp"] == "village"}
            return True

        return False

    # ─────────────────── PHASE RÉSOLUTION ────────────────────

    @staticmethod
    async def run_resolution(game: Game):
        game.phase = "RESOLUTION"
        game.ended_at = datetime.now(PARIS_TZ)

        # Récap des rôles
        role_lines = []
        for m in game.participants:
            rkey = game.roles_assignment.get(m.id)
            if not rkey:
                continue
            r = ROLES[rkey]
            status = "💀 mort" if m.id not in game.alive else "❤️ vivant"
            crown = " 👑" if m.id == game.captain_id else ""
            heart = " 💞" if m.id in game.lovers else ""
            winner = " ⭐" if m.id in game.winners else ""
            role_lines.append(f"{r['emoji']} **{m.display_name}** — {r['name_fr']} ({status}){crown}{heart}{winner}")

        camp_titles = {
            "wolf": ("🐺 Les loups l'emportent", "Les Ōkami régnent sur Shoen.", 0xe74c3c),
            "village": ("🏯 Le village est sauvé", "La meute a été décimée. Shoen retrouve la paix.", 0x43b581),
            "lovers": ("💞 Les amoureux triomphent", "Liés par le fil rouge, ils ont survécu à tout.", 0xe91e63),
            "tenshi": ("😇 Le Tenshi gagne seul", "L'Ange déchu a trouvé sa rédemption.", 0xf1c40f),
            None: ("🤷 Match nul", "Aucune condition de victoire atteinte.", 0x95a5a6),
        }
        title, desc, color = camp_titles.get(game.winning_camp, camp_titles[None])

        em = discord.Embed(title=title, description=desc, color=color)
        em.add_field(name="🎴 Tous les rôles", value="\n".join(role_lines) or "—", inline=False)
        em.set_footer(text=FOOTER_TEXT)
        await game.channel.send(embed=em)

        # XP, stats, badges
        await GameManager.distribute_rewards(game)

        # Sauvegarde historique
        try:
            participants_data = [
                {"id": str(m.id), "name": m.display_name,
                 "role": game.roles_assignment.get(m.id),
                 "alive": m.id in game.alive,
                 "won": m.id in game.winners}
                for m in game.participants
            ]
            save_game_history(
                game.game_id, game.guild.id, game.channel.id, game.host.id,
                game.size_mode, len(game.participants),
                game.winning_camp or "draw", game.night_number,
                game.started_at.isoformat(), game.ended_at.isoformat(),
                participants_data,
            )
        except Exception as e:
            log.error(f"Échec sauvegarde historique {game.game_id}: {e}")

        game.phase = "ENDED"

    @staticmethod
    async def distribute_rewards(game: Game):
        level_up_messages = []
        all_new_badges = {}

        for m in game.participants:
            uid = m.id
            rkey = game.roles_assignment.get(uid)
            if not rkey:
                continue
            increment_player_stat(uid, "games_played", 1)

            won = uid in game.winners
            xp_gain = 30  # base participation

            if won:
                increment_player_stat(uid, "games_won", 1)
                xp_gain = 90
                if rkey in ("okami", "oyaokami"):
                    increment_player_stat(uid, "wolf_wins", 1)
                    xp_gain = 110

            update_player_stats(uid, last_played=datetime.now(PARIS_TZ).isoformat())

            new_level, leveled_up, new_class = await award_xp(uid, xp_gain)
            if leveled_up:
                level_up_messages.append(
                    f"📈 {m.mention} passe niveau **{new_level}**"
                    + (f" ・ **{new_class}**" if new_class else "")
                )
            new_badges = check_and_award_badges(uid)
            if new_badges:
                all_new_badges[uid] = new_badges

        if level_up_messages:
            try:
                await game.channel.send(embed=discord.Embed(
                    title="📈 Progression",
                    description="\n".join(level_up_messages),
                    color=0xffd700,
                ).set_footer(text=FOOTER_TEXT))
            except discord.HTTPException:
                pass

        if all_new_badges:
            lines = []
            for uid, badges_list in all_new_badges.items():
                member = game.get_member(uid)
                if not member:
                    continue
                for bkey in badges_list:
                    b = BADGES[bkey]
                    lines.append(f"{b['emoji']} {member.mention} débloque **{b['name']}**")
            if lines:
                try:
                    await game.channel.send(embed=discord.Embed(
                        title="🏅 Badges débloqués",
                        description="\n".join(lines),
                        color=0xf1c40f,
                    ).set_footer(text=FOOTER_TEXT))
                except discord.HTTPException:
                    pass


# ========================= VIEWS =========================

class RecruitingView(discord.ui.View):
    def __init__(self, game: Game, timeout=None):
        if timeout is None:
            timeout = get_timing("recruit")
        super().__init__(timeout=timeout)
        self.game = game
        self.message = None
        self.cancelled = False

    async def refresh(self):
        if self.message:
            try:
                await self.message.edit(
                    embed=GameManager.build_recruiting_embed(self.game),
                    view=self,
                )
            except discord.HTTPException:
                pass

    async def on_timeout(self):
        # Si timeout : on annule si pas assez de joueurs, sinon on accepte tacitement
        if len(self.game.participants) < MIN_PLAYERS:
            self.cancelled = True
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Rejoindre 🎯", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.bot:
            return await interaction.response.send_message("Les bots ne peuvent pas jouer.", ephemeral=True)
        if is_bot_banned(interaction.user.id):
            return await interaction.response.send_message("Tu es banni du bot Jinrō.", ephemeral=True)
        if interaction.user in self.game.participants:
            return await interaction.response.send_message("Tu es déjà inscrit.", ephemeral=True)
        if len(self.game.participants) >= MAX_PLAYERS:
            return await interaction.response.send_message(f"Partie complète ({MAX_PLAYERS} joueurs max).", ephemeral=True)
        self.game.participants.append(interaction.user)
        await interaction.response.send_message(
            f"✅ Tu as rejoint la partie ! ({len(self.game.participants)} joueurs)",
            ephemeral=True,
        )
        await self.refresh()

    @discord.ui.button(label="Partir 🚪", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == self.game.host.id:
            return await interaction.response.send_message(
                "Tu es l'hôte. Utilise **Annuler** pour stopper la partie.",
                ephemeral=True,
            )
        if interaction.user not in self.game.participants:
            return await interaction.response.send_message("Tu n'es pas inscrit.", ephemeral=True)
        self.game.participants.remove(interaction.user)
        await interaction.response.send_message("👋 Tu as quitté la partie.", ephemeral=True)
        await self.refresh()

    @discord.ui.button(label="Lancer ▶️", style=discord.ButtonStyle.primary)
    async def launch(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.game.host.id:
            return await interaction.response.send_message("Seul l'hôte peut lancer.", ephemeral=True)
        if len(self.game.participants) < MIN_PLAYERS:
            return await interaction.response.send_message(
                f"Il faut au moins **{MIN_PLAYERS} joueurs**.",
                ephemeral=True,
            )
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Annuler ❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.game.host.id and not has_min_rank(interaction.user.id, 3):
            return await interaction.response.send_message(
                "Seul l'hôte ou un Sys peut annuler.",
                ephemeral=True,
            )
        self.cancelled = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


# ─── Action Views ───

class SimpleActionView(discord.ui.View):
    """Sélecteur générique : pick 1 cible parmi candidates, stocké dans game.night_actions[actor.id]."""
    def __init__(self, game: Game, actor: discord.Member, action_key: str, candidates, timeout=None):
        if timeout is None:
            timeout = get_timing("night")
        super().__init__(timeout=timeout)
        self.game = game
        self.actor = actor
        self.action_key = action_key
        options = [
            discord.SelectOption(label=m.display_name[:80], value=str(m.id))
            for m in candidates[:25]
        ]
        self.add_item(SimpleActionSelect(self, options))


class SimpleActionSelect(discord.ui.Select):
    def __init__(self, parent_view: SimpleActionView, options):
        super().__init__(placeholder="Choisis une cible...", min_values=1, max_values=1, options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        v = self.parent_view
        if interaction.user.id != v.actor.id:
            return await interaction.response.send_message("Ce n'est pas ton action.", ephemeral=True)
        target_id = int(self.values[0])
        action_label = {
            "miko": "miko_inspect",
            "mamori": "mamori_protect",
            "karyudo_shoot": "karyudo_shoot",
            "captain_successor": "captain_successor",
        }.get(v.action_key, v.action_key)
        v.game.night_actions[v.actor.id] = {"action": action_label, "target": target_id}
        target_m = v.game.get_member(target_id)
        target_name = target_m.display_name if target_m else "?"
        for item in v.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"✅ Action enregistrée : **{target_name}**", view=v
        )
        v.stop()


class WolfVoteView(discord.ui.View):
    """Vue partagée dans le salon des loups : tous les loups votent ensemble en temps réel.
    Met à jour l'embed pour afficher les votes au fur et à mesure.
    L'Oyaōkami a un bouton supplémentaire pour basculer en mode infection."""
    def __init__(self, game: Game, candidates, timeout):
        super().__init__(timeout=timeout)
        self.game = game
        self.candidates = candidates
        self.message = None
        self.infect_mode = False  # global pour l'Oyaōkami
        options = [
            discord.SelectOption(label=m.display_name[:80], value=str(m.id))
            for m in candidates[:25]
        ]
        self.add_item(_WolfVoteSelect(self, options))

        # Bouton infect pour l'Oyaōkami si pas encore utilisé
        if any(game.roles_assignment.get(w.id) == "oyaokami" for w in game.alive_wolves()) and not game.oyaokami_infect_used:
            self.add_item(_WolfInfectToggle(self))

    async def update_message(self):
        """Met à jour le message avec l'état actuel des votes."""
        if not self.message:
            return
        tally = {}
        for tgt in self.game.wolf_votes.values():
            tally[tgt] = tally.get(tgt, 0) + 1

        if tally:
            sorted_tally = sorted(tally.items(), key=lambda x: -x[1])
            lines = []
            for tgt_id, count in sorted_tally:
                m = self.game.get_member(tgt_id)
                if m:
                    lines.append(f"• **{m.display_name}** — {count} voix")
            tally_text = "\n".join(lines)
        else:
            tally_text = "*Aucun vote pour l'instant.*"

        em = discord.Embed(
            title=f"🐺 Vote de la meute — Nuit {self.game.night_number}",
            description=(
                "Choisissez votre victime. **La majorité l'emporte.**\n\n"
                f"**Votes actuels :**\n{tally_text}\n\n"
                f"*Loups :* {', '.join(w.mention for w in self.game.alive_wolves())}"
                + (f"\n\n🩸 **Mode infection : ON** (l'Oyaōkami transformera la cible au lieu de tuer)"
                   if self.infect_mode else "")
            ),
            color=0xe74c3c,
        ).set_footer(text=FOOTER_TEXT)
        try:
            await self.message.edit(embed=em, view=self)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class _WolfVoteSelect(discord.ui.Select):
    def __init__(self, parent_view: WolfVoteView, options):
        super().__init__(placeholder="Vote pour la victime...", min_values=1, max_values=1, options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        v = self.parent_view
        if not v.game.is_wolf(interaction.user.id):
            return await interaction.response.send_message("Seuls les loups peuvent voter.", ephemeral=True)
        if interaction.user.id not in v.game.alive:
            return await interaction.response.send_message("Tu es mort, tu ne peux plus voter.", ephemeral=True)
        target_id = int(self.values[0])
        v.game.wolf_votes[interaction.user.id] = target_id
        # Si l'Oyaōkami a activé le mode infect et c'est lui qui vote, on enregistre l'intention
        if v.infect_mode and v.game.roles_assignment.get(interaction.user.id) == "oyaokami" and not v.game.oyaokami_infect_used:
            v.game.night_actions[interaction.user.id] = {"action": "oyaokami_infect", "target": target_id}
        target_m = v.game.get_member(target_id)
        await interaction.response.send_message(
            f"✅ Vote enregistré : **{target_m.display_name if target_m else '?'}**",
            ephemeral=True,
        )
        await v.update_message()


class _WolfInfectToggle(discord.ui.Button):
    def __init__(self, parent_view: WolfVoteView):
        super().__init__(label="🩸 Mode infection : OFF", style=discord.ButtonStyle.secondary)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        v = self.parent_view
        if v.game.roles_assignment.get(interaction.user.id) != "oyaokami":
            return await interaction.response.send_message(
                "Seul l'Oyaōkami peut activer ce mode.", ephemeral=True
            )
        if v.game.oyaokami_infect_used:
            return await interaction.response.send_message(
                "Tu as déjà utilisé ton infection.", ephemeral=True
            )
        v.infect_mode = not v.infect_mode
        self.label = "🩸 Mode infection : ON" if v.infect_mode else "🩸 Mode infection : OFF"
        self.style = discord.ButtonStyle.danger if v.infect_mode else discord.ButtonStyle.secondary
        # Si on désactive et qu'il y avait une intention, on l'enlève
        if not v.infect_mode and interaction.user.id in v.game.night_actions:
            if v.game.night_actions[interaction.user.id].get("action") == "oyaokami_infect":
                v.game.night_actions.pop(interaction.user.id, None)
        await interaction.response.send_message(
            f"🩸 Mode infection {'**activé**' if v.infect_mode else '**désactivé**'}.",
            ephemeral=True,
        )
        await v.update_message()


class MajoActionView(discord.ui.View):
    """Sorcière : 3 choix : utiliser potion vie, potion mort, ou rien."""
    def __init__(self, game: Game, actor: discord.Member, timeout=None):
        if timeout is None:
            timeout = get_timing("witch")
        super().__init__(timeout=timeout)
        self.game = game
        self.actor = actor
        # Ajoute les boutons selon les potions restantes
        if not game.majo_heal_used:
            self.add_item(MajoHealButton(self))
        if not game.majo_poison_used:
            self.add_item(MajoPoisonButton(self))
        self.add_item(MajoPassButton(self))


class MajoHealButton(discord.ui.Button):
    def __init__(self, parent_view: MajoActionView):
        super().__init__(label="🍵 Sauver la victime des loups", style=discord.ButtonStyle.success)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        v = self.parent_view
        if interaction.user.id != v.actor.id:
            return await interaction.response.send_message("Pas ton action.", ephemeral=True)
        # On enregistre l'intention : la cible sera la victime des loups
        # On résoudra ça dans resolve_night : si la victime des loups = la cible "heal", on annule
        # Pour simplifier : on stocke "majo_heal" sans cible précise, la résolution comprend
        # Mais resolve_night attend une cible précise. Donc on attend la résolution des loups.
        # Trick : on stocke {"action": "majo_heal", "target": -1} → résolu côté serveur en regardant wolf_target
        v.game.night_actions[v.actor.id] = {"action": "majo_heal", "target": -1}
        for item in v.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="🍵 Tu utilises ta **potion de vie** sur la victime des loups cette nuit.",
            view=v,
        )
        v.stop()


class MajoPoisonButton(discord.ui.Button):
    def __init__(self, parent_view: MajoActionView):
        super().__init__(label="☠️ Empoisonner quelqu'un", style=discord.ButtonStyle.danger)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        v = self.parent_view
        if interaction.user.id != v.actor.id:
            return await interaction.response.send_message("Pas ton action.", ephemeral=True)
        # Affiche un select pour choisir la cible
        candidates = [m for m in v.game.alive_players() if m.id != v.actor.id]
        new_view = MajoPoisonTargetView(v.game, v.actor, candidates)
        await interaction.response.edit_message(
            content="☠️ Choisis ta cible pour la potion de mort :",
            view=new_view,
        )
        v.stop()


class MajoPassButton(discord.ui.Button):
    def __init__(self, parent_view: MajoActionView):
        super().__init__(label="💤 Passer la nuit", style=discord.ButtonStyle.secondary)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        v = self.parent_view
        if interaction.user.id != v.actor.id:
            return await interaction.response.send_message("Pas ton action.", ephemeral=True)
        for item in v.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="💤 Tu ne fais rien cette nuit.",
            view=v,
        )
        v.stop()


class MajoPoisonTargetView(discord.ui.View):
    def __init__(self, game: Game, actor: discord.Member, candidates, timeout=None):
        if timeout is None:
            timeout = get_timing("witch")
        super().__init__(timeout=timeout)
        self.game = game
        self.actor = actor
        options = [
            discord.SelectOption(label=m.display_name[:80], value=str(m.id))
            for m in candidates[:25]
        ]
        self.add_item(MajoPoisonSelect(self, options))


class MajoPoisonSelect(discord.ui.Select):
    def __init__(self, parent_view: MajoPoisonTargetView, options):
        super().__init__(placeholder="Cible de la potion de mort...", min_values=1, max_values=1, options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        v = self.parent_view
        if interaction.user.id != v.actor.id:
            return await interaction.response.send_message("Pas ton action.", ephemeral=True)
        target_id = int(self.values[0])
        v.game.night_actions[v.actor.id] = {"action": "majo_poison", "target": target_id}
        target_m = v.game.get_member(target_id)
        target_name = target_m.display_name if target_m else "?"
        for item in v.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"☠️ **{target_name}** sera empoisonné cette nuit.",
            view=v,
        )
        v.stop()


class EnmusubiActionView(discord.ui.View):
    """Cupidon : choisit 2 amoureux."""
    def __init__(self, game: Game, actor: discord.Member, candidates, timeout=None):
        if timeout is None:
            timeout = get_timing("night")
        super().__init__(timeout=timeout)
        self.game = game
        self.actor = actor
        options = [
            discord.SelectOption(label=m.display_name[:80], value=str(m.id))
            for m in candidates[:25]
        ]
        self.add_item(EnmusubiSelect(self, options))


class EnmusubiSelect(discord.ui.Select):
    def __init__(self, parent_view: EnmusubiActionView, options):
        super().__init__(placeholder="Choisis 2 amoureux...", min_values=2, max_values=2, options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        v = self.parent_view
        if interaction.user.id != v.actor.id:
            return await interaction.response.send_message("Pas ton action.", ephemeral=True)
        ids = [int(x) for x in self.values]
        if len(set(ids)) != 2:
            return await interaction.response.send_message("Choisis 2 personnes différentes.", ephemeral=True)
        v.game.lovers = set(ids)
        # Envoie un DM aux deux amoureux
        for lover_id in ids:
            lover = v.game.get_member(lover_id)
            other_id = [i for i in ids if i != lover_id][0]
            other_m = v.game.get_member(other_id)
            if lover and other_m:
                try:
                    other_role = ROLES[v.game.roles_assignment[other_id]]
                    em = discord.Embed(
                        title="💞 Tu es amoureux",
                        description=(
                            f"Le fil rouge de l'Enmusubi vous a liés.\n\n"
                            f"💞 Ton amoureux(se) est : **{other_m.display_name}** "
                            f"(*{other_role['name_fr']}*)\n\n"
                            f"Si l'un de vous meurt, l'autre suit. Si vous êtes de camps différents, "
                            f"vous gagnez **seuls** en étant les deux derniers en vie."
                        ),
                        color=0xe91e63,
                    )
                    em.set_footer(text=FOOTER_TEXT)
                    await lover.send(embed=em)
                except discord.HTTPException:
                    pass
        for item in v.children:
            item.disabled = True
        names = [v.game.get_member(i).display_name for i in ids if v.game.get_member(i)]
        await interaction.response.edit_message(
            content=f"💞 Tu as lié **{names[0]}** et **{names[1]}** par le fil rouge.",
            view=v,
        )
        v.stop()


class ShojoActionView(discord.ui.View):
    """Petite Fille : 2 boutons : espionner ou passer."""
    def __init__(self, game: Game, actor: discord.Member, timeout=None):
        if timeout is None:
            timeout = get_timing("night")
        super().__init__(timeout=timeout)
        self.game = game
        self.actor = actor

    @discord.ui.button(label="👁️ Espionner les loups (risqué)", style=discord.ButtonStyle.danger)
    async def peek(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.actor.id:
            return await interaction.response.send_message("Pas ton action.", ephemeral=True)
        self.game.night_actions[self.actor.id] = {"action": "shojo_peek", "target": None}
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="👁️ Tu entrouvres la porte... espérons que tu ne sois pas vue.",
            view=self,
        )
        self.stop()

    @discord.ui.button(label="💤 Rester cachée", style=discord.ButtonStyle.secondary)
    async def hide(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.actor.id:
            return await interaction.response.send_message("Pas ton action.", ephemeral=True)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="💤 Tu restes cachée cette nuit.",
            view=self,
        )
        self.stop()


# ─── Vote Views ───

class CaptainVoteView(discord.ui.View):
    """Vote pour élire le Capitaine au jour 1."""
    def __init__(self, game: Game, timeout=None):
        if timeout is None:
            timeout = get_timing("captain_vote")
        super().__init__(timeout=timeout)
        self.game = game
        self.votes = {}  # voter_id → target_id
        self.message = None
        candidates = game.alive_players()
        options = [
            discord.SelectOption(label=m.display_name[:80], value=str(m.id))
            for m in candidates[:25]
        ]
        self.add_item(CaptainVoteSelect(self, options))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class CaptainVoteSelect(discord.ui.Select):
    def __init__(self, parent_view: CaptainVoteView, options):
        super().__init__(placeholder="Pour qui votes-tu comme Sonchō ?", min_values=1, max_values=1, options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        v = self.parent_view
        uid = interaction.user.id
        if uid not in [m.id for m in v.game.participants]:
            return await interaction.response.send_message("Tu ne participes pas.", ephemeral=True)
        if uid not in v.game.alive:
            return await interaction.response.send_message("Tu es mort, tu ne peux pas voter.", ephemeral=True)
        target_id = int(self.values[0])
        v.votes[uid] = target_id
        await interaction.response.send_message(
            "✅ Ton vote a été enregistré (anonyme).", ephemeral=True
        )


class DayVoteView(discord.ui.View):
    """Vote du jour pour éliminer un joueur."""
    def __init__(self, game: Game, timeout=None):
        if timeout is None:
            timeout = get_timing("vote_day")
        super().__init__(timeout=timeout)
        self.game = game
        self.votes = {}
        self.message = None
        candidates = game.alive_players()
        options = [
            discord.SelectOption(label=m.display_name[:80], value=str(m.id))
            for m in candidates[:25]
        ]
        self.add_item(DayVoteSelect(self, options))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class DayVoteSelect(discord.ui.Select):
    def __init__(self, parent_view: DayVoteView, options):
        super().__init__(placeholder="Qui doit être éliminé ?", min_values=1, max_values=1, options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        v = self.parent_view
        uid = interaction.user.id
        if uid not in [m.id for m in v.game.participants]:
            return await interaction.response.send_message("Tu ne participes pas.", ephemeral=True)
        if uid not in v.game.alive:
            return await interaction.response.send_message("Tu es mort, tu ne peux pas voter.", ephemeral=True)
        target_id = int(self.values[0])
        v.votes[uid] = target_id
        await interaction.response.send_message(
            "✅ Ton vote a été enregistré (anonyme).", ephemeral=True
        )


# ─── Menu de lancement ───

class SizePresetSelect(discord.ui.Select):
    def __init__(self, host: discord.Member):
        options = []
        for key, preset in SIZE_PRESETS.items():
            options.append(discord.SelectOption(
                label=preset["label"],
                value=key,
                emoji=preset["emoji"],
                description=preset["description"][:100],
            ))
        super().__init__(
            placeholder="Choisis le format de ta partie...",
            min_values=1, max_values=1, options=options,
        )
        self.host = host

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.host.id:
            return await interaction.response.send_message(
                f"Seul {self.host.mention} peut choisir.", ephemeral=True
            )
        preset_key = self.values[0]
        preset = SIZE_PRESETS[preset_key]

        if preset_key == "custom":
            await interaction.response.send_modal(CustomSizeModal(self.host))
            for item in self.view.children:
                item.disabled = True
            try:
                await interaction.message.edit(view=self.view)
            except discord.HTTPException:
                pass
            self.view.stop()
            return

        player_count = preset["default"]
        for item in self.view.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"✅ Format sélectionné : **{preset['label']}** ({player_count} joueurs cible)",
            view=self.view,
        )
        self.view.stop()
        ctx = await bot.get_context(interaction.message)
        ctx.author = self.host
        await GameManager.start_game(ctx, preset_key, player_count)


class SizeSelectView(discord.ui.View):
    def __init__(self, host: discord.Member, timeout=120):
        super().__init__(timeout=timeout)
        self.add_item(SizePresetSelect(host))


class CustomSizeModal(discord.ui.Modal, title="Partie personnalisée"):
    def __init__(self, host: discord.Member):
        super().__init__()
        self.host = host
        self.count_input = discord.ui.TextInput(
            label=f"Nombre de joueurs ({MIN_PLAYERS}-{MAX_PLAYERS})",
            placeholder=f"Entre {MIN_PLAYERS} et {MAX_PLAYERS}",
            required=True,
            max_length=2,
        )
        self.add_item(self.count_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(self.count_input.value.strip())
        except ValueError:
            return await interaction.response.send_message("Il faut un nombre entier.", ephemeral=True)
        if n < MIN_PLAYERS or n > MAX_PLAYERS:
            return await interaction.response.send_message(
                f"Le nombre doit être entre {MIN_PLAYERS} et {MAX_PLAYERS}.", ephemeral=True
            )
        await interaction.response.send_message(
            f"✅ Partie personnalisée : **{n} joueurs cible**", ephemeral=True
        )

        # Construit un faux ctx
        class _Fake:
            pass
        ctx = _Fake()
        ctx.author = self.host
        ctx.guild = interaction.guild
        ctx.channel = interaction.channel
        ctx.send = interaction.channel.send
        ctx.bot = bot
        ctx.message = None
        await GameManager.start_game(ctx, "custom", n)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                  PARTIE 3 — COMMANDES, HELP, RUN                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# ========================= COMMANDE !lg / !partie =========================

@bot.command(name="lg", aliases=["partie", "loup", "jinro"])
async def _lg(ctx):
    """Lance une nouvelle partie de loup-garou."""
    if await check_ban(ctx):
        return
    if not has_min_rank(ctx.author.id, 2):
        return await ctx.send(embed=error_embed(
            "❌ Permission refusée", "**MJ+** requis pour lancer une partie."
        ))
    if ctx.channel.id in active_games:
        return await ctx.send(embed=error_embed(
            "❌ Partie en cours",
            "Une partie est déjà active dans ce salon."
        ))

    em = discord.Embed(
        title="🌙 Nouvelle nuit à Shoen",
        description=(
            f"**Hôte :** {ctx.author.mention}\n\n"
            f"Choisis le **format** de la partie ci-dessous.\n"
            f"*Une fois le format choisi, les inscriptions s'ouvrent pour 10 min max.*"
        ),
        color=0x3498db,
    )
    em.set_footer(text=FOOTER_TEXT)
    view = SizeSelectView(ctx.author)
    await ctx.send(embed=em, view=view)


@bot.command(name="abort")
async def _abort(ctx):
    """Annule une partie en cours (MJ+)."""
    if not has_min_rank(ctx.author.id, 2):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**MJ+** requis."))
    game = active_games.get(ctx.channel.id)
    if not game:
        return await ctx.send(embed=error_embed("❌ Aucune partie", "Aucune partie n'est active."))
    game.phase = "ENDED"
    active_games.pop(ctx.channel.id, None)
    await ctx.send(embed=success_embed("✅ Partie annulée", "La partie a été annulée par un MJ."))


@bot.command(name="roles")
async def _roles_cmd(ctx):
    """Liste tous les rôles."""
    if await check_ban(ctx):
        return
    wolf_lines = []
    village_lines = []
    solo_lines = []
    for key, role in ROLES.items():
        line = f"{role['emoji']} **{role['name']}** *({role['name_fr']})*"
        if role["camp"] == "wolf":
            wolf_lines.append(line)
        elif role["camp"] == "village":
            village_lines.append(line)
        else:
            solo_lines.append(line)
    em = discord.Embed(title="🎴 Rôles de Jinrō", color=embed_color())
    em.add_field(name=f"🐺 Camp Loup ({len(wolf_lines)})", value="\n".join(wolf_lines), inline=False)
    em.add_field(name=f"🏯 Camp Village ({len(village_lines)})", value="\n".join(village_lines), inline=False)
    em.add_field(name=f"✨ Camp Solo ({len(solo_lines)})", value="\n".join(solo_lines), inline=False)
    em.set_footer(text=f"{FOOTER_TEXT} ・ {get_prefix_cached()}role <clé> pour les détails")
    await ctx.send(embed=em)


@bot.command(name="role")
async def _role_detail(ctx, role_key: str = None):
    """Affiche le détail d'un rôle."""
    if await check_ban(ctx):
        return
    if not role_key or role_key not in ROLES:
        valid = ", ".join(f"`{k}`" for k in ROLES.keys())
        return await ctx.send(embed=error_embed(
            "❌ Rôle inconnu", f"Rôles valides : {valid}"
        ))
    role = ROLES[role_key]
    em = discord.Embed(
        title=f"{role['emoji']} {role['name']} ({role['name_fr']})",
        description=(
            f"**Camp :** {GameManager.camp_display(role['camp'])}\n\n"
            f"{role['description']}"
        ),
        color=0xe74c3c if role["camp"] == "wolf" else (0xf1c40f if role["camp"] == "solo" else 0x3498db),
    )
    if role.get("action_desc"):
        em.add_field(name="🎯 Action", value=role["action_desc"], inline=False)
    em.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=em)


# ========================= STATS =========================

@bot.command(name="stats", aliases=["profil"])
async def _stats(ctx, *, user_input: str = None):
    if await check_ban(ctx):
        return
    target = ctx.author
    if user_input:
        resolved = await resolve_member(ctx, user_input)
        target = resolved if resolved else ctx.author

    stats = get_player_stats(target.id)
    role_counts = get_role_counts(target.id)
    badges_list = get_user_badges(target.id)

    level = stats["level"]
    xp = stats["xp"]
    xp_curr_level = xp_for_level(level)
    xp_next_level = xp_for_level(level + 1) if level < 100 else xp
    xp_progress = xp - xp_curr_level
    xp_required = xp_next_level - xp_curr_level if level < 100 else 0
    current_class = class_for_level(level)
    next_class, next_class_lvl = next_class_info(level)

    games = stats["games_played"]
    wins = stats["games_won"]
    winrate = f"{(wins/games*100):.1f}%" if games > 0 else "—"
    vote_total = stats["correct_votes"] + stats["wrong_votes"]
    vote_acc = f"{(stats['correct_votes']/vote_total*100):.1f}%" if vote_total > 0 else "—"
    wolf_games = stats["times_wolf"]
    wolf_winrate = f"{(stats['wolf_wins']/wolf_games*100):.1f}%" if wolf_games > 0 else "—"

    fav_role_line = "*Aucun rôle joué*"
    if role_counts:
        top_key, top_count = role_counts[0]
        role = ROLES.get(top_key)
        if role:
            fav_role_line = f"{role['emoji']} **{role['name_fr']}** ({top_count}x)"

    lines = [
        f"🎴 **{current_class}** ・ Niveau **{level}** / 100",
        f"✨ **{xp}** XP" + (f"  ・  *{xp_progress} / {xp_required}*" if level < 100 else "  ・  *MAX*"),
    ]
    if next_class:
        lines.append(f"🎯 Prochaine classe : **{next_class}** (niveau {next_class_lvl})")
    lines.append("")
    lines.append(f"🎲 **{games}** parties  ・  🏆 **{wins}** gagnées ({winrate})")
    lines.append(f"🗳️ **{stats['correct_votes']}** votes justes  ・  Précision : {vote_acc}")
    lines.append(f"🐺 **{wolf_games}** parties en loup  ・  **{stats['wolf_wins']}** victoires ({wolf_winrate})")
    lines.append(f"👑 **{stats['times_captain']}** fois Sonchō  ・  🏹 **{stats['kills_as_hunter']}** kills en Chasseur")
    lines.append("")
    lines.append(f"⭐ Rôle préféré : {fav_role_line}")
    lines.append(f"🏅 Badges : **{len(badges_list)}** / {len(BADGES)}")

    em = discord.Embed(title=target.display_name, description="\n".join(lines), color=embed_color())
    em.set_thumbnail(url=target.display_avatar.url)
    em.set_footer(text=f"{FOOTER_TEXT} ・ {get_prefix_cached()}badges pour voir les badges")
    await ctx.send(embed=em)


@bot.command(name="badges")
async def _badges(ctx, *, user_input: str = None):
    if await check_ban(ctx):
        return
    target = ctx.author
    if user_input:
        resolved = await resolve_member(ctx, user_input)
        target = resolved if resolved else ctx.author

    owned = get_user_badges(target.id)
    owned_keys = {k for k, _ in owned}

    lines = []
    unlocked_count = 0
    for bkey, bdata in BADGES.items():
        if bkey in owned_keys:
            lines.append(f"{bdata['emoji']} **{bdata['name']}** — *{bdata['desc']}*")
            unlocked_count += 1
        else:
            lines.append(f"🔒 ~~{bdata['name']}~~ — *{bdata['desc']}*")

    em = discord.Embed(
        title=f"🏅 Badges — {target.display_name}",
        description=f"**{unlocked_count}** / {len(BADGES)} badges débloqués\n\n" + "\n".join(lines),
        color=embed_color(),
    )
    em.set_thumbnail(url=target.display_avatar.url)
    em.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=em)


@bot.command(name="classement", aliases=["lb", "leaderboard"])
async def _classement(ctx, metric: str = "xp"):
    if await check_ban(ctx):
        return
    metric_map = {
        "xp":      ("xp", "✨ Classement XP", "XP"),
        "wins":    ("games_won", "🏆 Classement Victoires", "victoires"),
        "games":   ("games_played", "🎲 Parties jouées", "parties"),
        "village": ("correct_votes", "🔍 Classement Détective village", "votes justes"),
        "wolf":    ("wolf_wins", "🐺 Classement Loups", "victoires en loup"),
        "hunter":  ("kills_as_hunter", "🏹 Classement Chasseur", "kills"),
    }
    if metric not in metric_map:
        return await ctx.send(embed=error_embed(
            "❌ Métrique inconnue",
            f"Métriques : `{'`, `'.join(metric_map.keys())}`"
        ))
    db_field, title, label = metric_map[metric]
    top = get_leaderboard(db_field, limit=10)
    if not top:
        return await ctx.send(embed=info_embed(title, "*Aucun joueur classé*"))
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, value) in enumerate(top):
        rank_marker = medals[i] if i < 3 else f"**{i+1}.**"
        member = ctx.guild.get_member(int(uid)) if ctx.guild else None
        name = member.mention if member else f"<@{uid}>"
        lines.append(f"{rank_marker} {name} ・ **{value}** {label}")
    em = discord.Embed(title=title, description="\n".join(lines), color=embed_color())
    em.set_footer(text=f"{FOOTER_TEXT} ・ {get_prefix_cached()}classement <{'/'.join(metric_map.keys())}>")
    await ctx.send(embed=em)


@bot.command(name="history")
async def _history(ctx):
    if await check_ban(ctx):
        return
    games = get_recent_games(ctx.guild.id, limit=10)
    if not games:
        return await ctx.send(embed=info_embed("📜 Historique", "Aucune partie enregistrée."))
    lines = []
    for g in games:
        try:
            ended = datetime.fromisoformat(g["ended_at"]).strftime("%d/%m %Hh%M")
        except (ValueError, TypeError):
            ended = "?"
        camp_emoji = {"wolf": "🐺", "village": "🏯", "lovers": "💞", "tenshi": "😇"}.get(g["winning_camp"], "🤷")
        camp_label = {"wolf": "Loups", "village": "Village", "lovers": "Amoureux", "tenshi": "Tenshi"}.get(
            g["winning_camp"], "Match nul")
        lines.append(
            f"**{ended}** — {g['player_count']}j ・ {g['nights_played']} nuits ・ "
            f"{camp_emoji} **{camp_label}**"
        )
    em = discord.Embed(
        title=f"📜 Dernières parties ({len(games)})",
        description="\n".join(lines),
        color=embed_color(),
    )
    em.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=em)


# ========================= ADMIN : RANGS =========================

@bot.command(name="sys")
async def _sys(ctx, *, user_input: str = None):
    if user_input is None:
        if not has_min_rank(ctx.author.id, 4):
            return await ctx.send(embed=error_embed("❌ Permission refusée", "**Buyer** requis."))
        ids = get_ranks_by_level(3)
        if not ids:
            return await ctx.send(embed=info_embed("📋 Liste Sys", "Aucun sys."))
        return await ctx.send(embed=info_embed(
            f"📋 Liste Sys ({len(ids)})", "\n".join(f"<@{uid}>" for uid in ids)
        ))
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Buyer** requis."))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention/ID/nom requis."))
    if get_rank_db(uid) == 3:
        return await ctx.send(embed=error_embed("Déjà Sys", f"{format_user_display(display, uid)} est déjà sys."))
    set_rank_db(uid, 3)
    await ctx.send(embed=success_embed("✅ Sys ajouté", f"{format_user_display(display, uid)} est maintenant **sys**."))


@bot.command(name="unsys")
async def _unsys(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Buyer** requis."))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention/ID/nom requis."))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention/ID/nom requis."))
    if get_rank_db(uid) != 3:
        return await ctx.send(embed=error_embed("Pas Sys", f"{format_user_display(display, uid)} n'est pas sys."))
    set_rank_db(uid, 0)
    await ctx.send(embed=success_embed("✅ Sys retiré", f"{format_user_display(display, uid)} n'est plus sys."))


@bot.command(name="mj")
async def _mj(ctx, *, user_input: str = None):
    if user_input is None:
        if not has_min_rank(ctx.author.id, 3):
            return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis."))
        ids = get_ranks_by_level(2)
        if not ids:
            return await ctx.send(embed=info_embed("📋 Liste MJ", "Aucun MJ."))
        return await ctx.send(embed=info_embed(
            f"📋 Liste MJ ({len(ids)})", "\n".join(f"<@{uid}>" for uid in ids)
        ))
    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis."))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention/ID/nom requis."))
    if get_rank_db(uid) >= 3:
        return await ctx.send(embed=error_embed("❌ Erreur", f"{format_user_display(display, uid)} a un rang supérieur."))
    set_rank_db(uid, 2)
    await ctx.send(embed=success_embed("✅ MJ ajouté", f"{format_user_display(display, uid)} est maintenant **MJ**."))


@bot.command(name="unmj")
async def _unmj(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis."))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention/ID/nom requis."))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention/ID/nom requis."))
    if get_rank_db(uid) != 2:
        return await ctx.send(embed=error_embed("Pas MJ", f"{format_user_display(display, uid)} n'est pas MJ."))
    set_rank_db(uid, 0)
    await ctx.send(embed=success_embed("✅ MJ retiré", f"{format_user_display(display, uid)} n'est plus MJ."))


@bot.command(name="joueur")
async def _joueur(ctx, *, user_input: str = None):
    if user_input is None:
        if not has_min_rank(ctx.author.id, 2):
            return await ctx.send(embed=error_embed("❌ Permission refusée", "**MJ+** requis."))
        ids = get_ranks_by_level(1)
        if not ids:
            return await ctx.send(embed=info_embed("📋 Joueurs vérifiés", "Aucun."))
        return await ctx.send(embed=info_embed(
            f"📋 Joueurs vérifiés ({len(ids)})", "\n".join(f"<@{uid}>" for uid in ids)
        ))
    if not has_min_rank(ctx.author.id, 2):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**MJ+** requis."))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention/ID/nom requis."))
    if get_rank_db(uid) >= 2:
        return await ctx.send(embed=error_embed("❌ Erreur", f"{format_user_display(display, uid)} a un rang supérieur."))
    set_rank_db(uid, 1)
    await ctx.send(embed=success_embed("✅ Joueur vérifié", f"{format_user_display(display, uid)} est maintenant **joueur vérifié**."))


@bot.command(name="unjoueur")
async def _unjoueur(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 2):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**MJ+** requis."))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention/ID/nom requis."))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention/ID/nom requis."))
    if get_rank_db(uid) != 1:
        return await ctx.send(embed=error_embed("Pas vérifié", f"{format_user_display(display, uid)} n'est pas vérifié."))
    set_rank_db(uid, 0)
    await ctx.send(embed=success_embed("✅ Vérification retirée", f"{format_user_display(display, uid)} n'est plus vérifié."))


# ========================= ADMIN : BAN =========================

@bot.command(name="ban")
async def _ban(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis."))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention/ID/nom requis."))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention/ID/nom requis."))
    if get_rank_db(uid) >= get_rank_db(ctx.author.id):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "Tu ne peux pas bannir un rang égal ou supérieur."))
    if is_bot_banned(uid):
        return await ctx.send(embed=error_embed("Déjà banni", f"{format_user_display(display, uid)} est déjà banni."))
    add_bot_ban(uid, ctx.author.id)
    await ctx.send(embed=success_embed("⛔ Banni du bot", f"{format_user_display(display, uid)} ne peut plus utiliser Jinrō."))


@bot.command(name="unban")
async def _unban(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis."))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention/ID/nom requis."))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention/ID/nom requis."))
    if not is_bot_banned(uid):
        return await ctx.send(embed=error_embed("Pas banni", f"{format_user_display(display, uid)} n'est pas banni."))
    remove_bot_ban(uid)
    await ctx.send(embed=success_embed("✅ Débanni", f"{format_user_display(display, uid)} peut à nouveau utiliser Jinrō."))


# ========================= ADMIN : ALLOW =========================

async def _resolve_channel(ctx, channel_input):
    clean = channel_input.strip("<#>")
    try:
        cid = int(clean)
        ch = ctx.guild.get_channel(cid)
        return ch, cid
    except ValueError:
        pass
    try:
        ch = await commands.TextChannelConverter().convert(ctx, channel_input)
        return ch, ch.id
    except commands.CommandError:
        return None, None


@bot.command(name="allow")
async def _allow(ctx, *, channel_input: str = None):
    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis."))
    if channel_input is None:
        allowed = get_allowed_channels(ctx.guild.id)
        if not allowed:
            return await ctx.send(embed=info_embed(
                "📋 Aucun salon autorisé",
                f"Utilise `{get_prefix_cached()}allow #salon` pour autoriser un salon."
            ))
        lines = []
        for cid in allowed:
            ch = ctx.guild.get_channel(int(cid))
            lines.append(f"• {ch.mention} (`{cid}`)" if ch else f"• *Salon inaccessible* (`{cid}`)")
        return await ctx.send(embed=info_embed(f"📋 Salons autorisés ({len(allowed)})", "\n".join(lines)))
    channel, raw_id = await _resolve_channel(ctx, channel_input)
    if not channel:
        return await ctx.send(embed=error_embed("❌ Salon introuvable", "Mention #salon ou ID."))
    if is_channel_allowed(ctx.guild.id, channel.id):
        return await ctx.send(embed=error_embed("Déjà autorisé", f"{channel.mention} est déjà autorisé."))
    add_allowed_channel(ctx.guild.id, channel.id, ctx.author.id)
    await ctx.send(embed=success_embed("✅ Salon autorisé", f"{channel.mention} est maintenant autorisé."))
    await send_log(ctx.guild, "Salon autorisé", ctx.author,
                   desc=f"Salon : {channel.mention} (`{channel.id}`)", color=0x43b581)


@bot.command(name="unallow")
async def _unallow(ctx, *, channel_input: str = None):
    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis."))
    if not channel_input:
        return await ctx.send(embed=error_embed("Argument manquant", f"Usage : `{get_prefix_cached()}unallow #salon`"))
    channel, raw_id = await _resolve_channel(ctx, channel_input)
    if not channel:
        if raw_id is not None:
            if remove_allowed_channel(ctx.guild.id, raw_id):
                return await ctx.send(embed=success_embed("✅ Salon retiré", f"Salon `{raw_id}` retiré."))
            return await ctx.send(embed=error_embed("Pas dans la liste", f"Salon `{raw_id}` pas autorisé."))
        return await ctx.send(embed=error_embed("❌ Salon introuvable", "Mention ou ID."))
    if not remove_allowed_channel(ctx.guild.id, channel.id):
        return await ctx.send(embed=error_embed("Pas dans la liste", f"{channel.mention} pas autorisé."))
    await ctx.send(embed=success_embed("✅ Salon retiré", f"{channel.mention} n'est plus autorisé."))
    await send_log(ctx.guild, "Salon retiré", ctx.author,
                   desc=f"Salon : {channel.mention} (`{channel.id}`)", color=0xf04747)


# ========================= ADMIN : SYSTÈME =========================

@bot.command(name="prefix")
async def _prefix(ctx, new_prefix: str = None):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "Seul le **Buyer** peut changer le prefix."))
    if not new_prefix:
        return await ctx.send(embed=info_embed("Prefix actuel", f"`{get_prefix_cached()}`"))
    set_config("prefix", new_prefix)
    await ctx.send(embed=success_embed("✅ Prefix modifié", f"Nouveau prefix : `{new_prefix}`"))


@bot.command(name="setlog")
async def _setlog(ctx, channel: discord.TextChannel = None):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "Seul le **Buyer** peut définir les logs."))
    if not channel:
        return await ctx.send(embed=error_embed("Argument manquant", "Mentionne un salon."))
    set_log_channel(ctx.guild.id, channel.id)
    await ctx.send(embed=success_embed("✅ Logs configurés", f"Logs dans {channel.mention}."))


TIMING_LABELS = {
    "recruit":      "Recrutement (inscriptions)",
    "night":        "Phase de nuit (acteurs + loups)",
    "witch":        "Phase Sorcière (après loups)",
    "debate":       "Débat du jour",
    "vote_day":     "Vote du jour",
    "captain_vote": "Élection du Capitaine",
    "hunter":       "Tir du Chasseur",
    "successor":    "Choix du successeur (Sonchō)",
}


@bot.command(name="settime", aliases=["timings", "settiming"])
async def _settime(ctx, phase: str = None, seconds: int = None):
    """Sys+ : modifie les durées des phases. `!settime` seul = liste."""
    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis."))

    # Sans argument : liste actuelle
    if phase is None:
        lines = []
        for key, label in TIMING_LABELS.items():
            cur = get_timing(key)
            default = DEFAULT_TIMINGS[key]
            marker = "" if cur == default else "  *(modifié)*"
            lines.append(f"`{key:<13}` **{cur}s**  — *{label}*{marker}")
        em = info_embed(
            "⏱️ Durées des phases",
            "\n".join(lines) +
            f"\n\n*Usage :* `{get_prefix_cached()}settime <phase> <secondes>`\n"
            f"*Reset :* `{get_prefix_cached()}settime <phase> reset`"
        )
        await ctx.send(embed=em)
        return

    phase = phase.lower().strip()
    if phase not in DEFAULT_TIMINGS:
        valid = ", ".join(f"`{k}`" for k in DEFAULT_TIMINGS)
        return await ctx.send(embed=error_embed(
            "❌ Phase inconnue",
            f"Phases valides : {valid}"
        ))

    # Reset : !settime <phase> reset (passé via le second arg textuel)
    # Comme seconds est typé int, on passe par ctx.message
    raw = ctx.message.content.split(maxsplit=3)
    if len(raw) >= 3 and raw[2].lower() == "reset":
        reset_timing(phase)
        return await ctx.send(embed=success_embed(
            "✅ Timing réinitialisé",
            f"`{phase}` est revenu à **{DEFAULT_TIMINGS[phase]}s** (défaut)."
        ))

    if seconds is None:
        return await ctx.send(embed=error_embed(
            "Argument manquant",
            f"Usage : `{get_prefix_cached()}settime {phase} <secondes>`\n"
            f"Actuel : **{get_timing(phase)}s** ・ Défaut : **{DEFAULT_TIMINGS[phase]}s**"
        ))

    if seconds < 5 or seconds > 3600:
        return await ctx.send(embed=error_embed(
            "❌ Valeur invalide",
            "La durée doit être entre **5** et **3600** secondes (1 heure)."
        ))

    set_timing(phase, seconds)
    label = TIMING_LABELS.get(phase, phase)
    await ctx.send(embed=success_embed(
        "✅ Timing modifié",
        f"**{label}** : maintenant **{seconds}s** *(défaut {DEFAULT_TIMINGS[phase]}s)*"
    ))


@bot.command(name="resetstats")
async def _resetstats(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis."))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention/ID/nom requis."))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention/ID/nom requis."))
    async with stats_lock:
        conn = get_db()
        conn.execute("DELETE FROM player_stats WHERE user_id = ?", (str(uid),))
        conn.execute("DELETE FROM role_counts WHERE user_id = ?", (str(uid),))
        conn.execute("DELETE FROM badges WHERE user_id = ?", (str(uid),))
        conn.commit()
        conn.close()
    await ctx.send(embed=success_embed("✅ Stats reset", f"Stats de {format_user_display(display, uid)} supprimées."))
    await send_log(ctx.guild, "Reset stats", ctx.author,
                   desc=f"Cible : {format_user_display(display, uid)}", color=0xe67e22)


@bot.command(name="addbuyer")
async def _addbuyer(ctx, *, user_input: str = None):
    """Ajoute un buyer (Buyer uniquement)."""
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "Seul un **Buyer** peut en ajouter."))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention/ID requis."))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention/ID/nom requis."))
    buyer_ids_raw = get_config("buyer_ids")
    buyer_ids = json.loads(buyer_ids_raw) if buyer_ids_raw else []
    if str(uid) in buyer_ids:
        return await ctx.send(embed=error_embed("Déjà Buyer", f"{format_user_display(display, uid)} est déjà Buyer."))
    buyer_ids.append(str(uid))
    set_config("buyer_ids", json.dumps(buyer_ids))
    await ctx.send(embed=success_embed("✅ Buyer ajouté", f"{format_user_display(display, uid)} est maintenant **Buyer**."))


@bot.command(name="removebuyer")
async def _removebuyer(ctx, *, user_input: str = None):
    """Retire un buyer (Buyer uniquement, sauf soi-même)."""
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "Seul un **Buyer** peut en retirer."))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention/ID requis."))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention/ID/nom requis."))
    buyer_ids_raw = get_config("buyer_ids")
    buyer_ids = json.loads(buyer_ids_raw) if buyer_ids_raw else []
    if str(uid) not in buyer_ids:
        return await ctx.send(embed=error_embed("Pas Buyer", f"{format_user_display(display, uid)} n'est pas Buyer."))
    if len(buyer_ids) <= 1:
        return await ctx.send(embed=error_embed("Impossible", "Il doit rester au moins 1 Buyer."))
    buyer_ids.remove(str(uid))
    set_config("buyer_ids", json.dumps(buyer_ids))
    await ctx.send(embed=success_embed("✅ Buyer retiré", f"{format_user_display(display, uid)} n'est plus Buyer."))


# ========================= HELP DYNAMIQUE =========================

HELP_CATEGORIES = {
    "jeu": {
        "emoji": "🎮", "label": "Jeu", "title": "🎮  Jeu",
        "items": [
            ("lg",          "Lancer une partie (MJ+)", 2),
            ("partie",      "Alias de lg", 2),
            ("abort",       "Annuler la partie en cours (MJ+)", 2),
            ("roles",       "Liste des rôles", 0),
            ("role <clé>",  "Détail d'un rôle", 0),
        ],
    },
    "profil": {
        "emoji": "👤", "label": "Profil", "title": "👤  Profil & Stats",
        "items": [
            ("stats [@user]",       "Stats d'un joueur", 0),
            ("badges [@user]",      "Badges d'un joueur", 0),
            ("classement <metric>", "Leaderboard (xp/wins/games/village/wolf/hunter)", 0),
            ("history",             "10 dernières parties", 0),
        ],
    },
    "perms": {
        "emoji": "👥", "label": "Permissions", "title": "👥  Permissions",
        "items": [
            ("joueur @u / unjoueur @u", "Gérer les joueurs vérifiés", 2),
            ("mj @u / unmj @u",         "Gérer les MJ", 3),
            ("sys @u / unsys @u",       "Gérer les Sys", 4),
            ("addbuyer @u / removebuyer @u", "Gérer les Buyers", 4),
        ],
    },
    "admin": {
        "emoji": "🔧", "label": "Admin", "title": "🔧  Admin",
        "items": [
            ("ban @u",         "Bannir du bot", 3),
            ("unban @u",       "Débannir du bot", 3),
            ("resetstats @u",  "Reset stats d'un joueur", 3),
        ],
    },
    "system": {
        "emoji": "⚙️", "label": "Système", "title": "⚙️  Système",
        "items": [
            ("allow #salon",            "Autoriser un salon", 3),
            ("unallow #salon",          "Retirer un salon autorisé", 3),
            ("allow",                   "Lister les salons autorisés", 3),
            ("settime",                 "Voir toutes les durées de phases", 3),
            ("settime <phase> <sec>",   "Modifier la durée d'une phase", 3),
            ("settime <phase> reset",   "Remettre la durée par défaut", 3),
            ("setlog #salon",           "Salon de logs", 4),
            ("prefix [new]",            "Changer le prefix", 4),
        ],
    },
    "hierarchy": {
        "emoji": "📋", "label": "Hiérarchie", "title": "📋  Hiérarchie",
        "min_rank": 2, "items": [],
    },
}


def help_accessible_items(key, rank):
    cat = HELP_CATEGORIES.get(key, {})
    return [(s, d) for (s, d, mr) in cat.get("items", []) if rank >= mr]


def help_category_visible(key, rank):
    cat = HELP_CATEGORIES.get(key, {})
    if "min_rank" in cat:
        return rank >= cat["min_rank"]
    return len(help_accessible_items(key, rank)) > 0


def build_help_category_embed(key, rank):
    p = get_prefix_cached()
    cat = HELP_CATEGORIES[key]
    em = discord.Embed(title=cat["title"], color=embed_color())
    items = help_accessible_items(key, rank)
    if not items:
        em.description = "*Aucune commande accessible à ton rang.*"
    else:
        max_syntax = max(len(f"{p}{syntax}") for syntax, _ in items)
        lines = [f"{p}{syntax}".ljust(max_syntax + 2) + f"→ {desc}" for syntax, desc in items]
        em.description = "```\n" + "\n".join(lines) + "\n```"
    em.set_footer(text=FOOTER_TEXT)
    return em


def build_help_hierarchy_embed(rank):
    em = discord.Embed(title="📋  Hiérarchie", color=embed_color())
    lines = ["```\nBuyer > Sys > MJ > Joueur vérifié > Tout le monde\n```\n"]
    levels = [
        (4, "👑 **Buyer**",          "Accès total : `!prefix`, `!setlog`, `!addbuyer`, `!sys`/`!unsys`"),
        (3, "🔧 **Sys**",             "`!allow`/`!unallow`, `!ban`/`!unban`, `!mj`/`!unmj`, `!resetstats`"),
        (2, "🎭 **MJ**",              "`!lg`, `!abort`, `!joueur`/`!unjoueur`"),
        (1, "✨ **Joueur vérifié**",   "Statut privilégié, identique aux membres sinon"),
        (0, "👤 **Tout le monde**",   "Voir stats, badges, classement, rôles"),
    ]
    for lvl, name, desc in levels:
        marker = " ← **toi**" if lvl == rank else ""
        lines.append(f"> {name} — {desc}{marker}")
    em.description = "\n".join(lines)
    em.set_footer(text=FOOTER_TEXT)
    return em


def build_help_home_embed(rank):
    p = get_prefix_cached()
    em = discord.Embed(color=embed_color())
    em.set_author(name="Jinrō ─ Panel d'aide")
    rank_label = rank_name(rank)
    intro = (
        f"```\n🕐  {get_french_time()}\n```\n"
        f"Bienvenue à **Shoen**. Méfie-toi : des loups marchent parmi les villageois.\n\n"
        f"**Prefix :** `{p}` ・ **Ton rang :** {rank_label}\n\n"
    )
    category_descriptions = {
        "jeu":       "Lancer/gérer les parties, voir les rôles",
        "profil":    "Stats personnelles, badges, classement",
        "perms":     "Attribuer les rangs",
        "admin":     "Modération des joueurs",
        "system":    "Configuration du bot",
        "hierarchy": "Qui peut faire quoi",
    }
    visible = []
    for key, lbl in category_descriptions.items():
        if help_category_visible(key, rank):
            cat = HELP_CATEGORIES[key]
            visible.append(f"> {cat['emoji']} **{cat['label']}** — {lbl}")
    em.description = intro + ("\n".join(visible) if visible else "")
    em.set_footer(text=FOOTER_TEXT)
    return em


def build_help_embed_for(key, rank):
    if key == "home":
        return build_help_home_embed(rank)
    if key == "hierarchy":
        return build_help_hierarchy_embed(rank)
    return build_help_category_embed(key, rank)


class HelpDropdown(discord.ui.Select):
    def __init__(self, user_rank):
        self.user_rank = user_rank
        options = [discord.SelectOption(label="Accueil", emoji="🏠", value="home")]
        for key, cat in HELP_CATEGORIES.items():
            if help_category_visible(key, user_rank):
                options.append(discord.SelectOption(label=cat["label"], emoji=cat["emoji"], value=key))
        super().__init__(placeholder="📂 Choisis une catégorie...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        if key != "home" and not help_category_visible(key, self.user_rank):
            return await interaction.response.send_message("Tu n'as pas accès à cette catégorie.", ephemeral=True)
        await interaction.response.edit_message(
            embed=build_help_embed_for(key, self.user_rank), view=self.view
        )


class HelpView(discord.ui.View):
    def __init__(self, author_id, user_rank):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.user_rank = user_rank
        self.add_item(HelpDropdown(user_rank))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                f"Ce menu n'est pas à toi. Fais `{get_prefix_cached()}help` pour le tien.",
                ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


@bot.command(name="help")
async def _help(ctx):
    rank = get_rank_db(ctx.author.id)
    view = HelpView(ctx.author.id, rank)
    await ctx.send(embed=build_help_home_embed(rank), view=view)


# ========================= RUN =========================

if __name__ == "__main__":
    try:
        log.info("Démarrage de Jinrō...")
        bot.run(BOT_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        log.info("Arrêt demandé par l'utilisateur.")
    except Exception as e:
        log.error(f"Erreur fatale au démarrage : {e}", exc_info=True)
        sys.exit(1)
