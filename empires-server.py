import os
import webbrowser
from pathlib import Path

import msgspec
from flask_session.base import MsgSpecSerializer

from quest_settings import quest_titles

os.environ["PBR_VERSION"] = '5.4.3'
if not os.environ.get('EDITOR'):
    os.environ["EDITOR"] = 'notepad'  # system specific!

import editor
from tendo import singleton
import init_settings as settings


if __name__ == '__main__':
    settings.read_ops()

if not settings.debug and os.environ.get("ALLOW_MULTI_INSTANCE") != "1":
    me = singleton.SingleInstance()

if os.name == 'nt':
    os.system('color')

from save_engine import db, save_database_uri, configure_database_engine, has_any_saves, count_sessions, \
    log_path, lookup_objects_save_by_position, get_all_sessions, \
    store_session, validate_save, InvalidSaveException, set_crash_log, my_games_path, install_path, base_path, \
    get_dict, get_saves
from save_migration import migrate, is_0_08a_preview
from builtins import print
from time import sleep
from datetime import timedelta

from flask import Flask, render_template, send_from_directory, request, Response, make_response, redirect
try:
    from flask import safe_join # flask 2.1.0 : safe_join is removed, use werkzeug.utils.safe_join instead."
except ImportError:
    from werkzeug.utils import safe_join
from flask_session import Session
from flask.sessions import SessionInterface, SecureCookieSession
from pyamf import remoting
import pyamf

import mod_engine
import swf_patch
import auth_engine
from battle_engine import battle_complete_response, spawn_fleet, next_campaign_response, assign_consumable_response, \
    get_active_island_by_map, set_active_island_by_map, format_player_fleet, \
    cancel_unstarted_invasions, register_fleetname_fleet, get_last_fleet_name, is_shielded, decode_unit_count_list, \
    encode_unit_strings, get_survival_player_fleet, encode_unit_string, BattleContext, StaleBattleReference
from game_settings import get_zid, initial_island, random_image, randomReward, get_sessions_id, unlock_expansion, \
    lookup_wave, lookup_crew_template
import threading #, webbrowaser
import pyamf.amf0
import json
import xml.etree.ElementTree as ET
from quest_engine import *
from state_machine import *
from logger import socketio, report_tutorial_step, report_world_log, report_other_log
import copy
import random

try:
    from flask_compress import Compress
except ImportError as error:
    print("Warning: compression can't be initialized. Compression is disabled.", error)
    compression = False

# import logging.config

version = "0.08a.2026_11_03"
release_date = 'Tuesday, 3 November 2026'

COMPRESS_MIMETYPES = ['text/html', 'text/css', 'text/xml', 'application/json', 'application/javascript',
                      'application/x-amf']
COMPRESS_LEVEL = 6
COMPRESS_MIN_SIZE = 500

# starting seeds
rand_seed_w = 5445  # very random
rand_seed_z = 844

compress = Compress() if settings.compression else None
sess = Session()

# Requests for static game assets never use the save game, but a server side session is loaded from and
# written back to the database on every request. With a save of several MB that makes each image/swf
# request take up to a second and they queue up behind each other.
ASSET_PATH_PREFIXES = ("/img/", "/js/", "/css/", "/nullassets/", "/assets/", "/127.0.0.1", "/gameSettings.xml",
                       "/changelog.txt", "/favicon.ico", "/files/empire-s.assets.zgncdn.com/assets/109338/ZGame",
                       # Since BASE_PATH became absolute these arrive at the root rather
                       # than under "/127.0.0.1...". They are assets and must skip the
                       # session like the rest: the game posts record_stats constantly,
                       # and letting each one load the save and write it back loses
                       # whatever a gateway call saved in the meantime.
                       "/record_stats.php", "/en_US.xml", "/questSettings.xml")


class AssetSkippingSessionInterface(SessionInterface):
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def open_session(self, app, request):
        if request.path.startswith(ASSET_PATH_PREFIXES):
            return SecureCookieSession()
        return self.inner.open_session(app, request)

    def save_session(self, app, session, response):
        if request.path.startswith(ASSET_PATH_PREFIXES):
            return
        # Do not let Flask-Session's refresh cookie overwrite explicit login/logout/switch cookies.
        cookie_prefix = f"{app.config.get('SESSION_COOKIE_NAME', 'session')}="
        if any(h.startswith(cookie_prefix) for h in response.headers.getlist("Set-Cookie")):
            return
        self.inner.save_session(app, session, response)


start = datetime.now()
app_base_path = base_path()
app: Flask = Flask(__name__, root_path=app_base_path)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "raisetheempires-shared-secret")

