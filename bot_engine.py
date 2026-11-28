"""Aggressive NPC bot accounts that periodically invade player islands."""

import copy
import random
import secrets
from datetime import datetime

from flask import current_app, has_request_context, session

import auth_engine
from game_settings import initial_island
from save_engine import db, get_saves, store_session

BOT_PROFILES = [
    {
        "username": "bot_viper",
        "island_name": "Viper Division",
        "zid": 900001,
        "level": 35,
        "units": ["UN68", "UN34", "UN02", "UM83", "UN87"],
    },
    {
        "username": "bot_kodiak",
        "island_name": "Kodiak Armor",
        "zid": 900002,
        "level": 32,
        "units": ["UN02", "UN60", "UM71", "UN34", "UN68"],
    },
    {
        "username": "bot_valkyrie",
        "island_name": "Valkyrie Wing",
        "zid": 900003,
        "level": 38,
        "units": ["UC68", "UC25", "UB94", "UB65", "UC71"],
    },
    {
        "username": "bot_obsidian",
        "island_name": "Obsidian Fleet",
        "zid": 900004,
        "level": 36,
        "units": ["UU37", "UU02", "UT71", "UU72", "UT49"],
    },
    {
        "username": "bot_hydra",
        "island_name": "Hydra Command",
        "zid": 900005,
        "level": 40,
        "units": ["UC25", "UC68", "UB94", "UC28", "UB52"],
    },
]

BOT_ZIDS = {b["zid"] for b in BOT_PROFILES}
_LAST_MEM_CHECK = 0.0


class BotConfig(db.Model):
    __tablename__ = "bot_config"

    id = db.Column(db.Integer, primary_key=True)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    interval_seconds = db.Column(db.Integer, default=1800, nullable=False)
    max_bots_per_target = db.Column(db.Integer, default=2, nullable=False)
    min_target_level = db.Column(db.Integer, default=3, nullable=False)
    last_tick_ts = db.Column(db.Float, default=0.0, nullable=False)


def get_bot_config():
    cfg = db.session.get(BotConfig, 1)
    if cfg is None:
        cfg = BotConfig(id=1)
        db.session.add(cfg)
        db.session.commit()
    return cfg


def _format_unit(code):
    return f"{code},0,0,0,0,0,0"


def _build_bot_save(bot):
    objects = initial_island["objects"]
    roads = initial_island["roads"]
    return {
        "user_object": {
            "userInfo": {
                "worldName": bot["island_name"],
                "player": {
                    "uid": bot["zid"],
                    "level": bot["level"],
                    "xp": bot["level"] * 5000,
                    "cash": 500,
                    "energy": 100,
                    "energyMax": 100,
                    "playerResourceType": 0,
                    "socialXpGood": 0,
                    "socialLevelGood": 1,
                    "socialXpBad": 500,
                    "socialLevelBad": 5,
                    "inventory": {"items": {}},
                },
                "world": {
                    "objects": copy.deepcopy(objects),
                    "roads": copy.deepcopy(roads),
                    "resources": {
                        "coins": 50000, "oil": 5000, "wood": 5000,
                        "aluminum": 1000, "copper": 0, "gold": 0, "iron": 0, "uranium": 0,
                    },
                    "research": {},
                    "questProgress": {},
                    "rewardRandSeedZ": 12345,
                    "rewardRandSeedW": 67890,
                },
            },
            "pvp": {
                "invaders": {"pve": {"quest": True}},
                "gpInv": None,
                "nAssists": None,
                "immunity": [],
            },
        },
        "quests": [],
        "fleets": {},
        "battles": [],
        "battle_status": {},
    }


def ensure_bot_accounts():
    """Idempotently create the 5 hard bot accounts and their island saves."""
    import pickle
    sess_model = current_app.session_interface.sql_session_model
    changed = False
    for bot in BOT_PROFILES:
        sid = f"session:bot_{bot['zid']}"
        acc = auth_engine.find_account(bot["username"])
        if acc is None:
            acc = auth_engine.Account(
                username=bot["username"],
                island_name=bot["island_name"],
                session_id=sid,
                role="bot",
            )
            acc.set_password(secrets.token_hex(24))
            db.session.add(acc)
            changed = True
        elif acc.role != "bot" or acc.session_id != sid:
            acc.role = "bot"
            acc.session_id = sid
            changed = True

        rec = sess_model.query.filter_by(session_id=sid).first()
        if rec is None:
            save_data = pickle.dumps(_build_bot_save(bot))
            db.session.add(sess_model(session_id=sid, data=save_data, expiry=datetime(2099, 1, 1)))
            changed = True
    if changed:
        db.session.commit()
    get_bot_config()


