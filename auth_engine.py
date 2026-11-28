"""Accounts for the login and register pages.

A save is keyed by the Flask session cookie, so an account is only a named
handle on one of those cookies: signing in restores the cookie that belongs to
the username, and the save loads exactly as it did before.

Passwords are stored as a Werkzeug hash, never in the clear. This is a local
preservation server with no password reset, so the hash is all there is.
"""

import re
from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

from save_engine import db

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,20}$")
MIN_PASSWORD_LENGTH = 6
MAX_ISLAND_NAME_LENGTH = 30
SESSION_KEY_PREFIX = "session:"


class Account(db.Model):
    __tablename__ = "accounts"

    id = db.Column(db.Integer, primary_key=True)
    # Stored lower case so "Egie" and "egie" cannot both be registered.
    username = db.Column(db.String(20), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    island_name = db.Column(db.String(MAX_ISLAND_NAME_LENGTH), nullable=False)
    # Row id of this account's save in the sessions table, "session:<cookie>".
    session_id = db.Column(db.String(255), nullable=True)
    is_suspended = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def cookie_value(self):
        """The session cookie that loads this account's save, or None."""
        if not self.session_id:
            return None
        return self.session_id.split(":", 1)[1] if ":" in self.session_id else self.session_id


def ensure_account_columns():
    """Add new columns to existing SQLite accounts table if missing."""
    from sqlalchemy import text
    cols = {row[1] for row in db.session.execute(text("PRAGMA table_info(accounts)"))}
    if "is_suspended" not in cols:
        db.session.execute(text("ALTER TABLE accounts ADD COLUMN is_suspended BOOLEAN NOT NULL DEFAULT 0"))
        db.session.commit()


def normalise_username(username):
    return (username or "").strip().lower()


def find_account(username):
    return Account.query.filter_by(username=normalise_username(username)).first()


def check_credentials(username, password):
    """Return (account, error). The error never says which half was wrong."""
    account = find_account(username)
    if account is None or not account.check_password(password or ""):
        return None, "Wrong username or password."
    if account.is_suspended:
        return None, "Account is suspended."
    return account, None


def validate_registration(username, password, confirm, island_name):
    """Return the first problem with these details, or None if they are fine."""
    username = (username or "").strip()
    island_name = (island_name or "").strip()

    if not USERNAME_PATTERN.match(username):
        return ("Username must be 3 to 20 characters, using letters, digits, "
                "dot, dash or underscore.")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password != confirm:
        return "The two passwords do not match."
    if not island_name:
        return "Give your island a name."
    if len(island_name) > MAX_ISLAND_NAME_LENGTH:
        return f"Island name must be at most {MAX_ISLAND_NAME_LENGTH} characters."
    if find_account(username) is not None:
        return "That username is taken."
    return None


def create_account(username, password, island_name):
    account = Account(username=normalise_username(username),
                      island_name=island_name.strip())
    account.set_password(password)
    db.session.add(account)
    db.session.commit()
    return account


def attach_save(account, sid):
    """Remember which save row belongs to this account."""
    account.session_id = SESSION_KEY_PREFIX + sid
    db.session.commit()