print("Root folder", app.root_path)
print("Instance folder", app.instance_path)
app.config['SESSION_TYPE'] = 'sqlalchemy'
_db_uri = save_database_uri(app.root_path, app.instance_path)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_uri
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = (
    {"pool_pre_ping": True}
    if _db_uri.startswith("sqlite:")
    else {"pool_pre_ping": True, "pool_size": 10, "max_overflow": 20}
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False # Flask-SQLAlchemy has its own event notification system that gets layered on top of SQLAlchemy. To do this, it tracks modifications to the SQLAlchemy session. This option disables the modification tracking system.
app.config['SESSION_SQLALCHEMY'] = db
app.config['SESSION_COOKIE_HTTPONLY'] = False
#app.config['SESSION_SERIALIZATION_FORMAT '] = 'json' # flask-session uses msgspec by default now which makes it undepickleable
app.config['MAX_FORM_MEMORY_SIZE'] = 5 * 1024 * 1024  # 5 MB
app.permanent_session_lifetime = timedelta(weeks=520)

MIN_ADMIN_ID = -1
STEELE_ID = -1
MAX_ADMIN_ID = 123

@app.route("/")
def index():
    # The game is the home page now. With no save loaded but saves on disk, go
    # to the picker first, otherwise opening "/" would quietly start a new one.
    if 'user_object' not in session and 'new_world_name' not in session and has_any_saves():
        return make_response(redirect('/login.html'))
    return ruffle()


def _current_account():
    aid = session.get("account_id")
    if aid:
        acc = db.session.get(auth_engine.Account, aid)
        if acc is not None:
            return acc
    sid = getattr(session, "sid", None)
    if sid:
        return auth_engine.Account.query.filter_by(
            session_id=auth_engine.SESSION_KEY_PREFIX + sid
        ).first()
    return None


ADMIN_EXACT_PATHS = {
    "/admin",
    "/admin/shutdown",
    "/admin/shutdown/cancel",
    "/admin/drop-gift",
    "/save-editor",
    "/list_session",
    "/wipe_session",
    "/gazillionaire",
    "/deprogress",
    "/language_editor",
}
ADMIN_PREFIXES = (
    "/admin/users",
    "/admin/tickets",
    "/seed/",
    "/patch/",
    "/unlock_quest/",
    "/complete_quest/",
    "/remove_quest/",
)


@app.before_request
def _protect_admin_routes():
    path = request.path
    if path in ADMIN_EXACT_PATHS or path.startswith(ADMIN_PREFIXES):
        acc = _current_account()
        if acc is None:
            if request.is_json or path.endswith(".json") or request.method == "POST":
                return Response(json.dumps({"error": "Authentication required"}), status=401, mimetype="application/json")
            return make_response(redirect("/login.html"))
        if not acc.is_admin or acc.is_suspended:
            if request.is_json or path.endswith(".json") or request.method == "POST":
                return Response(json.dumps({"error": "Admin access required"}), status=403, mimetype="application/json")
            return ("Forbidden: admin access required.", 403)


def game_page_context():
    """Template values shared by every page that embeds the game."""
    saves = get_saves()
    resources = get_dict(session, "user_object", "userInfo", "world", "resources")
    player = get_dict(session, "user_object", "userInfo", "player")
    acc = _current_account()
    return dict(time=datetime.now().timestamp(), zid=str(get_zid()),
                version=version,
                allies=json.dumps(get_allies_friend(saves),
                                  default=lambda o: '<not serializable>', sort_keys=False, indent=2),
                app_friends=json.dumps(get_allies_id(saves)),
                worldname=session['user_object']["userInfo"]["worldName"] if 'user_object' in session else "Emperor",
                resources=resources,
                cash=player.get("cash"),
                energy=player.get("energy", resources.get("energy")),
                is_admin=bool(acc and acc.is_admin),
                picture=get_avatar_pic(),
                dropdown_items=get_sessions_dropdown_info(saves),
                motd=get_motd())


@app.route("/ruffle.html")
def ruffle():
    account_id = session.get('account_id')
    if account_id:
        acc = db.session.get(auth_engine.Account, account_id)
        if acc is not None and acc.is_suspended:
            resp = make_response(redirect('/login.html?suspended=1'))
            resp.delete_cookie(app.config.get("SESSION_COOKIE_NAME", "session"))
            return resp
    if not validate_save(session, True):
        print("Invalid save game")
        return make_response(redirect('/save-editor'))
    return render_template("ruffle.html", **game_page_context())

def get_allies_friend(saves):
    return [ally["friend"] for ally in allies.values()
            if "friend" in ally and ally["friend"] and ally["neighbor"]] + get_sessions_friends(saves)


def get_suggested_friends():
    return [ally["friend"] for ally in allies.values()
            if "friend" in ally and ally["friend"] and not ally["neighbor"]]


def get_allies_id(saves):
    return [ally["appFriendId"] for ally in allies.values()
            if "appFriendId" in ally and ally["appFriendId"] is not None] + get_sessions_id(saves)


def get_allies_info():
    return [ally["info"] for ally in allies.values() if ally["info"] and ally.get("neighbor")]+ get_sessions_info(
        get_saves())


@app.route("/wipe_session", methods=['GET', 'POST'])
def wipe_session():
    session.clear()
    response = make_response(redirect('/login.html'))
    # response.set_cookie('session', '', expires=0)
    return response


# @app.route("/switch-session/session-id/<session_id>", methods=['GET', 'POST'])
# def switch_session(session_id):
#     response = make_response(redirect('/'))
#     #session.sid = session_id.split(":")[1]
#     #response.set_cookie('sessions3', session_id.split(":")[1], max_age=60*60*24*365*2)
#     return response


@app.route("/list_session", methods=['GET', 'POST'])
def list_session():
    response = get_sessions_dropdown_info(get_all_sessions())

    dump = json.dumps(response,
                      default=lambda o: '<not serializable>', sort_keys=False, indent=2)
    return dump


def get_sessions_dropdown_info(saves):
    if saves:
        response = [{
            "session_id": save['session_id'],
            "uid": save['user_object']["userInfo"]["player"]["uid"],
            "world_name": save['user_object']["userInfo"]["worldName"],
            "level": save['user_object']["userInfo"]["player"]["level"],
            "xp": save['user_object']["userInfo"]["player"]["xp"],
            "profilePic": "img/avatars/" + (save['profilePic'] if 'profilePic' in save and save['profilePic'] is not None else "blank.png"),
        } for save in saves if validate_save(save)]
    else:
        response = []
    return response

def get_sessions_info(saves):
    from game_settings import other_unique_saves
    filtered = other_unique_saves(saves)
    if filtered:
        response = [{
            "uid": save['user_object']["userInfo"]["player"]["uid"],
            "resource": save['user_object']["userInfo"]["player"]["playerResourceType"],
            "coins": save['user_object']["userInfo"]["world"]['resources']["coins"],
            "xp": save['user_object']["userInfo"]["player"]["xp"],
            "level": save['user_object']["userInfo"]["player"]["level"],
            "socialXpGood": save['user_object']["userInfo"]["player"]["socialXpGood"],
            "socialLevelGood": save['user_object']["userInfo"]["player"]["socialLevelGood"],
            "socialXpBad": save['user_object']["userInfo"]["player"]["socialXpBad"],
            "socialLevelBad": save['user_object']["userInfo"]["player"]["socialLevelBad"],
            "profilePic": "img/avatars/" + (save['profilePic'] if 'profilePic' in save and save['profilePic'] is not None else "blank.png"),
            "dominanceRank": 1,
            "pvpNumOccupiers": len([k for k, v in save['user_object']["pvp"]["invaders"].items() if k != "pve"]),
            "pvpNumOccupiersNotDefended": len([k for k, v in save['user_object']["pvp"]["invaders"].items() if k != "pve"]),
            "pvpInfo": next((v for k, v in save['user_object']["pvp"]["invaders"].items() if k == "u" + str(get_zid())), None),
            "tending": {
                "actions": 3
            }
        } for save in filtered]
    else:
        response = []
    return response


@app.route("/gazillionaire", methods=['GET', 'POST'])
def more_money():
    if 'user_object' in session:
        create_backup("gazillionaire cheat")
        player = session['user_object']["userInfo"]["player"]
        player['cash'] += 10000
        session['saved'] = str(session.get('saved', "")) +  "gazillionaire"
        response = make_response(redirect('/'))
        return response
    else:
        return ("Nope! You don't have a game session yet", 403)


@app.route("/deprogress", methods=['GET', 'POST'])
def deprogress_battle_map():
    if 'user_object' in session:
        # player = session['user_object']["userInfo"]["player"]
        campaign = session['user_object']['userInfo']['world']['campaign']
        # if map_name not in campaign['active'].keys():
        #     campaign['active'][map_name] = {"status": 0, "fleets": []}
        list = sorted(campaign['active'].keys())

        map, island = get_active_island_by_map(list[-1])

        create_backup("before deprogress battle map " + str(island+1) + "=>" + str(island))
        session['saved'] = str(session.get('saved', "")) + "deprogress"
        if island <= 0:
            del campaign['active'][map]
            deprogress_battle_map()
        else:
            set_active_island_by_map(map, island - 1)
        response = make_response(redirect('/'))
        return response
    else:
        return ("Nope! You don't have a game session yet", 403)


@app.route("/seed/w/<int:w>/z/<int:z>", methods=['GET', 'POST'])
def change_seed(w, z):
    if 'user_object' in session:
        create_backup("seed")
        world = session['user_object']["userInfo"]["world"]
        prev_seed = str(world["randSeedW"]) + ', ' + str(world["randSeedZ"])
        world["randSeedW"] = w
        world["randSeedZ"] = z
        print("Seed change", prev_seed, "=>", str(world["randSeedW"]) + ', ' + str(world["randSeedZ"]))

        session['saved'] = str(session.get('saved', "")) +  "seed"
        response = make_response(redirect('/'))
        return response
    else:
        return ("Nope! You don't have a game session yet", 403)


@app.route("/patch/<path:path>/empty-dict", methods=['GET', 'POST'])
def patch_user_empty_dict(path):
    return patch_user(path, {})


@app.route("/patch/<path:path>/empty-list", methods=['GET', 'POST'])
def patch_user_empty_list(path):
    return patch_user(path, [])


@app.route("/patch/<path:path>/none", methods=['GET', 'POST'])
def patch_user_dict(path):
    return patch_user(path, None)


@app.route("/patch/<path:path>/int/<int:value>", methods=['GET', 'POST'])
@app.route("/patch/<path:path>/string/<value>", methods=['GET', 'POST'])
def patch_user(path, value):
    if 'user_object' in session:
        if path.split('/')[0] not in ("saved", "original_save_version"):
            create_backup("patch")
            dictionary = session
            for p in path.split('/')[:-1]:
                dictionary = dictionary[p]

            dictionary[path.split('/')[-1]] = value

            session['saved'] = str(session.get('saved', "")) + "patch"
            response = make_response(redirect('/'))
            return response
        else:
            return ("Nope! Disallowed patch root", 403)
    else:
        return ("Nope! You don't have a game session yet", 403)


@app.route("/patch/<path:path>/list/index/<int:i>/int/<int:value>", methods=['GET', 'POST'])
@app.route("/patch/<path:path>/list/index/<int:i>/string/<value>", methods=['GET', 'POST'])
def patch_user_list(path, i, value):
    if 'user_object' in session:
        if path.split('/')[0] not in ("saved", "original_save_version"):
            create_backup("patch")
            dictionary = session
            for p in path.split('/'):
                dictionary = dictionary[p]

            dictionary[i] = value

            session['saved'] = str(session.get('saved', "")) + "patch"
            response = make_response(redirect('/'))
            return response
        else:
            return ("Nope! Disallowed patch root", 403)
    else:
        return ("Nope! You don't have a game session yet", 403)


@app.route("/unlock_quest/<value>", methods=['GET', 'POST'])
def unlock_quest(value):
    if 'user_object' in session:
        create_backup("Unlocked quest " + value)
        new_quests=[]
        meta = {}
        new_quest_with_sequels(value, new_quests, meta, force=True)
        merge_quest_progress(new_quests, session['quests'], "session quest")
        session['saved'] = str(session.get('saved', "")) + "unlock"

        response = make_response(redirect('/'))
        return response
    else:
        return ("Nope! You don't have a game session yet", 403)


@app.route("/complete_quest/<value>", methods=['GET', 'POST'])
def complete_quest(value):
    if 'user_object' in session:
        create_backup("Complete quest " + value)
        new_quests=[]
        meta = {}
        handle_quest_progress(meta, progress_quest(value))
        session['saved'] = str(session.get('saved', "")) + "complete"

        response = make_response(redirect('/'))
        return response
    else:
        return ("Nope! You don't have a game session yet", 403)


@app.route("/remove_quest/<value>", methods=['GET', 'POST'])
def remove_quest(value):
    if 'user_object' in session:
        create_backup("Remove quest " + value)
        session['quests'] = [e for e in session['quests'] if e["name"] != value]
        session['saved'] = str(session.get('saved', "")) + "remove"

        response = make_response(redirect('/'))
        return response
    else:
        return ("Nope! You don't have a game session yet", 403)


def admin_quest_buckets():
    """Split every known quest into not-started, in-progress and completed."""
    quests = session.get('quests') or []
    incomplete = [e["name"] for e in quests if not e.get("complete")]
    complete = [e["name"] for e in quests if e.get("complete")]
    return (
        {q: v for q, v in quest_titles.items() if q not in complete and q not in incomplete},
        {q: v for q, v in quest_titles.items() if q in incomplete},
        {q: v for q, v in quest_titles.items() if q in complete},
    )


_shutdown_at = None
_shutdown_timer = None


def _finish_shutdown():
    print("Shutdown countdown finished. Stopping server...")
    os._exit(0)


def schedule_shutdown(seconds=30):
    global _shutdown_at, _shutdown_timer
    if _shutdown_timer is not None:
        _shutdown_timer.cancel()
    _shutdown_at = datetime.now().timestamp() + seconds
    _shutdown_timer = threading.Timer(seconds, _finish_shutdown)
    _shutdown_timer.daemon = True
    _shutdown_timer.start()
    print(f"WARNING: Server shutting down in {seconds}s (press Ctrl+C again to force quit).")


def cancel_shutdown():
    global _shutdown_at, _shutdown_timer
    if _shutdown_timer is not None:
        _shutdown_timer.cancel()
        _shutdown_timer = None
    _shutdown_at = None
    print("Shutdown cancelled.")


def _handle_shutdown_signal(signum, _frame):
    if _shutdown_at is not None:
        print("Force shutdown requested.")
        os._exit(0)
    schedule_shutdown(30)


def admin_status():
    """A small snapshot of the current session, for the admin page to poll."""
    user_object = session.get('user_object')
    player = get_dict(session, "user_object", "userInfo", "player")
    world = get_dict(session, "user_object", "userInfo", "world")
    quests = session.get('quests') or []
    resources = get_dict(session, "user_object", "userInfo", "world", "resources")
    return {
        "has_session": user_object is not None,
        "save_valid": validate_save(session, False),
        "world_name": get_dict(session, "user_object", "userInfo").get("worldName"),
        "uid": player.get("uid"),
        "level": player.get("level"),
        "xp": player.get("xp"),
        "coins": resources.get("coins"),
        "cash": player.get("cash"),
        "energy": player.get("energy", resources.get("energy")),
        "wood": resources.get("wood"),
        "iron": resources.get("iron"),
        "oil": resources.get("oil"),
        "aluminum": resources.get("aluminum"),
        "copper": resources.get("copper"),
        "gold": resources.get("gold"),
        "uranium": resources.get("uranium"),
        "population": session.get('population'),
        "quests_complete": len([e for e in quests if e.get("complete")]),
        "quests_incomplete": len([e for e in quests if not e.get("complete")]),
        "save_version": session.get('save_version'),
        "save_count": count_sessions(),
        "shutdown_in": max(0, int(round(_shutdown_at - datetime.now().timestamp()))) if _shutdown_at else None,
    }


@app.route("/admin")
def admin_page():
    # Deliberately unauthenticated for now: it only exposes the debug routes
    # that already answer without a login, and the banner says so.
    quest_names, incomplete_quest_names, complete_quest_names = admin_quest_buckets()
    return render_template(
        "admin.html",
        version=version,
        release_date=release_date,
        listen_host=settings.host,
        listen_port=settings.port,
        swf_base=request.host_url,
        debug_mode=settings.debug,
        compression=settings.compression,
        caching=settings.caching,
        saves=get_sessions_dropdown_info(get_saves()),
        quest_names=quest_names,
        incomplete_quest_names=incomplete_quest_names,
        complete_quest_names=complete_quest_names,
    )


@app.route("/admin/status.json")
def admin_status_json():
    return Response(json.dumps(admin_status(), default=lambda o: '<not serializable>'),
                    mimetype="application/json")


@app.route("/admin/shutdown", methods=['POST'])
def admin_shutdown():
    data = request.get_json(silent=True) or {}
    secs = max(5, min(300, int(data.get("seconds") or 30)))
    schedule_shutdown(secs)
    return Response(json.dumps({"ok": True, "shutdown_in": secs}), mimetype="application/json")


@app.route("/admin/shutdown/cancel", methods=['POST'])
def admin_shutdown_cancel():
    cancel_shutdown()
    return Response(json.dumps({"ok": True}), mimetype="application/json")


def _drop_summary(drop):
    parts = []
    for label, val in (("Cash", drop.cash), ("Coins", drop.coins), ("Wood", drop.wood),
                       ("Oil", drop.oil), ("Iron", drop.iron), ("Energy", drop.energy)):
        if val > 0:
            parts.append(f"+{val} {label}")
    if drop.item_code and drop.item_count > 0:
        parts.append(f"+{drop.item_count}x {drop.item_code}")
    return ", ".join(parts) or "Gift"


@app.route("/chat/messages.json")
def chat_messages_json():
    acc = _current_account()
    if acc is None or acc.is_suspended:
        return Response(json.dumps({"error": "Login required"}), status=401, mimetype="application/json")
    msgs = auth_engine.ChatMessage.query.order_by(auth_engine.ChatMessage.id.desc()).limit(50).all()
    msgs.reverse()
    drop_ids = [m.gift_drop_id for m in msgs if m.gift_drop_id]
    drops_by_id = {}
    claims_count = {}
    my_claims = set()
    if drop_ids:
        for d in auth_engine.GiftDrop.query.filter(auth_engine.GiftDrop.id.in_(drop_ids)).all():
            drops_by_id[d.id] = d
            claims_count[d.id] = auth_engine.GiftClaim.query.filter_by(drop_id=d.id).count()
        my_claims = {
            c.drop_id for c in auth_engine.GiftClaim.query.filter(
                auth_engine.GiftClaim.drop_id.in_(drop_ids),
                auth_engine.GiftClaim.account_id == acc.id
            ).all()
        }
    out = []
    for m in msgs:
        item = {
            "id": m.id,
            "sender_name": m.sender_name,
            "sender_role": m.sender_role,
            "message": m.message,
            "time": m.created_at.strftime("%H:%M") if m.created_at else "",
            "drop": None,
        }
        if m.gift_drop_id and m.gift_drop_id in drops_by_id:
            d = drops_by_id[m.gift_drop_id]
            cnt = claims_count.get(d.id, 0)
            item["drop"] = {
                "id": d.id,
                "title": d.title,
                "summary": _drop_summary(d),
                "claims_count": cnt,
                "max_claims": d.max_claims,
                "already_claimed": d.id in my_claims,
                "remaining": max(0, d.max_claims - cnt),
            }
        out.append(item)
    return Response(json.dumps({"messages": out}), mimetype="application/json")


@app.route("/chat/send", methods=['POST'])
def chat_send():
    acc = _current_account()
    if acc is None or acc.is_suspended:
        return Response(json.dumps({"error": "Login required"}), status=401, mimetype="application/json")
    data = request.get_json(silent=True) or {}
    text = (data.get("message") or "").strip()
    if not text:
        return Response(json.dumps({"error": "Message empty"}), status=400, mimetype="application/json")
    sender_name = (get_dict(session, "user_object", "userInfo").get("worldName")
                   if session.get("user_object") else None) or acc.island_name or acc.username
    msg = auth_engine.ChatMessage(
        account_id=acc.id,
        sender_name=sender_name[:40],
        sender_role=acc.role,
        message=text[:300],
    )
    db.session.add(msg)
    db.session.commit()
    return Response(json.dumps({"ok": True, "id": msg.id}), mimetype="application/json")


@app.route("/admin/drop-gift", methods=['POST'])
def admin_drop_gift():
    acc = _current_account()
    data = request.get_json(silent=True) or {}
    cash = max(0, int(data.get("cash") or 0))
    coins = max(0, int(data.get("coins") or 0))
    wood = max(0, int(data.get("wood") or 0))
    oil = max(0, int(data.get("oil") or 0))
    iron = max(0, int(data.get("iron") or 0))
    energy = max(0, int(data.get("energy") or 0))
    item_code = (data.get("item_code") or "").strip()[:32]
    item_count = max(0, int(data.get("item_count") or 0)) if item_code else 0
    max_claims = max(1, min(1000, int(data.get("max_claims") or 10)))

    if not any([cash, coins, wood, oil, iron, energy, item_count]):
        return Response(json.dumps({"error": "Specify at least one reward"}), status=400, mimetype="application/json")

    note = (data.get("note") or "").strip()
    drop = auth_engine.GiftDrop(
        title=note[:120] if note else f"Gift Drop ({max_claims} orang pertama)",
        cash=cash, coins=coins, wood=wood, oil=oil, iron=iron, energy=energy,
        item_code=item_code, item_count=item_count,
        max_claims=max_claims,
        created_by=acc.username if acc else "admin",
    )
    db.session.add(drop)
    db.session.flush()

    msg = auth_engine.ChatMessage(
        account_id=acc.id,
        sender_name=acc.island_name or acc.username,
        sender_role="admin",
        message=f"[GIFT DROP] {_drop_summary(drop)} — {max_claims} orang pertama!",
        gift_drop_id=drop.id,
    )
    db.session.add(msg)
    db.session.commit()
    return Response(json.dumps({"ok": True, "drop_id": drop.id}), mimetype="application/json")


@app.route("/chat/claim/<int:drop_id>", methods=['POST'])
def chat_claim_drop(drop_id):
    acc = _current_account()
    if acc is None or acc.is_suspended:
        return Response(json.dumps({"error": "Login required"}), status=401, mimetype="application/json")
    if not session.get("user_object"):
        return Response(json.dumps({"error": "Load game first before claiming"}), status=400, mimetype="application/json")

    drop = auth_engine.GiftDrop.query.filter_by(id=drop_id).with_for_update().first()
    if drop is None:
        return Response(json.dumps({"error": "Gift drop not found"}), status=404, mimetype="application/json")

    if auth_engine.GiftClaim.query.filter_by(drop_id=drop.id, account_id=acc.id).first() is not None:
        return Response(json.dumps({"error": "Kamu sudah claim gift ini"}), status=400, mimetype="application/json")

    cnt = auth_engine.GiftClaim.query.filter_by(drop_id=drop.id).count()
    if cnt >= drop.max_claims:
        return Response(json.dumps({"error": "Kuota gift sudah habis"}), status=400, mimetype="application/json")

    player = session["user_object"]["userInfo"]["player"]
    res = session["user_object"]["userInfo"]["world"]["resources"]
    if drop.cash:
        player["cash"] = int(player.get("cash", 0)) + drop.cash
    if drop.energy:
        player["energy"] = int(player.get("energy", 0)) + drop.energy
    for k, v in (("coins", drop.coins), ("wood", drop.wood), ("oil", drop.oil), ("iron", drop.iron)):
        if v:
            res[k] = int(res.get(k, 0)) + v
    if drop.item_code and drop.item_count > 0:
        inv = player.setdefault("inventory", {}).setdefault("items", {})
        inv[drop.item_code] = int(inv.get(drop.item_code, 0)) + drop.item_count

    db.session.add(auth_engine.GiftClaim(drop_id=drop.id, account_id=acc.id))
    db.session.commit()
    return Response(json.dumps({
        "ok": True,
        "summary": _drop_summary(drop),
        "claims_count": cnt + 1,
        "max_claims": drop.max_claims,
    }), mimetype="application/json")


def _serialize_ticket(t):
    return {
        "id": t.id,
        "account_id": t.account_id,
        "username": t.username,
        "island_name": t.island_name,
        "subject": t.subject,
        "message": t.message,
        "status": t.status,
        "admin_reply": t.admin_reply or "",
        "created_at": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "",
    }


@app.route("/support/tickets.json")
def support_tickets_json():
    acc = _current_account()
    if acc is None or acc.is_suspended:
        return Response(json.dumps({"error": "Login required"}), status=401, mimetype="application/json")
    rows = auth_engine.SupportTicket.query.filter_by(account_id=acc.id).order_by(
        auth_engine.SupportTicket.id.desc()
    ).limit(20).all()
    return Response(json.dumps({"tickets": [_serialize_ticket(t) for t in rows]}), mimetype="application/json")


@app.route("/support/tickets", methods=['POST'])
def support_ticket_create():
    acc = _current_account()
    if acc is None or acc.is_suspended:
        return Response(json.dumps({"error": "Login required"}), status=401, mimetype="application/json")
    data = request.get_json(silent=True) or {}
    subject = (data.get("subject") or "").strip()[:120]
    message = (data.get("message") or "").strip()[:2000]
    if not subject or not message:
        return Response(json.dumps({"error": "Subject and message are required"}), status=400, mimetype="application/json")
    island_name = (get_dict(session, "user_object", "userInfo").get("worldName")
                   if session.get("user_object") else None) or acc.island_name or acc.username
    t = auth_engine.SupportTicket(
        account_id=acc.id,
        username=acc.username,
        island_name=island_name[:40],
        subject=subject,
        message=message,
    )
    db.session.add(t)
    db.session.commit()
    return Response(json.dumps({"ok": True, "ticket": _serialize_ticket(t)}), mimetype="application/json")


@app.route("/admin/tickets.json")
def admin_tickets_json():
    rows = auth_engine.SupportTicket.query.order_by(auth_engine.SupportTicket.id.desc()).limit(100).all()
    return Response(json.dumps({"tickets": [_serialize_ticket(t) for t in rows]}), mimetype="application/json")


@app.route("/admin/tickets/<int:ticket_id>/reply", methods=['POST'])
def admin_ticket_reply(ticket_id):
    t = db.session.get(auth_engine.SupportTicket, ticket_id)
    if t is None:
        return Response(json.dumps({"error": "Ticket not found"}), status=404, mimetype="application/json")
    data = request.get_json(silent=True) or {}
    reply = (data.get("reply") or "").strip()[:2000]
    status = (data.get("status") or ("replied" if reply else "closed")).strip().lower()
    if status not in ("open", "replied", "closed"):
        return Response(json.dumps({"error": "Invalid status"}), status=400, mimetype="application/json")
    if reply:
        t.admin_reply = reply
    t.status = status
    db.session.commit()
    return Response(json.dumps({"ok": True, "ticket": _serialize_ticket(t)}), mimetype="application/json")


@app.route("/friends/list.json")
def friends_list_json():
    acc = _current_account()
    if acc is None or acc.is_suspended:
        return Response(json.dumps({"error": "Login required"}), status=401, mimetype="application/json")
    rows = auth_engine.Friendship.query.filter(
        (auth_engine.Friendship.requester_id == acc.id) | (auth_engine.Friendship.addressee_id == acc.id)
    ).order_by(auth_engine.Friendship.id.desc()).all()
    other_ids = {r.addressee_id if r.requester_id == acc.id else r.requester_id for r in rows}
    accounts_by_id = {
        a.id: a for a in auth_engine.Account.query.filter(auth_engine.Account.id.in_(other_ids)).all()
    } if other_ids else {}

    friends, incoming, outgoing = [], [], []
    for r in rows:
        other_id = r.addressee_id if r.requester_id == acc.id else r.requester_id
        other = accounts_by_id.get(other_id)
        if not other:
            continue
        item = {"id": r.id, "account_id": other.id, "username": other.username, "island_name": other.island_name}
        if r.status == "accepted":
            friends.append(item)
        elif r.addressee_id == acc.id:
            incoming.append(item)
        else:
            outgoing.append(item)
    return Response(json.dumps({"friends": friends, "incoming": incoming, "outgoing": outgoing}), mimetype="application/json")


@app.route("/friends/request", methods=['POST'])
def friends_request_create():
    acc = _current_account()
    if acc is None or acc.is_suspended:
        return Response(json.dumps({"error": "Login required"}), status=401, mimetype="application/json")
    data = request.get_json(silent=True) or {}
    target_name = auth_engine.normalise_username(data.get("username"))
    if not target_name:
        return Response(json.dumps({"error": "Masukkan username"}), status=400, mimetype="application/json")
    target = auth_engine.find_account(target_name)
    if target is None:
        return Response(json.dumps({"error": "User tidak ditemukan"}), status=404, mimetype="application/json")
    if target.id == acc.id:
        return Response(json.dumps({"error": "Tidak bisa add diri sendiri"}), status=400, mimetype="application/json")

    existing = auth_engine.Friendship.query.filter(
        ((auth_engine.Friendship.requester_id == acc.id) & (auth_engine.Friendship.addressee_id == target.id)) |
        ((auth_engine.Friendship.requester_id == target.id) & (auth_engine.Friendship.addressee_id == acc.id))
    ).first()
    if existing is not None:
        if existing.status == "accepted":
            return Response(json.dumps({"error": "Sudah berteman"}), status=400, mimetype="application/json")
        if existing.addressee_id == acc.id:
            existing.status = "accepted"
            db.session.commit()
            return Response(json.dumps({"ok": True, "auto_accepted": True}), mimetype="application/json")
        return Response(json.dumps({"error": "Friend request sudah dikirim"}), status=400, mimetype="application/json")

    fr = auth_engine.Friendship(requester_id=acc.id, addressee_id=target.id, status="pending")
    db.session.add(fr)
    db.session.commit()
    return Response(json.dumps({"ok": True, "id": fr.id}), mimetype="application/json")


@app.route("/friends/<int:req_id>/accept", methods=['POST'])
def friends_request_accept(req_id):
    acc = _current_account()
    if acc is None or acc.is_suspended:
        return Response(json.dumps({"error": "Login required"}), status=401, mimetype="application/json")
    fr = db.session.get(auth_engine.Friendship, req_id)
    if fr is None or fr.addressee_id != acc.id:
        return Response(json.dumps({"error": "Request not found"}), status=404, mimetype="application/json")
    fr.status = "accepted"
    db.session.commit()
    return Response(json.dumps({"ok": True}), mimetype="application/json")


@app.route("/friends/<int:req_id>/remove", methods=['POST'])
def friends_request_remove(req_id):
    acc = _current_account()
    if acc is None or acc.is_suspended:
        return Response(json.dumps({"error": "Login required"}), status=401, mimetype="application/json")
    fr = db.session.get(auth_engine.Friendship, req_id)
    if fr is None or (fr.requester_id != acc.id and fr.addressee_id != acc.id):
        return Response(json.dumps({"error": "Not found"}), status=404, mimetype="application/json")
    db.session.delete(fr)
    db.session.commit()
    return Response(json.dumps({"ok": True}), mimetype="application/json")


def _find_save_for_account(account):
    """Return the decoded save dict for an Account, or None."""
    if not account or not account.session_id:
        return None
    for save in get_saves():
        if save.get("session_id") == account.session_id:
            return save
    return None


def _sync_active_session_if_matching(save):
    """If the admin's browser session holds this save, update it in place too."""
    if session.get("user_object") and ("session:" + getattr(session, "sid", "")) == save.get("session_id"):
        session["user_object"] = save["user_object"]


@app.route("/admin/users.json")
def admin_users_json():
    saves_by_sid = {s.get("session_id"): s for s in get_saves()}
    accounts = auth_engine.Account.query.order_by(auth_engine.Account.id.asc()).all()
    rows = []
    for acc in accounts:
        save = saves_by_sid.get(acc.session_id)
        player = get_dict(save, "user_object", "userInfo", "player") if save else {}
        res = get_dict(save, "user_object", "userInfo", "world", "resources") if save else {}
        world_name = (get_dict(save, "user_object", "userInfo").get("worldName")
                      if save else None) or acc.island_name
        rows.append({
            "id": acc.id,
            "username": acc.username,
            "island_name": world_name,
            "session_id": acc.session_id,
            "role": acc.role,
            "is_suspended": bool(acc.is_suspended),
            "created_at": acc.created_at.strftime("%Y-%m-%d %H:%M") if acc.created_at else "-",
            "has_save": save is not None,
            "uid": player.get("uid"),
            "level": player.get("level"),
            "xp": player.get("xp"),
            "cash": player.get("cash"),
            "coins": res.get("coins"),
            "wood": res.get("wood"),
            "oil": res.get("oil"),
            "iron": res.get("iron"),
            "energy": player.get("energy"),
            "energyMax": player.get("energyMax"),
        })
    return Response(json.dumps({"users": rows}, default=lambda o: '<not serializable>'),
                    mimetype="application/json")


@app.route("/admin/users/<int:account_id>/suspend", methods=['POST'])
def admin_user_suspend(account_id):
    acc = db.session.get(auth_engine.Account, account_id)
    if acc is None:
        return Response(json.dumps({"error": "User not found"}), status=404, mimetype="application/json")
    data = request.get_json(silent=True) or {}
    if "suspended" in data:
        acc.is_suspended = bool(data["suspended"])
    else:
        acc.is_suspended = not bool(acc.is_suspended)
    db.session.commit()
    return Response(json.dumps({"ok": True, "is_suspended": acc.is_suspended}), mimetype="application/json")


@app.route("/admin/users/<int:account_id>/role", methods=['POST'])
def admin_user_role(account_id):
    acc = db.session.get(auth_engine.Account, account_id)
    if acc is None:
        return Response(json.dumps({"error": "User not found"}), status=404, mimetype="application/json")
    data = request.get_json(silent=True) or {}
    new_role = (data.get("role") or "").strip().lower()
    if new_role not in ("admin", "player"):
        return Response(json.dumps({"error": "Invalid role"}), status=400, mimetype="application/json")
    me = _current_account()
    if me is not None and me.id == acc.id and new_role != "admin":
        return Response(json.dumps({"error": "Cannot demote yourself"}), status=400, mimetype="application/json")
    acc.role = new_role
    db.session.commit()
    return Response(json.dumps({"ok": True, "role": acc.role}), mimetype="application/json")


@app.route("/admin/users/<int:account_id>/edit", methods=['POST'])
def admin_user_edit(account_id):
    acc = db.session.get(auth_engine.Account, account_id)
    if acc is None:
        return Response(json.dumps({"error": "User not found"}), status=404, mimetype="application/json")
    data = request.get_json(silent=True) or {}
    island_name = (data.get("island_name") or "").strip()
    new_password = data.get("password") or ""

    if island_name:
        acc.island_name = island_name[:auth_engine.MAX_ISLAND_NAME_LENGTH]
    if new_password:
        if len(new_password) < auth_engine.MIN_PASSWORD_LENGTH:
            return Response(json.dumps({"error": f"Password min {auth_engine.MIN_PASSWORD_LENGTH} chars"}),
                            status=400, mimetype="application/json")
        acc.set_password(new_password)

    save = _find_save_for_account(acc)
    if save is not None:
        if island_name:
            save["user_object"]["userInfo"]["worldName"] = acc.island_name
        player = save["user_object"]["userInfo"]["player"]
        for field in ("level", "energy", "energyMax"):
            if data.get(field) not in (None, ""):
                player[field] = int(data[field])
        if data.get("energy") not in (None, ""):
            save["user_object"]["userInfo"]["world"]["resources"]["energy"] = int(data["energy"])
        store_session(save)
        _sync_active_session_if_matching(save)

    db.session.commit()
    return Response(json.dumps({"ok": True}), mimetype="application/json")


@app.route("/admin/users/<int:account_id>/gift", methods=['POST'])
def admin_user_gift(account_id):
    acc = db.session.get(auth_engine.Account, account_id)
    if acc is None:
        return Response(json.dumps({"error": "User not found"}), status=404, mimetype="application/json")
    save = _find_save_for_account(acc)
    if save is None:
        return Response(json.dumps({"error": "User has no save yet (must open game once first)"}),
                        status=400, mimetype="application/json")

    data = request.get_json(silent=True) or {}
    player = save["user_object"]["userInfo"]["player"]
    res = save["user_object"]["userInfo"]["world"]["resources"]

    for key in ("cash", "energy"):
        val = int(data.get(key) or 0)
        if val:
            player[key] = int(player.get(key, 0)) + val
    for key in ("coins", "wood", "oil", "iron"):
        val = int(data.get(key) or 0)
        if val:
            res[key] = int(res.get(key, 0)) + val

    item_code = (data.get("item_code") or "").strip()
    item_count = int(data.get("item_count") or 0)
    if item_code and item_count > 0:
        inv = player.setdefault("inventory", {}).setdefault("items", {})
        inv[item_code] = int(inv.get(item_code, 0)) + item_count

    store_session(save)
    _sync_active_session_if_matching(save)
    return Response(json.dumps({"ok": True}), mimetype="application/json")


@app.route("/admin/users/<int:account_id>/delete", methods=['POST'])
def admin_user_delete(account_id):
    acc = db.session.get(auth_engine.Account, account_id)
    if acc is None:
        return Response(json.dumps({"error": "User not found"}), status=404, mimetype="application/json")
    sid = acc.session_id
    db.session.delete(acc)
    if sid:
        sess_model = app.session_interface.sql_session_model
        rec = sess_model.query.filter_by(session_id=sid).first()
        if rec is not None:
            db.session.delete(rec)
    db.session.commit()
    return Response(json.dumps({"ok": True}), mimetype="application/json")


@app.route("/save-editor", methods=['GET'])
def save_editor():
    backups = []
    s = session
    backup_count = 0
    while "backup" in s:
        backups.append(format_backup_message(s["backup"]))
        s = s["backup"]
        backup_count += 1
    print("Backups present", backup_count)

    # No save loaded yet means no quests; the rest of the page renders fine.
    quests = session.get('quests') or []
    incomplete_quests = [e["name"] for e in quests if e["complete"] == False]
    complete_quests = [e["name"] for e in quests if e["complete"]]

    return render_template("save-editor.html", savegame=json.dumps(
        {
            'user_object': session['user_object'] if 'user_object' in session else None,
            'profilePic': session['profilePic'] if 'profilePic' in session else None,
            'quests': session['quests'] if 'quests' in session else None,
            'battle': session['battle'] if 'battle' in session else None,
            'last_battle': session['last_battle'] if 'last_battle' in session else None,
            'fleets': session['fleets'] if 'fleets' in session else None,
            'population': session['population'] if 'population' in session else None,
            'saved': session['saved'] if 'saved' in session else None,
            'save_version': session['save_version'] if 'save_version' in session else None,
            'original_save_version': session['original_save_version'] if 'original_save_version' in session else None,
        }, default=lambda o: '<not serializable>', sort_keys=False, indent=2), uid=get_zid(), backups=backups,
                           valid=validate_save(session, False),
                           quest_names={q: v for q, v in quest_titles.items() if q not in complete_quests and q not in incomplete_quests},
                           incomplete_quest_names={q: v for q, v in quest_titles.items() if q in incomplete_quests},
                           complete_quest_names={q: v for q, v in quest_titles.items() if q in complete_quests}
                           )


@app.route("/save-editor", methods=['POST'])
def save_savegame():
    print("Going to save:")
    restores = [int(key[7:]) for key in request.form.keys() if "restore" in key]

    if restores:
        save_game = session["backup"]
        for i in range(restores[0]):
            save_game = save_game["backup"]
        save_game = copy.deepcopy(save_game)
        print("restoring backup")
        message = "Revert to backup \"" + format_backup_message(save_game) + "\""
    else:
        save_game = json.loads(request.form['savegame'])
        message = "before " + request.form.get("message")

    print(repr(save_game))

    create_backup(message)
    session['saved'] = str(session.get('saved', "")) + "edit"
    session['user_object'] = save_game['user_object']
    if 'profilePic' in session and 'profilePic' in save_game :
        session['profilePic'] = save_game['profilePic']
    else:
        pass
    session['quests'] = save_game['quests']
    session['battle'] = save_game['battle']
    session['fleets'] = save_game['fleets']
    session['population'] = save_game['population']
    session['save_version'] = save_game.get('save_version')

    response = make_response(redirect('/'))
    return response
    # return ('', 400)


def format_backup_message(backup):
    return datetime.fromtimestamp(backup.get('saved_on', 0)).strftime("%d %b %Y %H:%M:%S") + ' - ' \
           + datetime.fromtimestamp(backup.get('replaced_on', 0)).strftime("%d %b %Y %H:%M:%S") + ' @' \
           + str(backup.get('save_version', 'UNKNOWN VERSION')) + ' ' \
           + str(backup.get("message", ""))


@app.route("/127.0.0.1record_stats.php", methods=['GET', 'POST'])
@app.route("/record_stats.php", methods=['GET', 'POST'])
def record_stats():
    return ('', 204)


GAME_SWF = "ZGame.109338_tracer2.swf"  # regular one
# GAME_SWF = "ZGame.109338.swf"
# GAME_SWF = "ZGame.109338_tracer2a.swf"  # with extra debug logging

_patched_swf_cache = {}


@app.route("/files/empire-s.assets.zgncdn.com/assets/109338/ZGame.109338.swf")
def flashFile():
    # Ruffle's NetConnection only speaks Flash Remoting to absolute http(s)
    # URLs, so rewrite the SWF's BASE_PATH to this server's own address. See
    # swf_patch for the details. The file on disk is left untouched; on any
    # unexpected shape we serve it unmodified, exactly as before.
    base_url = request.host_url  # e.g. "http://127.0.0.1:5005/"
    if base_url not in _patched_swf_cache:
        swf_path = os.path.join(base_path(), "assets", GAME_SWF)
        try:
            with open(swf_path, "rb") as swf_file:
                _patched_swf_cache[base_url] = swf_patch.patch_base_path(swf_file.read(), base_url)
            print("Patched SWF BASE_PATH ->", base_url)
        except (OSError, swf_patch.SwfPatchError) as error:
            print("WARNING: serving the SWF unpatched:", error)
            _patched_swf_cache[base_url] = None

    patched = _patched_swf_cache[base_url]
    if patched is None:
        return send_from_directory_mod("assets", GAME_SWF)
    return Response(patched, mimetype="application/x-shockwave-flash")


@app.route("/gameSettings.xml")
def game_settings_file():
    # return send_from_directory_mod("assets/32995", "gameSettings.xml")
    # return send_from_directory_mod("assets/32995", "gameSettings.xml")
    # return send_from_directory_mod("assets/29oct2012", "gameSettings.xml")
    # return send_from_directory_mod("assets/29oct2012", "gameSettings_placeholders.xml")
    return send_from_directory_mod("assets/29oct2012", "gameSettings_with_fixes.xml")


@app.route("/127.0.0.1en_US.xml")
@app.route("/en_US.xml")
def en_us_file():
    # return send_from_directory_mod("assets/32995", "en_US.xml")
    return send_from_directory_mod("assets/29oct2012", "en_US.xml")


@app.route("/127.0.0.1questSettings.xml")
@app.route("/questSettings.xml")
def quest_settings_file():
    return send_from_directory_mod("assets/29oct2012", "questSettings_with_fixes.xml")

ACCOUNT_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 2


@app.route("/login.html", methods=['GET', 'POST'])
def login_page():
    if request.method == 'GET':
        err = "Account is suspended." if request.args.get("suspended") else None
        return render_template("login.html", version=version, release_date=release_date,
                               registered=request.args.get("registered"), error=err)

    account, error = auth_engine.check_credentials(request.form.get("username"),
                                                   request.form.get("password"))
    if error:
        return render_template("login.html", version=version, release_date=release_date,
                               error=error, username=request.form.get("username", "")), 401

    response = make_response(redirect('/'))
    cookie = account.cookie_value
    if cookie:
        # Restoring the cookie restores the save it is keyed by.
        response.set_cookie(app.config.get("SESSION_COOKIE_NAME", "session"), cookie,
                            max_age=ACCOUNT_COOKIE_MAX_AGE, httponly=False, samesite='Lax')
    print("Login:", account.username)
    return response


@app.route("/register.html", methods=['GET', 'POST'])
def register_page():
    if request.method == 'GET':
        return render_template("register.html", version=version, release_date=release_date,
                               switched=request.args.get("switched"))

    username = request.form.get("username", "")
    island_name = request.form.get("island_name", "")
    error = auth_engine.validate_registration(username, request.form.get("password"),
                                              request.form.get("confirm"), island_name)
    if error:
        return render_template("register.html", version=version, release_date=release_date,
                               error=error, username=username, island_name=island_name), 400

    if 'user_object' in session or 'account_id' in session:
        # This browser already holds somebody's save. Writing the new empire
        # into the same cookie would overwrite it, so drop the cookie and let
        # them register again with a clean one.
        response = make_response(redirect('/register.html?switched=1'))
        response.delete_cookie(app.config.get("SESSION_COOKIE_NAME", "session"))
        return response

    account = auth_engine.create_account(username, request.form.get("password"), island_name)
    # The save itself is built by user_response() on the first game request;
    # it reads this name so the new empire is the player's, not a default.
    session['new_world_name'] = account.island_name
    session['account_id'] = account.id
    auth_engine.attach_save(account, session.sid)
    print("Registered:", account.username, "island", account.island_name)
    return redirect('/')


@app.route("/logout")
def logout_page():
    response = make_response(redirect('/login.html'))
    response.delete_cookie(app.config.get("SESSION_COOKIE_NAME", "session"))
    return response

def get_avail_avatars():
    # list(set([u for u in fetch_urls() if u.endswith('.png')]))
    # return avatar_file_names in os.walk(os.path.join(my_games_path() ,"templates/img"))
    # TODO
    avatar_list = ["2_PincusCP_100.png",
                    "4_NavyCP_04_100.png",
                    "5_NavyCP_03_100.png",
                    "6_NavyCP_02_100.png",
                    "7_NavyCP_01_100.png",
                    "8_HansCP_100.png",
                    "9_ArtCP_100.png",
                    "10_ArmyCP_04_100.png",
                    "11_ArmyCP_03_100.png",
                    "12_ArmyCP_02_100.png",
                    "13_ArmyCP_01_100.png",
                    "14_AirForceCP_04_100.png",
                    "15_AirForceCP_03_100.png",
                    "16_AirForceCP_02_100.png",
                    "17_AirForceCP_01_100.png"]
    return avatar_list

def get_avail_motds():
    # return avatar_file_names in os.walk(os.path.join(my_games_path() ,"templates/img/motds"))
    # TODO
    motds = [
        "Loading_BattleBlitz.png",
        "MOTD_AssailantAmbush01.png",
        "MOTD_BlazingInferno02.png",
        "MOTD_BlazingInferno03.png",
        "MOTD_BlazingInferno07.png",
        "MOTD_BlueBarrage11.png",
        "MOTD_BlueBarrage3.png",
        "MOTD_BlueBarrage6.png",
        "MOTD_BlueBarrage8.png",
        "MOTD_BlueBarrage9.png",
        "MOTD_BuildableAmityNavalStation.png",
        "MOTD_BuildableCompCenter.png",
        "MOTD_BuildableDeco_01.png",
        "MOTD_BuildableDeco_02.png",
        "MOTD_BuildableFarmCamp.png",
        "MOTD_BuildableLighthouse.png",
        "MOTD_BuildableNeighborhood.png",
        "MOTD_Campaign02Part01.png",
        "MOTD_CommandoDamageAcademy.png",
        "MOTD_DangerousDeployments3.png",
        "MOTD_DecoBuildableCyberWarfare.png",
        "MOTD_DecoDogPark.png",
        "MOTD_DooberDrop.png",
        "MOTD_ElementZCollider.png",
        "MOTD_Espionage_HQ.png",
        "MOTD_FactoryRefresh.png",
        "MOTD_FierceFighters2.png",
        "MOTD_FrenchXMilitaryCenter.png",
        "MOTD_MGS_02.png",
        "MOTD_MGS_03.png",
        "MOTD_MGS_04.png",
        "MOTD_MS_036.png",
        "MOTD_MS_038.png",
        "MOTD_MS_040.png",
        "MOTD_MS_041.png",
        "MOTD_MS_042.png",
        "MOTD_MS_043.png",
        "MOTD_MS_044_03.png",
        "MOTD_MS_045_02.png",
        "MOTD_MS_046_03.png",
        "MOTD_MS_047_03.png",
        "MOTD_MS_050_02.png",
        "MOTD_MenacingMarines01.png",
        "MOTD_MiniGame.png",
        "MOTD_OreMine.png",
        "MOTD_Parliament.png",
        "MOTD_PotOfOre.png",
        "MOTD_QuestBuildableStatue.png",
        "MOTD_Research_1_1.png",
        "MOTD_SovereignSentinels01.png",
        "MOTD_SovereignSentinels04.png",
        "MOTD_SurvivalModeRefresh02.png",
        "MOTD_Tier08.png",
        "MOTD_TierSurfacing.png",
        "MOTD_VRTrainingSimulator.png",
        "MOTD_VeteranWarriors01.png",
        "MOTD_WorldGames.png",
        "MOTD_LightningDeal01.png",
        "MOTD_LightningDeal02.png",
        "MOTD_LightningDeal03.png",
        "MOTD_LightningDeal04.png",
        "MOTD_LightningDeal05.png",
        "MOTD_LightningDeal06.png",
        "MOTD_LightningDeal07.png",
        "MOTD_LightningDeal08.png",
        "MOTD_LightningDeal09.png",
        "MOTD_LightningDeal10.png",
        "MOTD_LightningDeal11.png",
        "MOTD_LightningDeal12.png",
        "MOTD_LightningDeal13.png",
        "MOTD_LightningDeal14.png",
        "MOTD_LightningDeal15.png",
        "MOTD_LightningDeal16.png",
        "MOTD_LightningDeal17.png",
        "MOTD_LightningDeal18.png",
        "MOTD_LightningDeal19.png",
        "MOTD_LightningDeal20.png",
        "MOTD_LightningDeal21.png",
        "MOTD_LightningDeal22.png",
        "MOTD_LightningDeal25.png",
        "MOTD_LightningDeal28.png",
        "MOTD_LightningDeal29.png",
        "MOTD_LightningDeal30.png",
        "MOTD_LightningDeal31.png",
        "MOTD_LightningDeal32.png",
        "MOTD_LightningDeal33.png",
        "MOTD_LightningDeal34.png",
        "MOTD_Agents_of_Terror.png",
        "MOTD_Alcatraz.png",
        "MOTD_Battleship_Bounty.png",
        "MOTD_Black_Gold_Rush.png",
        "MOTD_Blazing_Inferno_1.png",
        "MOTD_Blazing_Inferno_10.png",
        "MOTD_Blazing_Inferno_4.png",
        "MOTD_Blazing_Inferno_5.png",
        "MOTD_Blazing_Inferno_6.png",
        "MOTD_Blazing_Inferno_8.png",
        "MOTD_Blazing_Inferno_9.png",
        "MOTD_BuildableMechResearch.png",
        "MOTD_Crisis_on_Mercadia.png",
        "MOTD_Cyclone_Power.png",
        "MOTD_Deep_Sea_Discord.png",
        "MOTD_Democracy_by_Warfare.png",
        "MOTD_ElementZ.png",
        "MOTD_FightingTitan.png",
        "MOTD_Fissile_Mission.png",
        "MOTD_Furious_Five_1.png",
        "MOTD_Furious_Five_2.png",
        "MOTD_Furious_Five_3.png",
        "MOTD_Furious_Five_4.png",
        "MOTD_Future_Fleet_Week.png",
        "MOTD_Lucky_Shot_Raffle_Mini-Game.png",
        "MOTD_Madman_Menace.png",
        "MOTD_MechLab.png",
        "MOTD_Menacing_Mercenaries_2.png",
        "MOTD_Nuts_and_Bolts.png",
        "MOTD_Operation_Arctic_Blast.png",
        "MOTD_Out_of_Thin_Air.png",
        "MOTD_Pillage_and_Plunder.png",
        "MOTD_Race_against_Time.png",
        "MOTD_Stolen_AI.png",
        "MOTD_The_Missing_Plans.png",
        "MOTD_Tyrants_and_Assassins.png",
        "MOTD_White_Sabres_1.png",
        "MOTD_White_Sabres_2.png",
        "MOTD_White_Sabres_3.png",
        "MOTD_White_Sabres_4.png",
        "MOTD_Wrath_of_the_Titans_II.png",
        "MOTD_AirForceStorage_01.png",
        "MOTD_ArmyStorage_01.png",
        "MOTD_ElementZRegenerator.png",
        "MOTD_GoldUnits.png",
        "MOTD_HeavenlyRulersPalace.png",
        "MOTD_NavalStorage_01.png",
        "MOTD_SurvivalMode.png",
        "MOTD_World_Domination.png"
    ]
    return motds

@app.route("/chooseavatar/<path:path>", methods=['GET', 'POST'])
def choose_avatar(path):
    avatar = path
    session['profilePic'] = avatar
    response = make_response(redirect('/'))
    return response

def get_avatar_pic():
    avatar_pic = "img/avatars/" + (session['profilePic'] if 'profilePic' in session and session['profilePic'] is not None else "blank.png")
    return avatar_pic

def get_motd():
    motd = "img/motds/" + random.choice(get_avail_motds())
    return motd

@app.route("/changelog.txt")
def change_log():
    return render_template("changelog.txt")

@app.route("/img/<path:path>")
def template_img(path):
    return send_from_directory_mod("templates/img", path)

@app.route("/js/<path:path>")
def template_js(path):
    return send_from_directory_mod("templates/js", path)

@app.route("/css/<path:path>")
def template_css(path):
    return send_from_directory_mod("templates/css", path)

@app.route('/nullassets/<path:path>')
def send_sol_assets(path):
    return send_from_directory_mod('assets/sol_assets_octdict/assets', path)

@app.route('/assets/<path:path>')
def send_sol_assets_alternate(path):
    return send_from_directory_mod('assets/sol_assets_octdict/assets', path)


def send_from_directory_mod(directory, filename, **options):
    absolute_directory = os.path.join(install_path(), directory)
    path = safe_join(os.fspath(absolute_directory), os.fspath(filename))
    print(path)
    mod_path = Path(directory, filename).as_posix()  # mods are keyed relative to the install folder

    return mod_engine.mod.get(mod_path)() if mod_path in mod_engine.mod else send_from_directory(absolute_directory, filename, **options)


def _normalize_amf(obj):
    """Convert Ruffle AMF0 ECMA-array dicts ({0: v0, 1: v1, ...}) into lists recursively."""
    if isinstance(obj, dict):
        if obj and all(isinstance(k, int) for k in obj.keys()):
            keys = sorted(obj.keys())
            if keys == list(range(len(keys))):
                return [_normalize_amf(obj[k]) for k in keys]
        for k, v in list(obj.items()):
            obj[k] = _normalize_amf(v)
        return obj
    if isinstance(obj, list):
        return [_normalize_amf(x) for x in obj]
    return obj


def amf_batch(batch):
    """Return the request batch as a list.

    Flash Player sends the AS3 Array as a strict AMF0 array, which pyamf decodes
    to a list. Ruffle serializes the same Array as an ECMA array, which decodes
    to a dict keyed by index, so iterating it yields ints instead of requests.
    Accept both and recursively normalize nested ECMA arrays in params.
    """
    reqs = [batch[key] for key in sorted(batch, key=lambda k: int(k))] if isinstance(batch, dict) else list(batch or [])
    for r in reqs:
        if hasattr(r, "params"):
            r.params = _normalize_amf(r.params)
    return reqs


@app.route('/files/empire-s.assets.zgncdn.com/assets/109338/127.0.0.1flashservices/gateway.php', methods=['POST'])
@app.route('/flashservices/gateway.php', methods=['POST'])
def post_gateway():
    print("Gateway:")
    print(repr(request))
    # print("Data:")
    # print(request.data)
    resp_msg = remoting.decode(request.data)
    # print(resp_msg.headers)
    print(resp_msg.bodies)
    # print(resp_msg.bodies[0])

    resps = []
    for reqq in amf_batch(resp_msg.bodies[0][1].body[1]):
        if reqq.functionName == 'UserService.initUser':
            try:
                resps.append(user_response())
            except InvalidSaveException as error:
                print('Handling InvalidSaveException error:', error)
                # return make_response(redirect('/save-editor')) #can't do it here

        elif reqq.functionName == 'DataServicesService.getRequestFriends':
            resps.append(friend_response())
        elif reqq.functionName == 'PVPService.getUsersInvaderChallenges':
            resps.append(invader_response())
        elif reqq.functionName == 'ZlingshotService.presence':
            resps.append(zlingshot_response())
        elif reqq.functionName == 'DataServicesService.getRecentPlayers':
            resps.append(recent_response())
        elif reqq.functionName == 'DataServicesService.getFriendsInfo':
            resps.append(friend_info_response())
        elif reqq.functionName == 'UserService.tutorialProgress':
            resps.append(tutorial_response(reqq.params[0], reqq.sequence, resp_msg.bodies[0][0]))
        elif reqq.functionName == 'WorldService.performAction':
            wr = perform_world_response(reqq.params)
            resps.append(wr)
            report_world_log(reqq.params[0] + ' id ' + str(reqq.params[1].id) + '@' + reqq.params[1].position,
                             wr["data"], reqq.params, reqq.sequence, resp_msg.bodies[0][0],
                             wr["metadata"].get('QuestComponent'), wr["metadata"].get('newPVE'))
        elif reqq.functionName == 'DataServicesService.getSuggestedNeighbors':
            resps.append(neighbor_suggestion_response())
        elif reqq.functionName == 'UserService.setSeenFlag':
            resps.append(seen_flag_response(reqq.params[0]))
        elif reqq.functionName == 'PVPService.createRandomFleetChallenge':
            resps.append(random_fleet_challenge_response(reqq.params[0]))
        elif reqq.functionName == 'WorldService.spawnFleet':
            resps.append(spawn_fleet(reqq.params[0]))
        elif reqq.functionName == 'PVPService.loadChallenge':
            resps.append(load_challenge_response(reqq.params[0]))
        elif reqq.functionName == 'WorldService.resolveBattle':
            try:
                resps.append(battle_complete_response(reqq.params[0]))
            except StaleBattleReference as error:
                print('Ignoring stale resolveBattle request:', error)
                resps.append(dummy_response())
        elif reqq.functionName == 'WorldService.genericString':
            resps.append(generic_string_response(reqq.params[0]))
        elif reqq.functionName == 'UserService.streakBonus':
            resps.append(streak_bonus_response(reqq.params[0]))
        elif reqq.functionName == 'UserService.setWorldName':
            resps.append(world_name_response(reqq.params[0]))
        elif reqq.functionName == 'WorldService.updateRoads':
            resps.append(update_roads_response(reqq.params[0]))
        elif reqq.functionName == 'UserService.streamPublish':
            resps.append(stream_publish_response(reqq.params))
        elif reqq.functionName == 'WorldService.stopMayhemEvent':
            resps.append(stop_mayhem_response())
        elif reqq.functionName == 'UserService.saveOptions':
            resps.append(save_options_response(reqq.params[0]))
        elif reqq.functionName == 'WorldService.fullScreen':
            resps.append(full_screen_response())
        elif reqq.functionName == 'WorldService.viewZoom':
            resps.append(view_zoom_response(reqq.params[0].get('zoom')))
        elif reqq.functionName == 'WorldService.loadWorld':
            resps.append(load_world_response(reqq.params))
        elif reqq.functionName == 'VisitorService.help':
            resps.append(tend_ally_response(reqq.params))
        elif reqq.functionName == 'WorldService.beginNextCampaign':
            resps.append(next_campaign_response(reqq.params[0]))
        elif reqq.functionName == 'WorldService.addFleet':
            resps.append(add_fleet_response(reqq.params[0]))
        elif reqq.functionName == 'WorldService.assignConsumable':
            try:
                resps.append(assign_consumable_response(reqq.params[0]))
            except StaleBattleReference as error:
                print('Ignoring stale assignConsumable request:', error)
                resps.append(dummy_response())
        elif reqq.functionName == 'UserService.publishUserAction':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.sendUserNotification':
            resps.append(dummy_response())
        elif reqq.functionName == 'WorldService.sendZaspReport':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.buyCrest':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.buyHealth':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.buySlots':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.clearNotifications':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.createClan':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.getClanInfo':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.getNeighborClanInfo':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.loadAllianceBattle':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.postGroupFeed':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.processMemberQueue':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.removeMember':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.addTaunt':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.updateName':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.updateCrest':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.updateTauntViewTime':
            resps.append(dummy_response())
        elif reqq.functionName == 'DeathMatchService.fetchOpponents':
            resps.append(dummy_response())
        elif reqq.functionName == 'DeathMatchService.joinRoom':
            resps.append(dummy_response())
        elif reqq.functionName == 'DeathMatchService.processRewardQueue':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.acceptQuest':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.acceptDecoBuildableRepel':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.acceptDefenseTowerRepel':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.acceptedGDP':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.acceptedTOS':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.acceptFriendRepel':
            resps.append(accept_friend_repel_response(reqq.params[0]))
        elif reqq.functionName == 'CrossPromoService.accepted':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.acknowledgeTOSStatus':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.reactivateFightMeter':
            resps.append(dummy_response())
        elif reqq.functionName == 'DominationModeService.addDominationChat':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.addFriendPublish':
            resps.append(dummy_response())
        elif reqq.functionName == 'ClansService.completeQuest':
            resps.append(dummy_response())
        elif reqq.functionName == 'RequestService.partRequest':
            resps.append(part_request_response(reqq.params))
        elif reqq.functionName == 'WorldService.beginQuestBattle':
            resps.append(dummy_response())
        elif reqq.functionName == 'BlackMarketHelperService.tradeForPart':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.bookmarksDailySpin':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.buyBack':
            resps.append(dummy_response())
        elif reqq.functionName == 'MiniGameService.buyMiniGameFuel':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.buyCrewRepelPosition':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.buyExpansion':
            resps.append(buy_expansion_response(reqq.params[0]))
        elif reqq.functionName == 'UserService.buyFullHeal':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.buyItem':
            resps.append(buy_item_response(reqq.params[0]))
        elif reqq.functionName == 'UserService.buyItems':
            resps.append(buy_items_response(reqq.params[0]))
        elif reqq.functionName == 'UserService.useItem':
            resps.append(use_item_response(reqq.params[0]))
        elif reqq.functionName == 'UserService.buyMOTDItem':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.buyQuestRestartTask':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.buyQuestTask':
            resps.append(buy_quest_task_response(reqq.params[0]))
        elif reqq.functionName == 'UserService.buyRewardItem':
            resps.append(dummy_response())
        elif reqq.functionName == 'WorldService.calculateRansom':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.cancelUnstartedChallenge':
            resps.append(cancel_unstarted_challenge_response())
        elif reqq.functionName == 'UserService.checkForPromoReward':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.clearOldFlashTokens':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.clearIncentive':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.collectLeaderboards':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.completeSocialRepel':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.crewNeighborPoll':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.crewZMCEvent':
            resps.append(dummy_response())
        elif reqq.functionName == 'MiniGameService.dropBomb':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.energizerSetup':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.EPGiftSend':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.finishSpy':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.setEspionageHQData':
            resps.append(dummy_response())
        elif reqq.functionName == 'WorldService.exitBattle':
            resps.append(exit_battle_response())
        elif reqq.functionName == 'WorldService.expireAQuest':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.getFightList':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.flashFeedRedeemItem':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.EPGiftThankYou':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.getAllChallenges':
            resps.append(dummy_response())
        elif reqq.functionName == 'DominationModeService.getDominationChat':
            resps.append(dummy_response())
        elif reqq.functionName == 'DominationModeService.getDominationModeOpponentList':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.getFBCreditPromoStatus':
            resps.append(dummy_response())
        elif reqq.functionName == 'FeedService.getFeed':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.getLeaderboards':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.getNeighborVisitChallenges':
            resps.append(neighbor_invader_response(reqq.params[0]))
        elif reqq.functionName == 'UserService.getPrisonerInfo':
            resps.append(dummy_response())
        elif reqq.functionName == 'DataServicesService.getPromoData':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.getTargetingData':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.getTargetingGroups':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.grantWatchToEarnRewardNew':
            resps.append(dummy_response())
        elif reqq.functionName == 'DominationModeService.loadDominationModeBattle':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.loadEnemyFleetForChallenge':
            resps.append(random_enemy_fleet_challenge_response(reqq.params[0]))
        elif reqq.functionName == 'QuestSurvivalModeService.loadQuestSurvivalMode':
            resps.append(dummy_response())
        elif reqq.functionName == 'SurvivalModeService.loadSurvivalMode':
            resps.append(load_survival_mode_response(reqq.params[0]))
        elif reqq.functionName == 'UserService.lcs':
            resps.append(dummy_response())
        elif reqq.functionName == 'DataServicesService.getMatchmakingUsersData':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.matchMakingOptFlag':
            resps.append(dummy_response())
        elif reqq.functionName == 'WorldService.mechlabStatus':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.megaSeriesReset':
            resps.append(dummy_response())
        elif reqq.functionName == 'ZlingshotService.fetch':
            resps.append(dummy_response())
        elif reqq.functionName == 'MFSService.collectReward':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.motdAction':
            resps.append(dummy_response())
        elif reqq.functionName == 'WorldService.multiHarvest':
            resps.append(dummy_response())
        elif reqq.functionName == 'DataServicesService.getRecommendedNeighbors':
            resps.append(dummy_response())
        elif reqq.functionName == 'WorldService.openDialog':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.promoAction':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.purchaseAmmoRefill':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.purchaseContractUnlock':
            resps.append(purchase_contact_unlock(reqq.params[0]))
        elif reqq.functionName == 'UserService.purchaseEnergyRefill':
            resps.append(purchase_energy_refill_response(reqq.params[0]))
        elif reqq.functionName == 'UserService.purchaseManaRefill':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.purchaseUnlock':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.globalPVPOptInOut':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.slotMachineSpin':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.slotMachineSpinBuy':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.immunityExtend':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.immunityStart':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.occupationPlace':
            resps.append(occupation_place_response(reqq.params))
        elif reqq.functionName == 'PVPService.pillage':
            resps.append(pillage_response(reqq.params))
        elif reqq.functionName == 'UserService.doFavQuest':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.doSeenQuestNotification':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.questTreeReset':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.questTreeSetMode':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.questTreeStartQuest':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.questTreeUnlockQuest':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.rejectDecoBuildableRepel':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.rejectDefenseTowerRepel':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.rejectFriendRepel':
            resps.append(reject_friend_repel_response(reqq.params[0]))
        elif reqq.functionName == 'UserService.removeExpiredInventory':
            resps.append(dummy_response())
        elif reqq.functionName == 'WorldService.removeExtraInventoryBuildings':
            resps.append(dummy_response())
        elif reqq.functionName == 'WorldService.removeExtraWorldBuildings':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.setTitanName':
            resps.append(dummy_response())
        elif reqq.functionName == 'RequestService.allianceInviteRequest':
            resps.append(dummy_response())
        elif reqq.functionName == 'RequestService.allianceJoinRequest':
            resps.append(dummy_response())
        elif reqq.functionName == 'RequestService.crewRequest':
            resps.append(crew_request_response(reqq.params))
        elif reqq.functionName == 'RequestService.invasionHelpRequest':
            resps.append(dummy_response())
        elif reqq.functionName == 'RequestService.neighborRequest':
            resps.append(dummy_response())
        elif reqq.functionName == 'RequestService.giftRequest':
            resps.append(dummy_response())
        elif reqq.functionName == 'WorldService.resetParliamentDestroyed':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.retrieveNeighborRepelChallenge':
            resps.append(neighbor_repel_challenge_response(reqq.params))
        elif reqq.functionName == 'PVPService.reviveAllies':
            resps.append(dummy_response())
        elif reqq.functionName == 'WorldService.reviveUnits':
            resps.append(dummy_response())
        elif reqq.functionName == 'WorldService.moveRoad':
            resps.append(update_roads_response(reqq.params[0]))
        elif reqq.functionName == 'WorldService.sellRoad':
            resps.append(update_roads_response(reqq.params[0]))
        elif reqq.functionName == 'PVPService.seenPrisonCampNotification':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.seenStrikeTeamComment':
            resps.append(dummy_response())
        elif reqq.functionName == 'WorldService.select':
            resps.append(select_response(reqq.params[0]))
        elif reqq.functionName == 'UserService.setCommandoAnimationDone':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.setCurrentCampaign':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.setDefenderComment':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.setEnergiserAnimationDone':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.setFBCreditParticipation':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.setInvasionComment':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.setStrikeTeamComment':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.setTag':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.spend':
            resps.append(dummy_response())
        elif reqq.functionName == 'DeathMatchService.loadBattle':
            resps.append(dummy_response())
        elif reqq.functionName == 'WorldService.startMayhemEvent':
            resps.append(dummy_response())
        elif reqq.functionName == 'MiniGameService.loadGame':
            resps.append(dummy_response())
        elif reqq.functionName == 'MiniGameService.stop':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.streamPublishWithComment':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.superOreOrder':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.setSurvivalModeToaster':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.treasureVaultSpin':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.unitDropRevealAll':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.unitDropSwitchUnit':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.unitUnlock':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.buyUnlimitedEnergy':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.unlockResource':
            resps.append(dummy_response())
        elif reqq.functionName == 'DominationModeService.updateDefenseForce':
            resps.append(dummy_response())
        elif reqq.functionName == 'WorldService.upgradeResearchBuilding':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.upgradeState':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.useItem':
            resps.append(dummy_response())
        elif reqq.functionName == 'PVPService.useStrikeTeam':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.viralSurfacingSeen':
            resps.append(dummy_response())
        elif reqq.functionName == 'VisitorService.accept':
            resps.append(accept_tend_ally_response(reqq.params))
        elif reqq.functionName == 'VisitorService.decline':
            resps.append(decline_tend_ally_response(reqq.params))
        elif reqq.functionName == 'VisitorService.helpedInvalid':
            resps.append(dummy_response())
        elif reqq.functionName == 'UserService.grantWatchToEarnReward':
            resps.append(dummy_response())
        elif reqq.functionName == 'ZlingshotService.zoom':
            resps.append(dummy_response())
        else:
            resps.append(dummy_response())

        if reqq.functionName != 'UserService.tutorialProgress' and reqq.functionName != 'WorldService.performAction':
            report_other_log(reqq.functionName, resps[-1] if resps else None, reqq, resp_msg.bodies[0][0])

    emsg = {
        "serverTime": datetime.now().timestamp(),
        "errorType": 0,
        "data": resps
    }

    req = remoting.Response(emsg)
    ev = remoting.Envelope(pyamf.AMF0)
    ev[resp_msg.bodies[0][0]] = req
    #  print(ev.headers)
    # print(ev.bodies)

    ret_body = remoting.encode(ev, strict=True, logger=True).getvalue()  # .read()
    # print(ret_body)
    return Response(ret_body, mimetype='application/x-amf')

    # return ('', 204)


# Each military building's basic unit is gated by a contract, not by a level:
# gameSettings marks them with unlockOverrideName "Barracks 01", "Drydocks 01"
# and "Hangar 01". unlockedContracts is only ever filled by buying an unlock, so
# without seeding these the starter troops can never be trained at all.
#
# Only the basic unit of each building is seeded. The same contract names also
# cover premium aircraft (Starfire, Blackbird, Vampire Fighter) and all thirty
# Powerup Factory consumables, which are not starter equipment.
STARTER_CONTRACTS = [
    "U01",  # Cadet Soldiers   (Barracks 01)
    "U43",  # Ironclad Gunboat (Drydocks 01)
    "U66",  # Vintage Fighter  (Hangar 01)
]


def init_user():
    # global game_objects
    unit = "U01,,,,"

    # resources = {"energy": 100, "coins": 100000, "oil": 7000, "wood": 5000, "aluminum": 9000,
    #                                 "copper": 4000, "gold": 3000, "iron": 2000, "uranium": 1000}

    resources = {"energy": 100, "coins": 5000, "oil": 25, "wood": 150, "aluminum": 1000,
                 "copper": 0, "gold": 0, "iron": 0, "uranium": 0}

    # xp = 20000
    # level =100
    # zcash = 1000
    # energy = 200
    # energy_max = 400
    xp = 0
    level = 1
    zcash = 10000
    energy = 100
    energy_max = 100

    # user_fleet = {
    #     "type": "army",
    #     "uid": "0",
    #     "name": "FleetName",
    #     "status": 0,
    #     "target": "",
    #     "consumables": [],
    #     "inventory": [],
    #     "playerLevel": 1,
    #     "specialBits": None,
    #     "lost": None,
    #     "lastUnitLost": None,
    #     "lastIndexLost": None,
    #     "allies": None,
    #     "battleTarget": None,
    #     "battleTimestamp": None,
    #     "ransomRandom": None,
    #     "ransomResource": None,
    #     "ransomAmount": None,
    #     "units": [unit],  # only one unit for tutorial [unit, unit, unit],
    #     "store": [0],  # [0, 0, 0],
    #     "fleets": None,
    #     "upgrades": None,
    #     "hp": None
    # }

    user = {
        "userInfo": {
            "player": {
                "uid": get_zid(),
                "lastTrackingTimestamp": 0,
                "viralSurfacing": {"seen": [], "counts": {}},
                "crewNeighbors": [],
                "dm_band": None,
                "dm_endTS": None,
                "tutorialProgress": "",
                "xp_multiplier": 1,
                "cp_static": 1,
                "lightningDeals": {},
                "friendFlashSessionInfo": {},
                "m_friendFlashEPBank": 1000,
                "playerResourceType": 3,  # 0:coins 1:oil 2: wood 3:alum,
                "meetsCriteriaLowWalletOnly": False,
                "newInstallExperiments": None,
                "staticWorkers": 20,
                "staticWorkersCap": 100,
                # "expansions": {
                #     "data": [4294967295, 4294967295, 4294967295, 4294967295, 4294967295, 4294967295, 4294967295,
                #              4294967295, 4294967295, 4294967295, 4294967295, 4294967295, 4294967295, 4294967295,
                #              4294967295, 4294967295, 4294967295, 4294967295, 4294967295, 4294967295, 4294967295,
                #              4294967295, 4294967295]},
                "expansions": {"data": [0,0,0,0,805306368,6291456,49152,0,0,0,0]},
                "questTrees": {},
                "questTreeUnlocks": {},
                "questTreeData": {},
                "starredQuests": [],
                "seenQuests": [],
                "megaseriesList": {},
                "questGatesComplete": False,
                "warpSeconds": 0,
                "worldGamesOver": 0,
                "lastLoginTimestamp": 0,
                "RUXStatus": False,
                "RUXQuest": False,
                "RUXDay": 0,
                "RUXInitShow": False,
                "RUXDelta": 0,
                "MFSRewardPoints": 0,
                "MFSRewardClaimed": False,
                "MFSRewardLap": 0,
                "mana": {"value": 100},
                "cash": zcash,
                "level": level,
                "xp": xp,
                "numCashTransactions": 100,
                "cashPurchasedTotal": 1000,
                "transactionLog": [],
                "combatSpecialBits": None,
                "promos": None,
                "autopromo": None,
                "motdMerchHistory": None,
                "motdMerchUnlockHistory": None,
                "lufCodes": None,
                "lufEndDate": 0,
                "lufZRTData": None,
                "lufCodesLevelUnlocked": None,
                "miniGamePlayedTS": 0,
                "bomberFuel": 1000,
                "bombTierReached": 0,
                "miniGameRewardTier": 0,
                "miniGameRewards": [],
                "miniGameTotalFuel": 1500,
                "mgrsData": "",
                "seenFlags": {},
                # seen "introCine":True otherwise blackscreen but no startepisode then ->  either false (ZGlobal.noIntroCineVariant != ZGlobal.EXPERIMENT_NO_INTRO_CINE && Zlab.getFlashVar("mute") == null)
                "motdsViewed": [],
                "motdsAccepted": [],
                "m_mayhemEvents": None,
                "bountyCenter": {"lastActivated": 0, "bountyBonds": 0},
                "energizerRefill": 1000,
                "energiserEventFinished": False,
                "energiserAnimationInitTime": 0,
                "energyDeduct": 0,
                "energizerMax": 1000,
                "energy": energy,
                "energyMax": energy_max,
                "unlimitedEnergyTS": 0,
                "unlimitedEnergyIndex": 0,
                "lastEnergyCheck": datetime.now().timestamp(),
                "ammo": 1234,
                "ammoMax": 2000,
                "options": {"musicDisabled": False, "sfxDisabled": False},
                "socialLevelGood": 100,
                "socialXpGood": 10000,
                "socialLevelBad": 0,
                "socialXpBad": 0,
                "scrap": None,
                "enableIdlePopup": False,
                "enableInterPage": False,
                "enableOnetimePopup": False,
                "alliesUsed": None,
                "storageData": {},
                "inventory": {"items": {"B01": 20}},
                "neighborVisits": {},
                "unlockedUnits": [],
                "unlockedItems": [],
                "unlockedContracts": list(STARTER_CONTRACTS),
                "tags": {},
                "consumableUsedByCode": None,
                "treasureVaultOpenTS": 0,
                "watchToEarn": {"dailyWatchCount": 0, "dailyWatchStartTime": 0},
                "userGroups": None,
                "flashFeedTokens": None,
                "lastSurvialModeToaster": 0,
                "clanStatus": None,
                "allianceStorage": None,
                "acceptedAllianceQuests": None,
                "isCIP": False,
                "acceptedGDP": True,
                "acceptedTOS": True,
                "fightMeter": {"lastActivated": 0, "lastInitWeek": 0, "weekInitTarget": 0},
                "fightsPerDay": 1,
                # "fleets": None,
                "activeVirals": {},
                "sequentialLogins": 1,
                "mechDailyRewardOffset": 0,

                "world": {
                    "deathMatch": None
                }
            },
            "worldName": "Rising Empire",
            "titanName": "Titan",
            "isCIP": False,
            "dominanceDefaultFleets": [],
            "bookmarkReward": 0,
            "iconCodes": None,

            "world": {"fleets": [], "enemies": [], "globalFleetId": 0, "battleStatus": {},  # user_fleet
                      "research": {}, "research2": {"buildingTypesUpgraded": None, "treesUnlocked": None},
                      "resourceOrder": ["aluminum", "copper", "gold", "iron", "uranium"],
                      "globalObjectId": 10000,  # initial id high enough not to overlap with preloaded objects
                      "sizeX": 200,
                      "sizeY": 200,
                      "ownerId": 0,
                      "randSeedW": rand_seed_w,
                      "randSeedZ": rand_seed_z,
                      "unitDropData": {"unclaimedUnits": []},
                      "islands": 1,
                      "roadData": initial_island["roads"],
                      "objects": initial_island["objects"],
                      "rewardRandSeedW": 484584,
                      "rewardRandSeedZ": 7549,
                      "ransomRandSeedW": 456647,
                      "ransomRandSeedZ": 4546,
                      "scrapRandSeedW": 5646,
                      "scrapRandSeedZ": 3567,
                      "resources": resources,
                      "campaign": {"current": "camp001", "active": {}, "mastery": {}}
                      }

        },
        "neighbors": get_allies_info(),
        "unlockedResource": {"aluminum": 3, "copper": 4, "gold": 5, "iron": 6},
        "showBookmark": True,
        "firstDay": True,
        "survivalMode": {
            "curSet": "set3",
            "curWave": 1,
            "curLoop": 0,
            "curPlayerFleet": None,
            "curEnemyFleet": None,
            "lastPlayedTime": 0,
            "rewardRefreshCount": 0,
            "rewards": {},
            "rewardChanged": False,
        },
        "prisonerInfo": [],
        "pvp": {"invaders": {"pve": {"quest": True}}, "gpInv": None, "nAssists": None, "immunity": [],
                "notifications": None, "pcCapturedNotify": None, "pcEscapedNotify": None},
        "autoFilledCrewBuildings": None,
        "numCrewNeighborsDirty": 0,
        "ineligibleGiftRecipients": [],
        "priceTestSettings": {},
        "buyBackSettings": {},
        "DEATHMATCH_FRAG_LIMITS": None,
        "DEATHMATCH_CACHE_TTL": None,
        "DEATHMATCH_OPP_COOLDOWN": None,
        "DEATHMATCH_STANDARD_REWARDS": None,
        "DEATHMATCH_GRAND_REWARDS": None,
        "DEATHMATCH_START_TIME": None,
        "DEATHMATCH_DURATION": None,
        "clansInfo": None,
        "immunityTimeVariant": 0,
        "experiments": {"empire_combataicancritical": 2, "empire_decorations_master": 2, "empire_doober_pickup": 3,
                        "empires_consumable_2": 3, "empire_research_shield_upgrade": 2, "empires_support_units": 5,
                        "empire_buildable_zrig_master": 3, "empire_request2_master": 2, "empire_mfs_uili": 4,
                        "empire_survivalmode3_master": 3, "empire_survivalMode_master": 2,
                        "empire_survivalmode_enhancements": 2, "empire_mech_fight_master": 3, "empire_mech_fight_gate_flat": 5,
                        "empire_mech_lab_master": 3, "empire_titanresearch_master": 2, "empire_tier_8": 4,
                        "empire_campv2_enabled": 2, "empire_campaign_mastery": 2},
        "completedQuests": [],
        "decorationsInfo": None,
        "treasureVaultHighlights": None,
        "crewSlotCostExperiment": 0,
        "combatAllyGatingVarient": 0,
        "cashOnLevelUpVarient": 0,
        "immunityViralVarient": 0,
        "autopublishFeedVarient": 0,
        "autopublishFeedMessageVarient": 0,
        "publishFeedPermissionVarient": 0,
        "autopublishAllFeedVarient": 0,
        "researchPartsVariantArmy": 0,
        "researchPartsVariantNavy": 0,
        "researchPartsVariantAir": 0,
        "noIntroCineVariant": 2,  # disable intro cinematics (we don't have yet)
        "energyRewardVariant_v2": 0,
        "energyRewardVariant_v3": 0,
        "allyTimeoutVariant": 0,
        "speedUpViralVarient": 0,
        "pricetestEarlyUnitUnlockVariant": 0,
        "starterBundleVariant": 0,
        "meetsCriteriaStarterPack": False,
        "energyRewardModVariant": 0,
        "enhancedProgressBarVariant": 0,
        "epSpendConfirmation": 0,
        "leaderboardsVariant": 0,
        "survivalModeMaster": 0,
        "survivalModeContinueCost": 0,
        "survivalModeCardAccess": 0,
        "INTERSTITIAL_GIFTING_REWARDS": "",
        "FREE_GIFT_PARTS_BUILDING_DATA": "",
        "snid": 0,
        "snuid": 0,
        "visitorHelpRequests": {},
        "LE_DISCOUNT_SALE": 0,
        "LOW_EP_POPUP": None,
        "COMBAT_NO_ENERGY": 0,
        "COMBAT_DOUBLE_XP": 2,
        "COMBAT_HALF_RANSOM": 1,
        "overlaySurveyURL": None,
        "friendBarVarient": 0,
        "requestTracker": {"giftRequestSent": {}, "helpRequestSent": {}, "crewRequestSent": {},
                           "neighborRequestSent": {}, "repelInvasionRequestSent": {}, "allianceInviteRequestSent": {}},
        "activeRequests": {"request_speedup": None, "request_buildingpart_send": None, "request_crew": None,
                           "request_gift_request": None, "request_gift_send": None, "request_neighbor": None,
                           "request_repel_invasion": None, "request_alliance_invite": None},
        "activeGiftRequests": [],
        "activeHelpRequests": [],
        "allPendingNeighbors": [],
        "playerAsn": 1,
        "questInitAsn": 1,
        "partsGatherInitAsn": 1,
        "blackmarketItems": None,
        "worldEvents": None,
        "FLASHFEED_EXPIRE_TIME": 0,
        "FB_REQ_PERMS": "gdpr",
    }
    return user


# Q0516 ? start
def user_response():
    new_quests = []
    if 'user_object' in session:
        print("Loading user from save")
        if not validate_save(session):
            print("invalid save file")
            raise InvalidSaveException
        user = session['user_object']
        user["userInfo"]["player"]["uid"] = get_zid()
        if session.get('save_version') != version:
            print("WARNING: Save game was saved with version", session.get('save_version'), "while game is version",
                  version)

        qc = session['quests']

        user["neighbors"] = get_allies_info()
        if not user.get("survivalMode"):
            user["survivalMode"] = {
                "curSet": "set3",
                "curWave": 1,
                "curLoop": 0,
                "curPlayerFleet": None,
                "curEnemyFleet": None,
                "lastPlayedTime": 0,
                "rewardRefreshCount": 0,
                "rewards": {},
                "rewardChanged": False,
            }

        meta = {"newPVE": 0, "QuestComponent": [e for e in session['quests'] if e["complete"] == False]}

    else:
        user = copy.deepcopy(init_user())
        print("initialized new")
        # Set by the register page, so a new empire carries the player's own
        # island name instead of the default.
        new_world_name = session.pop('new_world_name', None)
        if new_world_name:
            user["userInfo"]["worldName"] = new_world_name
            print("New empire named", new_world_name)
        session['user_object'] = user
        # qc = [{"name": "Q0516", "complete":False, "expired":False,"progress":[0],"completedTasks":0}]
        session['quests'] = []

        session['save_version'] = version
        session['original_save_version'] = version
        session['saved_on'] = datetime.now().timestamp()
        meta = {"newPVE": 0, "QuestComponent": [e for e in session['quests'] if e["complete"] == False]}
        new_quest_with_sequels("Q0516", new_quests, meta)

    # session['user_object']["userInfo"]["player"]["tutorialProgress"] = "tut_step_krunsch1Battle2Speeech" #'tut_step_inviteFriendsViral'
    # session['user_object']["userInfo"]["player"]["tutorialProgress"] = 'tut_step_remindCombatUIWaitForPreBattleUI'
    # session['user_object']["userInfo"]["player"]["tutorialProgress"] = 'tut_step_remindCombatUIClearCircles'
    # session['user_object']["userInfo"]["player"]["lastEnergyCheck"] = datetime.now().timestamp()
    # save migration only
    # if "lastEnergyCheck" not in session['user_object']["userInfo"]["player"]:
    #     session['user_object']["userInfo"]["player"]["lastEnergyCheck"] = datetime.now().timestamp()

    replenish_energy()

    session["battle"] = None
    session["fleets"] = {}
    session['population'] = lookup_yield()

    if "market" not in session:
        session["market"] = {}
    # #temp migration
    # session['user_object']["experiments"]["empire_store_sorting_rev_enhanced"] = 0
    # session['user_object']["experiments"]["empires_shop_improvements"] = 0
    session['user_object']["experiments"]["empire_combataicancritical"] = 2
    unlock_expansion(156)
    unlock_expansion(157)
    unlock_expansion(181)
    unlock_expansion(182)
    unlock_expansion(206)
    unlock_expansion(207)
    # battle_status = 0
    # island = 2
    # replay_island = 0
    #
    # status_campaign = battle_status | replay_island << 8 | island << 20
    # status_campaign_2 = battle_status | replay_island << 8 | 5 << 20
    # # #
    # user['userInfo']['world']['campaign'] = {"current": "camp001", "active":{'C000': {"status": status_campaign, "fleets":[]},
    #                                                                        'C003': {"status": status_campaign_2, "fleets":[]}}, "mastery": {}}

    # user['userInfo']['world']['campaign'] =  {"current": "camp001", "active": {}, "mastery": {}}
    # user['userInfo']['world']['campaign'] =  {"current": "camp001", "active": {'C000': {"status": 1 << 20, "fleets":[]}}, "mastery": {}}

    # session['campaign'] = {}
    # session['campaign']['C003'] = {'island':4} #will receive a next "island": 1

    # if ""B01": 20"


    # Existing saves predate the seeding in init_user, and a save made before
    # this would have no way to train its starter units.
    player_data = session['user_object']["userInfo"]["player"]
    unlocked_contracts = player_data.setdefault("unlockedContracts", [])
    missing_contracts = [code for code in STARTER_CONTRACTS if code not in unlocked_contracts]
    if missing_contracts:
        unlocked_contracts.extend(missing_contracts)
        print("Unlocking starter contracts", missing_contracts)

    item_inventory = session['user_object']["userInfo"]["player"]["inventory"]["items"]
    if item_inventory.get("B01", 0) < 20:
        item_inventory["B01"] = 20
        print("Refilling upgrade blueprints to 20")  # until friend gift mechanisms are working
    if item_inventory.get("B05", 0) < 25:
        item_inventory["B05"] = 25
        print("Refilling advanced hull plating to 25")  # until friend gift mechanisms are working
    if item_inventory.get("B18", 0) < 25:
        item_inventory["B18"] = 25
        print("Refilling propeller to 25")  # until friend gift mechanisms are working
    if item_inventory.get("B04", 0) < 25:
        item_inventory["B04"] = 25
        print("Refilling mission map to 25")  # until friend gift mechanisms are working
    if item_inventory.get("B23", 0) < 25:
        item_inventory["B23"] = 25
        print("Refilling blast shield to 25")  # until friend gift mechanisms are working
    if item_inventory.get("B17", 0) < 25:
        item_inventory["B17"] = 25
        print("Refilling deck turret to 25")  # until friend gift mechanisms are working

    mechLab_inventory_req = session['user_object']["userInfo"]["player"]["xp"]
    # Set level 21 (1881 xp) for all players to get Titan Lab in inventory
    if mechLab_inventory_req > 1880:
        # every session should only get once
        if not any(i["itemName"] == "MechLab" for i in session['user_object']["userInfo"]["world"]["objects"]):
            if item_inventory.get("TL01", 0) < 1:
                item_inventory["TL01"] = 1
                print("Level 21 has been reached, Titan Lab unlocked at inventory")

    handle_quest_progress(meta, progress_inventory_count())
    handle_quest_progress(meta, progress_neighbor_count())
    handle_quest_progress(meta, progress_upgrades_count())

    activate_unlocked_quests(new_quests, meta)

    for neighbor in user["neighbors"]:
        if neighbor["uid"] in [MIN_ADMIN_ID, MAX_ADMIN_ID]:
            neighbor["level"] =  user["userInfo"]["player"]["level"] + 5

    if session.get('save_version') != version or is_0_08a_preview(version):
        print("Trying migration")
        migrate(meta, session.get('save_version'), version)

    sleep(0.05)  # bugfix required delay for loading entire screen

    # for e in session['user_object']["userInfo"]["world"]["objects"]:
    #     e['lastUpdated'] = 1308211628  #1 minute earlier to test
    user["completedQuests"] = [e["name"] for e in session['quests'] if e["complete"] == True]

    merge_quest_progress(new_quests, meta['QuestComponent'], "output quest")
    merge_quest_progress(new_quests, session['quests'], "session quest")
    user_response = {"errorType": 0, "userId": get_zid(), "metadata": meta,
                     # {"name": "Q0531", "complete":False, "expired":False,"progress":[0],"completedTasks":0},{"name": "QW120", "complete":False, "expired":False,"progress":[0],"completedTasks":0}
                     "data": user}
    return user_response


def friend_response():
    friend_types = ["recommendedFriends", "zyngaFriends", "empireFriends", "sevenDayFriends", "fourteenDayFriends",
                    "thirtyDayFriends", "payerFriends", "allFriends", "zyngaFriendsByEngagement", "empireFriendsByEngagement"]
    friend = {friend_type: {"data": [dict(ally["friend"]) for ally in allies.values() if ally.get("friend") and ally.get(friend_type)]} for friend_type in friend_types}



    #     "recommendedFriends": {"data": [{"zid": 124, "uid":124, "first_name": "MissTery", "sex": 'F',"portrait": "assets/game/GeneralAssetGroup_UI.swf/TheVille_50.png",
    # "pic": "assets/game/GeneralAssetGroup_UI.swf/TheVille_50.png",
    # "pic_square": "assets/game/GeneralAssetGroup_UI.swf/TheVille_50.png"}]},
    #     "zyngaFriends": {"data": [{"zid": 124,"uid":124, "first_name": "MissTery", "sex": 'F', "portrait": "assets/game/GeneralAssetGroup_UI.swf/TheVille_50.png",
    # "pic": "assets/game/GeneralAssetGroup_UI.swf/TheVille_50.png",
    # "pic_square": "assets/game/GeneralAssetGroup_UI.swf/TheVille_50.png"}]},
    #     "empireFriends": {"data": [{"zid": 124,"uid":124, "first_name": "MissTery", "sex": 'F', "portrait": "assets/game/GeneralAssetGroup_UI.swf/TheVille_50.png",
    # "pic": "assets/game/GeneralAssetGroup_UI.swf/TheVille_50.png",
    # "pic_square": "assets/game/GeneralAssetGroup_UI.swf/TheVille_50.png"}]},
    #     "sevenDayFriends": {"data": [{"zid": 124, "uid":124,"first_name": "MissTery", "sex": 'F', "portrait": "assets/game/GeneralAssetGroup_UI.swf/TheVille_50.png",
    # "pic": "assets/game/GeneralAssetGroup_UI.swf/TheVille_50.png",
    # "pic_square": "assets/game/GeneralAssetGroup_UI.swf/TheVille_50.png"}]},
    #     "fourteenDayFriends": {"data": []},
    #     "thirtyDayFriends": {"data": []},
    #     "payerFriends": {"data": []},
    #     "allFriends": {"data": [{"zid": 124,"uid":124, "first_name": "MissTery", "sex": 'F',"portrait": "assets/game/GeneralAssetGroup_UI.swf/TheVille_50.png",
    # "pic": "assets/game/GeneralAssetGroup_UI.swf/TheVille_50.png",
    # "pic_square": "assets/game/GeneralAssetGroup_UI.swf/TheVille_50.png"}]},
    #     "zyngaFriendsByEngagement": {"data": []},
    #     "empireFriendsByEngagement": {"data": []},


    friend_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                       "data": friend}
    return friend_response