def _pick_invasion_pos(save):
    objs = save["user_object"]["userInfo"]["world"].get("objects") or []
    candidates = [o.get("position") for o in objs if isinstance(o, dict) and o.get("position")]
    if candidates:
        pos = random.choice(candidates).split(",")
        if len(pos) >= 2:
            return f"{pos[0]},{pos[1]}"
    return "57,58"


def _has_active_immunity(user_obj, now_ts):
    imm = (user_obj.get("pvp") or {}).get("immunity") or []
    if isinstance(imm, list):
        for entry in imm:
            if isinstance(entry, dict) and float(entry.get("endTime", 0) or 0) > now_ts:
                return True
    return False


def run_bot_invasions(force=False, announce=True):
    """Send hard bot invasions to eligible non-bot players. Returns summary dict."""
    cfg = get_bot_config()
    now_ts = datetime.now().timestamp()
    if not force:
        if not cfg.enabled:
            return {"ran": False, "reason": "disabled", "invasions": 0}
        if now_ts - cfg.last_tick_ts < cfg.interval_seconds:
            return {"ran": False, "reason": "cooldown", "invasions": 0}

    cfg.last_tick_ts = now_ts
    db.session.commit()

    saves = get_saves()
    total_invasions = 0
    attacking_bots = set()
    active_uid = None
    if has_request_context() and "user_object" in session:
        active_uid = str(session["user_object"]["userInfo"]["player"].get("uid"))

    for save in saves:
        player = save["user_object"]["userInfo"]["player"]
        uid = player.get("uid")
        if uid in BOT_ZIDS or int(player.get("level", 1) or 1) < cfg.min_target_level:
            continue
        if _has_active_immunity(save["user_object"], now_ts):
            continue

        pvp = save["user_object"].setdefault("pvp", {"invaders": {"pve": {"quest": True}}})
        invaders = pvp.setdefault("invaders", {"pve": {"quest": True}})
        already = {k for k in invaders if k.startswith("u")}
        pool = [b for b in BOT_PROFILES if f"u{b['zid']}" not in already]
        if not pool:
            continue

        random.shuffle(pool)
        chosen = pool[: max(1, cfg.max_bots_per_target)]
        for bot in chosen:
            fleet = [_format_unit(c) for c in bot["units"]]
            key = f"u{bot['zid']}"
            invaders[key] = {
                "ts": now_ts,
                "pillTS": now_ts,
                "status": 1,
                "pos": _pick_invasion_pos(save),
                "size": 5,
                "chID": str(bot["zid"]),
                "defender_fleet": list(fleet),
                "attacker_fleet": list(fleet),
            }
            total_invasions += 1
            attacking_bots.add(bot["island_name"])

        store_session(save)
        if active_uid and str(uid) == active_uid:
            session["user_object"].setdefault("pvp", {})["invaders"] = copy.deepcopy(invaders)

    if announce and total_invasions > 0:
        names = ", ".join(sorted(attacking_bots)[:3])
        extra = f" +{len(attacking_bots) - 3}" if len(attacking_bots) > 3 else ""
        msg = auth_engine.ChatMessage(
            account_id=0,
            sender_name="BOT ALERT",
            sender_role="admin",
            message=f"{names}{extra} melancarkan {total_invasions} invasi (Hard)!",
        )
        db.session.add(msg)
        db.session.commit()

    return {"ran": True, "invasions": total_invasions, "bots": sorted(attacking_bots)}


def maybe_tick_bots():
    """Lightweight periodic check called on player requests."""
    global _LAST_MEM_CHECK
    now_ts = datetime.now().timestamp()
    if now_ts - _LAST_MEM_CHECK < 60:
        return
    _LAST_MEM_CHECK = now_ts
    try:
        run_bot_invasions(force=False, announce=True)
    except Exception as e:
        print("Bot tick error:", e)


def get_bots_status():
    cfg = get_bot_config()
    saves = get_saves()
    counts = {b["zid"]: 0 for b in BOT_PROFILES}
    for save in saves:
        invaders = (save["user_object"].get("pvp") or {}).get("invaders") or {}
        for b in BOT_PROFILES:
            if f"u{b['zid']}" in invaders:
                counts[b["zid"]] += 1

    return {
        "enabled": cfg.enabled,
        "interval_seconds": cfg.interval_seconds,
        "max_bots_per_target": cfg.max_bots_per_target,
        "min_target_level": cfg.min_target_level,
        "bots": [
            {
                "username": b["username"],
                "island_name": b["island_name"],
                "zid": b["zid"],
                "level": b["level"],
                "units": b["units"],
                "active_invasions": counts[b["zid"]],
            }
            for b in BOT_PROFILES
        ],
    }