def invader_response():
    invader_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                        "data": [invader_entry(k[1:]) for k, v in session['user_object']["pvp"]["invaders"].items() if k != "pve"]}
    return invader_response


def neighbor_invader_response(uid):
    saves = [save for save in get_saves() if
              str(save['user_object']["userInfo"]["player"]["uid"]) == str(uid)]

    if saves:
        neigbor_invader_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                            "data": [invader_entry(k[1:]) for k, v in saves[0]['user_object']["pvp"]["invaders"].items() if k != "pve"]}
    else:
        neigbor_invader_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                                    "data": []}
    return neigbor_invader_response


def neighbor_repel_challenge_response(params):
    [save] = [save for save in get_saves() if
              str(save['user_object']["userInfo"]["player"]["uid"]) == str(params[0])]

    neigbor_invader_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                        "data": next((invader_entry(k[1:] + "_" + str(params[0])) for k, v in save['user_object']["pvp"]["invaders"].items() if k == "u" + str(params[1])), None)}
    return neigbor_invader_response


def accept_friend_repel_response(invader_uid):
    accept_friend_repel_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                        "data": []}

    del session['user_object']["pvp"]["invaders"]["u" + invader_uid]
    return accept_friend_repel_response


def reject_friend_repel_response(invader_uid):
    reject_friend_repel_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                        "data": []}

    del session['user_object']["pvp"]["invaders"]["u" + str(invader_uid)]["dID"]
    return reject_friend_repel_response


def cancel_unstarted_challenge_response():
    cancel_unstarted_invasions()
    cancel_unstarted_challenge_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                        "data": []}

    return cancel_unstarted_challenge_response


def invader_entry(id):
    response = {"state": 0,
                "status": 0,
                "pFID": "",
                "eFID": "fleet1_" + id,
                "eID": id,
                "isAI": False,
                "isPVE": False,
                "isAssist": False
                }
    return response


def zlingshot_response():
    zlingshot_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                          "data": {}}
    return zlingshot_response


def recent_response():
    recent_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                       "data": []}
    return recent_response


def friend_info_response():
    friend_info_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                            "data": {"nonAppFriends": [
                                ]}}
    return friend_info_response


def tutorial_response(step, sequence, endpoint):
    meta = {"newPVE": 0}
    qz = {"name": "Q0516", "complete": True, "expired": False, "progress": [1], "completedTasks": 1}
    qz_cadets_start = {"name": "Q0531", "complete": False, "expired": False, "progress": [0], "completedTasks": 0}
    # complete cadets?
    qz_cadets_start = {"name": "Q0531", "complete": False, "expired": False, "progress": [0], "completedTasks": 0}
    qz_cadets_done = {"name": "Q0531", "complete": True, "expired": False, "progress": [1], "completedTasks": 1}
    qz_invasion_start = {"name": "Q6016", "complete": False, "expired": False, "progress": [0], "completedTasks": 0}
    qz_invasion_done = {"name": "Q6016", "complete": True, "expired": False, "progress": [1], "completedTasks": 1}
    flag_Q1098_start = {"name": "Q1098", "complete": False, "expired": False, "progress": [0], "completedTasks": 0}
    flag_Q1098_done = {"name": "Q1098", "complete": True, "expired": False, "progress": [1], "completedTasks": 1}
    cadets_Q0611_start = {"name": "Q0611", "complete": False, "expired": False, "progress": [0], "completedTasks": 0}
    cadets_Q0611_done = {"name": "Q0611", "complete": True, "expired": False, "progress": [1], "completedTasks": 1}
    flag_Q6011_start = {"name": "Q6011", "complete": False, "expired": False, "progress": [0], "completedTasks": 0}
    flag_Q6011_done = {"name": "Q6011", "complete": True, "expired": False, "progress": [1], "completedTasks": 1}

    sergeant_Q0671_start = {"name": "Q0671", "complete": False, "expired": False, "progress": [0, 0],
                            "completedTasks": 0}  # after 6011
    sergeant_Q0671_done = {"name": "Q0671", "complete": True, "expired": False, "progress": [1, 1], "completedTasks": 2}
    flag_Q0591_start = {"name": "Q0591", "complete": False, "expired": False, "progress": [0], "completedTasks": 0}
    flag_Q0591_done = {"name": "Q0591", "complete": True, "expired": False, "progress": [1], "completedTasks": 1}
    farm_Q0571_start = {"name": "Q0571", "complete": False, "expired": False, "progress": [0, 0], "completedTasks": 0}
    farm_Q0571_done = {"name": "Q0571", "complete": True, "expired": False, "progress": [1, 1], "completedTasks": 2}
    corn_Q0521_start = {"name": "Q0521", "complete": False, "expired": False, "progress": [0], "completedTasks": 0}
    corn_Q0521_done = {"name": "Q0521", "complete": True, "expired": False, "progress": [1], "completedTasks": 1}
    parliament_Q0691_start = {"name": "Q0691", "complete": False, "expired": False, "progress": [0],
                              "completedTasks": 0}
    parliament_Q0691_done = {"name": "Q0691", "complete": True, "expired": False, "progress": [1], "completedTasks": 1}

    # if step == 'tut_step_placeBarracksServer':
    #     meta['QuestComponent'] = [qz, qz_cadets_start]
    # if step == 'tut_step_cadetsComplete':
    #     pass
    # handle_quest_progress(meta, progress_action("build")) build is covered
    # meta['QuestComponent'] = [qz_cadets_done, qz_invasion_start]  #what starts invasion?
    ## meta["newPVE"] = {"status": 2, "pos": "60,63,0", "villain":"v18", "quest":"Q6016"}
    # if step == 'tut_step_firstInvasionEnd':
    # if step == 'tut_step_postFirstInvasionResumeQuests':
    #     handle_quest_progress(meta, progress_action(
    #         "fight"))
    # meta['QuestComponent'] = [qz_invasion_done,flag_Q1098_start,cadets_Q0611_start]
    #  meta["newPVE"] = {"status": 2, "pos": "60,66,0", "villain":"v18", "quest":"Q6016"}  #contineous battle mode experience QT01_05b_2
    # if step == 'tut_step_placeFlagQuestDialog':
    #     meta['QuestComponent'] = [flag_Q1098_done, flag_Q6011_start]

    # if step == 'tut_step_placeFlagWaitForInventoryOpen':   # sometimes one of them is skipped?
    #     meta['QuestComponent'] = [flag_Q1098_done, flag_Q6011_start]
    # if step == 'tut_step_placeFlagEnd':
    #     meta['QuestComponent'] = [flag_Q6011_done, sergeant_Q0671_start, flag_Q0591_start]
    # if step == 'tut_step_buildFarm':  #after cadets placed?
    #     meta['QuestComponent'] = [cadets_Q0611_done, sergeant_Q0671_start] #possibly already done by this point see if it doesn't redo quests already done
    # if step == 'tut_step_placeHouseEnd':  #after cadets placed?
    #     meta['QuestComponent'] = [flag_Q0591_done, farm_Q0571_start]
    # if step == 'tut_step_placeFarmEnd':  #after cadets placed?
    #     meta['QuestComponent'] = [farm_Q0571_done, corn_Q0521_start, parliament_Q0691_start, sergeant_Q0671_start]
    #
    if step == 'tut_step_inviteFriendsEndPauseTutorial':
        handle_quest_progress(meta, progress_neighbor_count())

    merge_quest_progress(meta['QuestComponent'] if 'QuestComponent' in meta else [], session['quests'], "session quest")
    session['user_object']["userInfo"]["player"]["tutorialProgress"] = step  # TODO: revert step when loading if needed

    report_tutorial_step(step, meta['QuestComponent'] if 'QuestComponent' in meta else None, meta['newPVE'], sequence,
                         endpoint)
    tutorial_response = {"errorType": 0, "userId": 1, "metadata": meta,
                         "data": []}
    return tutorial_response


#def perform_world_response(step, supplied_id, position, item_name, reference_item, from_inventory, elapsed, cancel, req2):
def perform_world_response(params):

    step=params[0]
    supplied_id=params[1].id
    position=params[1].position
    item_name=params[1].itemName

    reference_item=params[2][0].get('referenceItem') if len(params[2]) > 0 else None
    from_inventory=params[2][0].get('isGift') if len(params[2]) > 0 else None
    elapsed=params[2][0].get('elapsed') if len(params[2]) > 0 else None
    cancel=params[2][0].get('cancel') if len(params[2]) > 0 else None
    req2=params[2][0] if len(params[2]) > 0 else None
    index_ref=params[2][0] if len(params[2]) > 0 else None

    print("this is step",step, supplied_id, position)

    id = supplied_id
    if step == "place":
        session['user_object']["userInfo"]["world"]["globalObjectId"] += 1  # for place only!
        id = session['user_object']["userInfo"]["world"]["globalObjectId"]
        session['user_object']["userInfo"]["world"]["objects"].append({
            "id": id,
            "itemName": item_name,
            "position": position,
            "referenceItem": reference_item,
            "state": 0
        })

    # cur_object = lookup_object(id)
    # print("cur_object used:"`, repr(cur_object))
    #
    #
    # game_item = lookup_item(item_name)
    # print("item used:", repr(game_item))
    #
    # state_machine = lookup_state_machine(game_item['stateMachineValues']['-stateMachineName'])
    # print("state_machine used:", repr(state_machine))
    # state = lookup_state(state_machine, cur_object['state'])
    # print("cur state:", repr(state))
    # next_click_state = lookup_state(state_machine, state['-clickNext']) # not all states have this!! end states? autostate after time?
    # print("next_click_state:", repr(next_click_state))
    meta = {"newPVE": 0}
    print(step)
    if step in ["setState", "clear", "move", "speedUp", "add", "list", "remove", "staffPosition"] and \
            not any(e['id'] == id for e in session['user_object']["userInfo"]["world"]["objects"]):
        # client can refer to an object that is already gone (e.g. double click); don't crash the request
        print("WARNING: Object", id, "not found for", step + ". Ignoring.")
        return {"errorType": 0, "userId": 1, "metadata": meta, "data": {"id": id}}
    if step in ["place", "setState"]:
        click_next_state(True, id, meta, step, reference_item, cancel=cancel)  # place & setstate only

    if step == "setState":
        if lookup_object(id)["referenceItem"] is None and reference_item is not None:
            ref_item = lookup_item_by_code(reference_item.split(":")[0])
            if item_name == "Mastery Factory 01" and int(ref_item.get("masteryTokenCost","0"))> 0:
                session['user_object']["userInfo"]["player"]["inventory"]["items"]["VTK"] -= int(ref_item["masteryTokenCost"])
            else:
                costs = ref_item.get("cost")
                do_costs({k: v for k, v in costs.items() if k != "-cash"})
        lookup_object(id)["referenceItem"] = reference_item

    if step == "clear":
        session['user_object']["userInfo"]["world"]["objects"].remove(lookup_object(id))
        print("Object", id, "removed")

    if step == "move":
        lookup_object(id)["position"] = position
        print("Object", id, "moved to", position)

    if step == "place":
        if not from_inventory:
            costs = lookup_item_by_name(item_name).get("cost")
            if "-unitClass" in lookup_item_by_name(item_name):
                do_costs({k: v for k, v in costs.items() if k == "-cash"})
            else:
                do_costs(costs)
        else:
            item = lookup_item_by_name(item_name)
            item_inventory = session['user_object']["userInfo"]["player"]["inventory"]["items"]

            if item['-code'] in item_inventory:
                item_inventory[item['-code']] -= 1
                if item_inventory[item['-code']] <= 0:
                    del item_inventory[item['-code']]
                print("Placing", item_name + "(" + item['-code'] + ")", "from inventory")
            else:
                print("ERROR: Placing", item_name + "(" + item['-code'] + ")",
                      "from inventory but not in inventory. Ignoring for now.")

    if step == "speedUp":
        # lookup_object(id)['lastUpdated'] = lookup_object(id).get('lastUpdated', 0) - elapsed * 1000
        click_next_state(False, id, meta, step, reference_item, speed_up=True)
    # TODO: cost of speedup?

    if step == "add":
        market = lookup_object(id)
        refund_market_order(market)
        place_market_order(market, req2, meta)

    perform_world_response = {"errorType": 0, "userId": 1, "metadata": meta,
                              "data": {"id": id}}
    if step == "list":
        market = lookup_object(id)
        # perform_world_response["data"].update(session['market'].get(str(id), {}))
        perform_world_response["data"] = [market]
        # perform_world_response["data"][0]["id"] = id

    if step == "remove":
        market = lookup_object(id)
        perform_world_response["data"] = ["success"]  #  TODO fail if bought
        refund_market_order(market)

    if step == "sell":
        for i in range(len(session['user_object']["userInfo"]["world"]["objects"])):
            if session['user_object']["userInfo"]["world"]["objects"][i]['position'] == position:
                del session['user_object']["userInfo"]["world"]["objects"][i]
                break
        #TODO cost for selling
        # list1 = session['user_object']["userInfo"]["world"]["objects"]
        # session['user_object']["userInfo"]["world"]["objects"] = list(filter(lambda i: i['position'] != position), list1)

    if step == "staffPosition":
        crewTemplate = lookup_crew_template(item_name)
        num_slots = len(crewTemplate["position"])
        decoration = lookup_object(id)

        # TODO Crew index position is given, but not used.
        position_index = index_ref['index']

        current_crew = decoration.get("crewInfo", [])
        new_crew = current_crew + ["-1"]
        decoration["crewInfo"] = new_crew[:num_slots]

        print("staffing")

    if step == "decoCrewBuyOnce":
        crewTemplate = lookup_crew_template(item_name)
        num_slots = len(crewTemplate["position"])
        decoration = lookup_object(id)

        decoration["crewInfo"] = ["-1"] * num_slots
        print("staffing full")

    if step == "randomRewards":
        print(lookup_item_by_name(item_name).get("-code", 0))
        item_info = randomReward(lookup_item_by_name(item_name).get("-code", 0))
        item_code = item_info[0]
        item_amount = item_info[1]
        item_type = item_info[2]
        if item_type == "cash":
            item_amount *= 1000
        perform_world_response["data"]["data"] = {}
        perform_world_response["data"]["data"]["type"] = item_type
        perform_world_response["data"]["data"]["item"] = item_code
        perform_world_response["data"]["data"]["count"] = item_amount
        if item_type == "item":
            if item_code not in session['user_object']["userInfo"]["player"]["inventory"]["items"]:
                session['user_object']["userInfo"]["player"]["inventory"]["items"][item_code] = item_amount
            else:
                session['user_object']["userInfo"]["player"]["inventory"]["items"][item_code] += item_amount
        elif item_type == "cash":
            session['user_object']["userInfo"]["player"]["cash"] += item_amount
        else:
            session['user_object']["userInfo"]["world"]['resources']["coins"] += item_amount

    print("perform_world_response", repr(perform_world_response))
    return perform_world_response


def refund_market_order(market):
    world = session['user_object']["userInfo"]["world"]
    resources = world['resources']
    if market.get("type") == "resource":
        standard_resources = ["coins", "oil" , "wood", "aluminum", "copper", "gold", "iron", "uranium"]
        resources[standard_resources[int(market["item"])]] += market["units"]
        print("Market: Refunded", standard_resources[int(market["item"])] + ":", str(market["units"]) +
              "(" + str(resources[standard_resources[int(market["item"])]]) + ")")
    else:
        print("TODO: Can't refund market type", market.get("type"), "yet")
    market["type"] = None
    market["item"] = None
    market["units"] = None


def place_market_order(market, order, meta):
    world = session['user_object']["userInfo"]["world"]
    resources = world['resources']
    if order["type"] == "resource":
        standard_resources = ["coins", "oil", "wood", "aluminum", "copper", "gold", "iron", "uranium"]
        resources[standard_resources[int(order["item"])]] -= order["units"]
        print("Market: Order placed & removed", standard_resources[int(order["item"])] + ":", str(order["units"])
              + "(" + str(resources[standard_resources[int(order["item"])]]) + ")")
        handle_quest_progress(meta, progress_market_added_count(order["units"]))
    else:
        print("TODO: Can't reserve market type", order.get("type"), "yet")
    market["type"] = order["type"]
    market["item"] = order["item"]
    market["units"] = order["units"]


def neighbor_suggestion_response():
    neighbor_suggestion_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                                    "data": get_suggested_friends()}
    return neighbor_suggestion_response


def seen_flag_response(flag):
    seen_flags = session['user_object']["userInfo"]["player"]["seenFlags"]

    seen_flags[flag] = seen_flags.get(flag, 0) + 1

    seen_flag_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                          "data": []}
    return seen_flag_response


def random_fleet_challenge_response(host_uid):
    unit_user = "U01,,,,"
    unit = "U01,,,,"

    # user_fleet = {
    #     "type": "army",
    #     "uid": "0",
    #     "name": "FleetName",
    #     "status": 0,
    #     "target": "",
    #     "consumables": [],
    #     "inventory": [],
    #     "playerLevel": 1,
    #     "specialBits": None,
    #     "lost": None,
    #     "lastUnitLost": None,
    #     "lastIndexLost": None,
    #     "allies": None,
    #     "battleTarget": None,
    #     "battleTimestamp": None,
    #     "ransomRandom": None,
    #     "ransomResource": None,
    #     "ransomAmount": None,
    #     "units": [
    #         unit_user,
    #
    #               ],  # only one unit for tutorial [unit, unit, unit],
    #     "store": [0],  # [0, 0, 0],
    #     "fleets": None,
    #     "upgrades": None,
    #     "hp": None
    # }

    [save] = [save for save in get_saves() if str(save['user_object']["userInfo"]["player"]["uid"]) == str(host_uid)]
    defender_fleet = save['user_object']["pvp"]["invaders"]["u" + str(get_zid())]["defender_fleet"]

    subtype = lookup_item_by_code(defender_fleet[0].split(',')[0])["-subtype"]

    fleet = {
        "type": subtype,
        "uid": host_uid,
        "name": "FleetName",
        "status": 0,
        "target": "",
        "consumables": [],
        "inventory": [],
        "playerLevel": 1,
        "specialBits": None,
        "lost": None,
        "lastUnitLost": None,
        "lastIndexLost": None,
        "allies": None,
        "battleTarget": None,
        "battleTimestamp": None,
        "ransomRandom": None,
        "ransomResource": None,
        "ransomAmount": None,
        "units": defender_fleet,
        "store": [0],  # [0, 0, 0],
        "fleets": [],
        "upgrades": None,
        "hp": None,
        "invader": False
    }

    register_fleetname_fleet(fleet)

    random_fleet_challenge_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                                       "data": {
                                           "state": 0,
                                           "challengerFleet": fleet,
                                           "challengeInfo": {"status": 0, "state": 1},
                                           "maxUnits": 5
                                       }}
    return random_fleet_challenge_response


def random_enemy_fleet_challenge_response(enemy_fleet_id):
    unit_user = "U01,,,,"
    unit = "U01,,,,"

    # user_fleet = {
    #     "type": "army",
    #     "uid": "0",
    #     "name": "FleetName",
    #     "status": 0,
    #     "target": "",
    #     "consumables": [],
    #     "inventory": [],
    #     "playerLevel": 1,
    #     "specialBits": None,
    #     "lost": None,
    #     "lastUnitLost": None,
    #     "lastIndexLost": None,
    #     "allies": None,
    #     "battleTarget": None,
    #     "battleTimestamp": None,
    #     "ransomRandom": None,
    #     "ransomResource": None,
    #     "ransomAmount": None,
    #     "units": [
    #         unit_user,
    #
    #               ],  # only one unit for tutorial [unit, unit, unit],
    #     "store": [0],  # [0, 0, 0],
    #     "fleets": None,
    #     "upgrades": None,
    #     "hp": None
    # }

    placeholder_fleet = [format_player_fleet("PT24"), format_player_fleet("PT23"), format_player_fleet("PT02"), format_player_fleet("PT22"), format_player_fleet("PT12")]

    if len(enemy_fleet_id.split("_")) <= 2:
        attacker_fleet = session['user_object']["pvp"]["invaders"]["u" + enemy_fleet_id.split("_")[1]].get('attacker_fleet', placeholder_fleet)
    else:
        [save] = [save for save in get_saves() if
                  str(save['user_object']["userInfo"]["player"]["uid"]) == str(enemy_fleet_id.split("_")[2])]
        attacker_fleet = save['user_object']["pvp"]["invaders"]["u" + enemy_fleet_id.split("_")[1]].get('attacker_fleet', placeholder_fleet)

    subtype = lookup_item_by_code(attacker_fleet[0].split(',')[0])["-subtype"]

    fleet = {
        "type": subtype,
        "uid": enemy_fleet_id.split("_")[1],
        "invaded_uid": enemy_fleet_id.split("_")[2] if len(enemy_fleet_id.split("_")) > 2 else None,
        "name": "FleetName",
        "status": 0,
        "target": "",
        "consumables": [],
        "inventory": [],
        "playerLevel": 1,
        "specialBits": None,
        "lost": None,
        "lastUnitLost": None,
        "lastIndexLost": None,
        "allies": None,
        "battleTarget": None,
        "battleTimestamp": None,
        "ransomRandom": None,
        "ransomResource": None,
        "ransomAmount": None,
        "units": attacker_fleet,  # [unit,unit,unit,unit,unit],  # only one unit for tutorial [unit, unit, unit],
        "store": [0],  # [0, 0, 0],
        "fleets": [],
        "upgrades": None,
        "hp": None,
        "invader": True
    }

    register_fleetname_fleet(fleet)

    random_enemy_fleet_challenge_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                                       "data": {
                                           "state": 0,
                                           "challengerFleet": fleet,
                                           "challengeInfo": {"status": 0, "state": 1},
                                           "maxUnits": 5
                                       }}
    return random_enemy_fleet_challenge_response


def load_challenge_response(param):
    load_challenge_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                               "data": {"eFID": "pve", "state": 1}}  # CHALLENGE_STATE_IN_PROGRESS

    session["fleets"][param['challengeeFleet']['name']] = param['challengeeFleet']['units']
    print("Challenge Player fleet:", param['challengeeFleet']['units'])

    return load_challenge_response


def occupation_place_response(params):
    cancel_unstarted_invasions()

    [save] = [save for save in get_saves() if str(save['user_object']["userInfo"]["player"]["uid"]) == str(params[0])]
    occupied_objects = lookup_objects_save_by_position(save, params[1], params[2], 5)
    # todo defenders from larger area?
    # occupied_objects_double = lookup_objects_save_by_position(save, params[1] - 3, params[2] - 3, 11)
    occupied_items = [lookup_item_by_name(e["itemName"]) for e in occupied_objects]
    defense_units = [e for e in occupied_items if "unit" in e]
    defense_units.sort(key=lambda e: int(e["unit"].get("-strength", "1000")), reverse=True)

    subtype = defense_units[0]["-subtype"] if defense_units else "army"

    defense_units = [e for e in defense_units if e["-subtype"] == subtype]

    defense_units = defense_units[:5]

    defense_fleet = [format_player_fleet(e["-code"]) for e in defense_units][:5]

    if not defense_fleet:
        defense_fleet = [format_player_fleet("PT24"), format_player_fleet("PT23"), format_player_fleet("PT02"), format_player_fleet("PT22"), format_player_fleet("PT12")]
    else:
        defense_fleet = (defense_fleet + [format_player_fleet("U01" if subtype == "army" else ("U43" if subtype == "navy" else "U66"))] * 2)[:5]

    save['user_object']["pvp"]["invaders"]["u" + str(get_zid())] = {
        "ts": datetime.now().timestamp(),
        "pillTS": datetime.now().timestamp(),
        "status": 1,
        "pos":  str(params[1]) + "," + str(params[2]),
        "size": 5,
        "chID": str(get_zid()),
        "defender_fleet": defense_fleet
    }
    store_session(save)

    occupation_place_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                      "data": []}
    return occupation_place_response


def pillage_response(params):
    [save] = [save for save in get_saves() if str(save['user_object']["userInfo"]["player"]["uid"]) == str(params[0])]
    # occupied_objects = lookup_objects_save_by_position(save, params[1], params[2], 5)
    # occupied_items = [lookup_item_by_name(e["itemName"]) for e in occupied_objects]
    # defense_units = [e for e in occupied_items if "unit" in e]
    # defense_units.sort(key=lambda e: int(e["unit"].get("-strength", "1000")), reverse=True)
    # defense_units = defense_units[:5]

    save['user_object']["pvp"]["invaders"]["u" + str(get_zid())]["pillTS"] = datetime.now().timestamp()
    store_session(save)

    pillage_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                      "data": []}
    return pillage_response



def generic_string_response(param):
    meta = {"newPVE": 0}
    handle_quest_progress(meta, all_lambda(progress_action("genericString"),
                                           progress_parameter_equals("_string", str(param))))

    generic_string_response = {"errorType": 0, "userId": 1, "metadata": meta,
                               "data": []}
    return generic_string_response


def streak_bonus_response(param):
    meta = {"newPVE": 0}

    do_rewards("Streak", {"_type": "coins", "_count": param["amount"]}, meta)

    streak_bonus_response = {"errorType": 0, "userId": 1, "metadata": meta,
                             "data": []}
    return streak_bonus_response


def world_name_response(name):
    world_name_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                           "data": []}
    session['user_object']["userInfo"]["worldName"] = name

    return world_name_response


def update_roads_response(roads):
    session["user_object"]["userInfo"]["world"]["roadData"] = roads
    update_roads_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                             "data": []}
    return update_roads_response


def stream_publish_response(params):
    meta = {"newPVE": 0}
    handle_quest_progress(meta, progress_feed(params[0]))
    stream_publish_response = {"errorType": 0, "userId": 1, "metadata": meta,
                               "data": []}
    return stream_publish_response


def stop_mayhem_response():
    stop_mayhem_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                            "data": []}
    return stop_mayhem_response


def save_options_response(options):
    save_options_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                             "data": []}
    session['user_object']["userInfo"]["player"]["options"] = options
    return save_options_response


def full_screen_response():
    meta = {"newPVE": 0}
    handle_quest_progress(meta, progress_action("fullscreen"))
    full_screen_response = {"errorType": 0, "userId": 1, "metadata": meta,
                            "data": []}
    return full_screen_response


def view_zoom_response(zoom):
    meta = {"newPVE": 0}
    handle_quest_progress(meta, all_lambda(progress_action("zoom"), progress_parameter_equals("_zoom", str(zoom))))
    view_zoom_response = {"errorType": 0, "userId": 1, "metadata": meta,
                          "data": []}
    return view_zoom_response


def load_world_response(params):
    meta = {"newPVE": 0}
    handle_quest_progress(meta, progress_action("visit"))

    print("world response. requested uid=", int(params[0]), session['user_object']["userInfo"]["player"]["uid"])
    if int(params[0]) == session['user_object']["userInfo"]["player"]["uid"]:
        ally = session['user_object']["userInfo"]
        # qc = session['quests']
        print("reloading user from save")
    else:
        ally = copy.deepcopy(init_user()["userInfo"])
        if int(params[0]) == STEELE_ID:
            ally["world"]["sizeX"] = 100
            ally["world"]["sizeY"] = 100
        ally["player"]["uid"] = int(params[0])
        if str(params[0]) in allies:
            if "objects" in allies[str(params[0])]:
                ally["world"]["objects"] = allies[str(params[0])]["objects"]
            if "roads" in allies[str(params[0])]:
                ally["world"]["roadData"] = allies[str(params[0])]["roads"]
            ##Added
            if "expansions" in allies[str(params[0])]:
                ally["expansions"] = allies[str(params[0])]["expansions"]
            if "worldName" in allies[str(params[0])]:
                ally["worldName"] = allies[str(params[0])]["worldName"]
            if "titanName" in allies[str(params[0])]:
                ally["titanName"] = allies[str(params[0])]["titanName"]
        else:
            [save] = [save for save in get_saves() if str(save['user_object']["userInfo"]["player"]["uid"]) == str(params[0])]
            ally["world"]["objects"] = save['user_object']["userInfo"]["world"]["objects"]
            ally["world"]["roadData"] = save['user_object']["userInfo"]["world"]["roadData"]
            ally["expansions"] = save['user_object']["userInfo"]["player"]["expansions"]
            ally["worldName"] = save['user_object']["userInfo"]["worldName"]
            ally["titanName"] = save['user_object']["userInfo"]["titanName"]
            ally["pvpInvaders"] = save['user_object']["pvp"]["invaders"]

        # ally["gf"] = False
        # ally["yimf"] = ""
        # ally["novisit"] = False
        # ally["globalPVP"] = {}
        # ally["nonFriendInfo"] = {}
        # ally["untendableObjIDs"] = []

        # ally["world"]["yimf"] = ""
    ally["pvpMode"] = params[2]
    ally["pvpImmunity"] = {"expTS": None}
    ally["visitorEnergy"] = 5

    load_world_response = {"errorType": 0, "userId": 1, "metadata": meta,
                           "data": ally}
    return load_world_response


def tend_ally_response(params):
    meta = {"newPVE": 0}
    handle_quest_progress(meta, progress_action("tending"))

    for save in get_saves():
        if save['user_object']["userInfo"]["player"]["uid"] == int(params[0]):
            if not save['user_object']["visitorHelpRequests"]:
                save['user_object']["visitorHelpRequests"] = {}
            if str(get_zid()) in save['user_object']["visitorHelpRequests"]:
                save['user_object']["visitorHelpRequests"][str(get_zid())] += "," + str(params[1])
            else:
                save['user_object']["visitorHelpRequests"][str(get_zid())] = str(params[1])
            store_session(save)
            click_next_state(False, params[1], meta, None, None, tending=True, save=copy.deepcopy(save), tend_type=params[2])

    tend_ally_response = {"errorType": 0, "userId": 1, "metadata": meta,
                          "data": []}
    return tend_ally_response


def accept_tend_ally_response(params):
    meta = {"newPVE": 0}
    for item in session['user_object']["visitorHelpRequests"][params[0]].split(","):
        click_next_state(False, int(item), meta, None, None, playback_tend=True)

    del session['user_object']["visitorHelpRequests"][params[0]]

    accept_tend_ally_response = {"errorType": 0, "userId": 1, "metadata": meta,
                          "data": []}
    return accept_tend_ally_response


def decline_tend_ally_response(params):
    meta = {"newPVE": 0}

    del session['user_object']["visitorHelpRequests"][params[0]]

    decline_tend_ally_response = {"errorType": 0, "userId": 1, "metadata": meta,
                          "data": []}
    return decline_tend_ally_response


def add_fleet_response(param):
    meta = {"newPVE": 0}

    add_fleet_response = {"errorType": 0, "userId": 1, "metadata": meta,
                          "data": []}

    session["fleets"][param['name']] = param['units']
    if "allies" in param:
        session["fleets"]["ally_" + param['name']] = param['allies']
    print("Player fleet:", param['units'])
    return add_fleet_response

def buy_item_response(param):
    meta = {"newPVE": 0}
    print(param)
    buy_item(meta, param["code"], param["amount"])
    buy_item_response = {"errorType": 0, "userId": 1, "metadata": meta,
                      "data": []}
    return buy_item_response
    #TODO buy powerup in combact


def buy_item(meta, code, amount):
    item = lookup_item_by_code(code)
    player = session['user_object']["userInfo"]["player"]
    world = session['user_object']["userInfo"]["world"]
    resources = world['resources']
    # standard_resources = ["coins", "oil", "wood", "aluminum", "copper", "gold", "iron", "uranium"]
    # resources[standard_resources[int(market["item"])]] += market["units"]
    if item["-type"] == 'mercenary':
        print("Buying mercenary")
    elif item.get("-subtype") == "expansion":
        print("Buying expansion")
    elif "-resourceType" in item and item["-resourceType"] != "energy":
        resources[item["-resourceType"]] += amount * int(item.get("resourceYield", "1"))

        print("Buy: received", item["-resourceType"] + ":", str(amount) +
              "(" + str(resources[item["-resourceType"]]) + ")")
    else:
        # param["useCash"]
        item_inventory = session['user_object']["userInfo"]["player"]["inventory"]["items"]
        item_inventory[code] = item_inventory.get(code, 0) + 1
    costs = item.get("cost", {})
    if "-cash" in costs:
        player['cash'] -= get_cash_cost(item, amount)
    else:  # e.g. rare resources bought with coins (RS11-RS15)
        do_costs({k: str(int(v.split('|')[0]) * amount) for k, v in costs.items()})
    handle_quest_progress(meta, progress_buy_consumable(item))


def buy_items_response(param):
    print(repr(param))
    meta = {"newPVE": 0}
    resource_Dict = dict(param["itemData"])
    for Res_code in resource_Dict:
        buy_item(meta, Res_code,resource_Dict[Res_code])
    buy_items_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                      "data": []}
    return buy_items_response


def use_item_response(param):
    print(param)
    item_inventory = session['user_object']["userInfo"]["player"]["inventory"]["items"]
    item_inventory[param] -= 1
    use_item_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                      "data": []}

    return use_item_response



# TODO
def purchase_energy_refill_response(param):
    print(repr(param))

    player = session['user_object']["userInfo"]["player"]
    world = session['user_object']["userInfo"]["world"]
    resources = world['resources']

    player['energy'] += int(player['energyMax']-player['energy'])  # TODO put item in inventory (storable?)

    player['cash'] -= int(player['energyMax']-player['energy'])

    purchase_energy_refill_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                      "data": []}
    return purchase_energy_refill_response


def buy_quest_task_response(param):
    print(param,type(param))
    
    # reqq.params[2][0].get('referenceItem')
    # player = session['user_object']["userInfo"]["player"]
    # world = session['user_object']["userInfo"]["world"]

    meta = {"newPVE": 0}
    handle_quest_progress(meta, progress_quest_task(param["name"], param["taskIndex"]))
    buy_quest_task_response = {"errorType": 0, "userId": 1, "metadata": meta, "data": []}
    return buy_quest_task_response


def buy_expansion_response(param):
    print(repr(param))
    unlock_expansion(param['index'])
    meta = {"newPVE": 0}
    handle_quest_progress(meta, progress_action("expansionsPurchased"))
    buy_expansion_response = {"errorType": 0, "userId": 1, "metadata": meta,
                      "data": []}
    return buy_expansion_response


def purchase_contact_unlock(param):
    print(repr(param))
    item = lookup_item_by_code(param["itemCode"])

    #TODO specops contractMultiple
    unlock_cost = int(game_settings['settings']['gamesettings']['-contractUnlockMultiple']) * get_cash_cost(item, 1)

    player = session['user_object']["userInfo"]["player"]
    player['cash'] -= unlock_cost
    item_inventory = session['user_object']["userInfo"]["player"]["inventory"]["items"]
    item_inventory[param["itemCode"]] = item_inventory.get(param["itemCode"], 0) + 1
    player['unlockedContracts'].append(param["itemCode"])

    meta = {"newPVE": 0}
    purchase_contact_unlock = {"errorType": 0, "userId": 1, "metadata": meta,
                              "data": []}
    return purchase_contact_unlock


def get_cash_cost(item, amount):
    #TODO unit price expirements cost
    #TODO priceTestSettings
    #TODO EXPERIMENT_LE_DISCOUNT_SALE
    cash_cost = float(item["cost"]["-cash"])

    required_level = int(item.get("requiredLevel", "0"))
    player_level = session["user_object"]["userInfo"]["player"]["level"]

    le_discount = 0
    if "requiredDate" in item:
        if required_level >= 25:
            if player_level >= 51:
                le_discount = .25
            elif player_level >= 35:
                le_discount = .15
        elif required_level >= 15:
            if player_level >= 51:
                le_discount = .30
            elif player_level >= 35:
                le_discount = .20
            elif player_level >= 20:
                le_discount = .10

    return math.ceil(cash_cost * amount * (1 - le_discount))


def select_response(param):
    meta = {"newPVE": 0}
    handle_quest_progress(meta, all_lambda(progress_action("select"),
                                           progress_parameter_equals("_item", param['itemCode'])))
    select_response = {"errorType": 0, "userId": 1, "metadata": meta,
                          "data": []}
    return select_response


def part_request_response(params):
    item = lookup_item_by_name(params[0])
    item_inventory = session['user_object']["userInfo"]["player"]["inventory"]["items"]

    item_inventory[item["-code"]] = item_inventory.get(item["-code"], 0) + len(params[3])

    meta = {"newPVE": 0}
    handle_quest_progress(meta, progress_gifted_parts(item, len(params[3])))
    handle_quest_progress(meta, progress_inventory_count())

    part_request_response = {"errorType": 0, "userId": 1, "metadata": meta,
                      "data": []}
    return part_request_response

def crew_request_response(params):
    # 'params': [[124], 10003, 'Parliament', 'crew'] (or {0: 124, ...} from Ruffle AMF)
    raw_friends = params[0]
    friends = [raw_friends[k] for k in sorted(raw_friends.keys())] if isinstance(raw_friends, dict) else list(raw_friends or [])
    building_id = params[1]
    building_name = params[2]
    action = params[3]
    
    building = next((e for e in session['user_object']["userInfo"]["world"]["objects"] if e['id'] == building_id), None)
    if building is None:
        print("crew_request_response: building", building_id, building_name, "not found in world objects, skipping")
        return {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0}, "data": []}
    
    if action == 'crew':
        
        current_crew = building.get("crewInfo", [])

        crewTemplate = lookup_crew_template(building_name)
        max_buyable_slots = int(crewTemplate["-numUnbuyableSlots"])
        num_slots = len(crewTemplate["position"])

        # Empty slots are automatically filled (NPC neighbors + real player friends).
        auto_accept_friends = [f for f in friends if (str(f) in allies and allies[str(f)].get("neighbor")) or str(f).lstrip("-").isdigit()]
        avail_slots = max(min(num_slots - len(current_crew), num_slots), 0)
        new_crew = current_crew + auto_accept_friends[:avail_slots]
        building["crewInfo"] = [str(x) for x in new_crew] # string needed, so that no friend is 'deleted' on display
        
        print(building_name, "new crew (max", num_slots, "slots):", building.get("crewInfo", []))
    
    meta = {"newPVE": 0}
    crew_request_response = {"errorType": 0, "userId": 1, "metadata": meta, 'data': []}
    return crew_request_response


def exit_battle_response():
    exit_battle_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                      "data": []}

    #session["last_battle"] = session["battle"]
    session["battle"] = None

    return exit_battle_response


def load_survival_mode_response(param):
    wave_index = session["fleets"]["FleetName"]["playerLevel"] + 1 if is_continued_survival(param) else 1
    if param.get("continueGame") or "playerFleet" in param or (not is_new_wave_survival(param) and is_resume_survival()):
        wave_index = wave_index - 1
    wave = lookup_wave("set3", wave_index)

    if "playerFleet" not in param and not is_new_wave_survival(param) and is_resume_survival():
        unit_codes = decode_unit_count_list(wave['fleet'])
        #baddies = [encode_unit_string(code, hp=hp) for code, hp in zip(unit_codes, session["battle"][1]) if hp > 0]
        baddies = encode_unit_strings(decode_unit_count_list(wave['fleet']))
    else:
        baddies = encode_unit_strings(decode_unit_count_list(wave['fleet']))

    fleet = {
        "type": wave["-unitType"],
        "uid": 1,
        "name": "FleetName",
        "status": 4096, #survival enemy
        "target": "",
        "consumables": [],
        "inventory": [],
        "playerLevel": wave_index,
        "specialBits": None,
        "lost": None,
        "lastUnitLost": None,
        "lastIndexLost": None,
        "allies": None,
        "battleTarget": None,
        "battleTimestamp": None,
        "ransomRandom": None,
        "ransomResource": None,
        "ransomAmount": None,
        "units": baddies,
        "store": [0],  # [0, 0, 0],
        "fleets": [],
        "upgrades": None,
        "hp": None,
        "invader": True
    }

    if "playerFleet" in param:
        load_survival_mode_resp = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                                   "data": {'state': 1, 'storage': {"curSet": "set3", "curWave": wave_index, "curLoop": 0,
                                                                    "curPlayerFleet": None,
                                                                    "curEnemyFleet": None, "lastPlayedTime": 0,
                                                                    "rewardRefreshCount": 0, "rewards": {},
                                                                    "rewardChanged": False}}}
        session["fleets"][param['playerFleetName']] = param["playerFleet"]["units"]
    elif is_new_wave_survival(param):
        # load_survival_mode_resp = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
        #                            "data": {}}
        load_survival_mode_resp = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                                   "data": {'state': 3, 'storage': {"curSet": "set3", "curWave": wave_index, "curLoop": 0,
                                                                    "curPlayerFleet": get_survival_player_fleet(),
                                                                    "curEnemyFleet": fleet, "lastPlayedTime": 0,
                                                                    "rewardRefreshCount": 0, "rewards": {},
                                                                    "rewardChanged": False}, "enemyWaveFleet": fleet, "playerFleet":get_survival_player_fleet()}}
        session["battle"] = session["last_battle"]
        session["battle"] = ([e for e in session["battle"][0] if e != 0], None, session["battle"][2], session["battle"][3])
        register_fleetname_fleet(fleet)
    elif is_resume_survival():
        print("resume paused survival")
        # load_survival_mode_resp = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
        #                            "data": {}}
        load_survival_mode_resp = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                                   "data": {'state': 3,
                                            'storage': {"curSet": "set3", "curWave": wave_index, "curLoop": 0,
                                                        "curPlayerFleet": get_survival_player_fleet(),
                                                        "curEnemyFleet": fleet, "lastPlayedTime": 0,
                                                        "rewardRefreshCount": 0, "rewards": {},
                                                        "rewardChanged": False}, "enemyWaveFleet": fleet,
                                            "playerFleet": get_survival_player_fleet()}}
        #session["battle"] = session["last_battle"]  # paused # current battle is still in battle  WorldService.exitBattle isn't called on pause only genericString with ExitBattle
        # session["battle"] = ([e for e in session["battle"][0] if e != 0], None, session["battle"][2])
        register_fleetname_fleet(fleet)
    else:
        load_survival_mode_resp = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                                   "data": {'state': 2, 'storage': {"curSet": "set3", "curWave": wave_index, "curLoop": 0,
                                                                    "curPlayerFleet": None,
                                                                    "curEnemyFleet": None, "lastPlayedTime": 0,
                                                                    "rewardRefreshCount": 0, "rewards": {},
                                                                    "rewardChanged": False}, "enemyWaveFleet": fleet}}
        register_fleetname_fleet(fleet)
        session["last_battle"] = None
    return load_survival_mode_resp


def is_continued_survival(param):
    return "FleetName" in session["fleets"] and \
           session["fleets"]["FleetName"]["status"] == 4096 and \
           (param["set"] is None or is_resume_survival())


def is_new_wave_survival(param):
    return param["set"] is None and not param.get("continueGame")


def is_resume_survival():
    return session.get("last_battle") and "FleetName" in session["fleets"] and session["fleets"]["FleetName"][
        "status"] == 4096


# def unit_encode


def dummy_response():
    dummy_response = {"errorType": 0, "userId": 1, "metadata": {"newPVE": 0},
                      "data": []}
    return dummy_response


@app.route("/language_editor")
def language_editor():
    tree = ET.parse("assets/29oct2012/en_US.xml")
    root = tree.getroot()
    for pkg in root:
        print(pkg.tag, pkg.attrib)
        for text in pkg:
            print("  ",text.tag, text.attrib)

    return '', 204



# @app.after_request
# def add_header(r):
#     """
#     Add headers to both force latest IE rendering engine or Chrome Frame,
#     and also to cache the rendered page for 10 minutes.
#     """
#     # r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
#     # r.headers["Pragma"] = "no-cache"
#     # r.headers["Expires"] = "0"
#     # r.headers['Cache-Control'] = 'public, max-age=0'
#     return r

@socketio.on('message')
def handle_message(message):
    print('received message: ' + message)


@socketio.on('my event')
def handle_my_custom_event(json):
    print('received json: ' + str(json))


@socketio.on('delete_save')
def delete_save(message):
    # session.permanent = False
    # session.clear() #unsolved flask-session bug

    # print('deleted save: ' + message)
    print("Save will be deleted after redirect " + message)


@app.errorhandler(500)
def server_error_page(error):
    # Without the explicit code Flask answers 500s with "200 OK", which hides
    # server errors from anything checking the status (the admin page did).
    return 'It went wrong', 500


def enc_hook(obj):
    if isinstance(obj, BattleContext):
        return {
            "__type__": "BattleContext",
            "__v__": 1,
            "map_name": obj.map_name,
            "island": obj.island,
            "replaying": obj.replaying,
        }
    raise TypeError

def decode_objects(obj):
    if isinstance(obj, dict) and (obj.get("__type__") == "BattleContext" or obj.get("type") == "BattleContext"): #something odd going on
        return BattleContext(
            map_name=obj.get("map_name"),
            island=obj.get("island"),
            replaying=obj.get("replaying", False),
        )

    if isinstance(obj, list):
        return [decode_objects(x) for x in obj]

    if isinstance(obj, dict):
        return {k: decode_objects(v) for k, v in obj.items()}

    return obj

_original_init = MsgSpecSerializer.__init__

def serializer_init_patch(self, app, format):
    _original_init(self, app, format)

    self.encoder = msgspec.msgpack.Encoder(enc_hook=enc_hook)
    original_decode = self.decode

    def decode_wrapper(data):
        return decode_objects(original_decode(data))

    self.decode = decode_wrapper

MsgSpecSerializer.__init__ = serializer_init_patch



if __name__ == '__main__':
    if 'WERKZEUG_RUN_MAIN' not in os.environ and settings.open_browser:
        if os.path.exists(os.path.join("chromium", "chrome.exe")):
            threading.Timer(1.25, lambda: os.system(os.path.join("chromium", "chrome.exe") + " --user-data-dir=\"" + os.path.join(my_games_path(), "chromium-profile") + "\"" + " --allow-outdated-plugins " + ("--app=" if settings.app_mode else "") + "http://" + settings.http_host + ":" + str(settings.port) + "/" + settings.http_path)).start()
        elif os.path.exists(os.path.join("chromium", "chrome")):
            threading.Timer(1.25, lambda: os.system(os.path.join("chromium", "chrome") + " --user-data-dir=\"" + os.path.join(my_games_path(), "chromium-profile") + "\"" + " --–allow-outdated-plugins " + ("--app=" if settings.app_mode else "") + "http://" + settings.http_host + ":" + str(settings.port) + "/" + settings.http_path)).start()
        else:
            threading.Timer(1.25, lambda: webbrowser.open("http://" + settings.http_host + ":" + str(settings.port) + "/" + settings.http_path)).start()
    # init_db(app, db)
    set_crash_log(settings.crash_log)
    if settings.compression:
        compress.init_app(app)
    socketio.init_app(app)
    db.init_app(app)
    sess.init_app(app)
    with app.app_context():
        configure_database_engine(db.engine)
        db.create_all()  # accounts table; flask-session makes its own
        auth_engine.ensure_account_columns()
    app.session_interface = AssetSkippingSessionInterface(app.session_interface)
    # session.app.session_interface.db.create_all()
    # app.session_interface.db.create_all()
    # db.create_all()

    import signal
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    socketio.run(app, host=settings.host, port=settings.port, debug=settings.debug, allow_unsafe_werkzeug=True)
    # app.run(host='127.0.0.1', port=5005, debug=True)
    # logging.getLogger('socketio').setLevel(logging.ERROR)
    # logging.getLogger('engineio').setLevel(logging.ERROR)
    # logging.getLogger('geventwebsocket.handler').setLevel(logging.ERROR)