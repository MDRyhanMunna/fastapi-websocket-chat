import os
import re
import sqlite3
import uuid

from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt

from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    UploadFile,
    File,
    Header,
    HTTPException,
    status,
)

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pwdlib import PasswordHash


app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "chat.db"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_SIZE = 10 * 1024 * 1024

ALLOWED_REACTIONS = {"👍", "❤️", "😂", "😮", "😢"}

ALLOWED_UPLOAD_TYPES = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/gif": {".gif"},
    "image/webp": {".webp"},
    "application/pdf": {".pdf"},
    "text/plain": {".txt"},
    "text/csv": {".csv"},
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {".docx"},
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {".xlsx"},
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": {".pptx"},
}

SECRET_KEY = os.getenv(
    "CHAT_SECRET_KEY",
    "dev-secret-change-this-before-production",
)

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

password_hash = PasswordHash.recommended()

app.mount(
    "/uploads",
    StaticFiles(directory=str(UPLOAD_DIR)),
    name="uploads",
)


class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class RoomCreateRequest(BaseModel):
    name: str
    description: str = ""
    visibility: str = "public"


class RoomMemberRequest(BaseModel):
    username: str


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_database_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _table_columns(connection, table_name: str):
    rows = connection.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()
    return {row["name"] for row in rows}


def initialize_database():
    connection = get_database_connection()

    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'public'
                    CHECK(visibility IN ('public', 'private')),
                owner_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(owner_id) REFERENCES users(id)
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS room_members (
                room_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'member'
                    CHECK(role IN ('owner', 'moderator', 'member')),
                joined_at TEXT NOT NULL,
                PRIMARY KEY(room_id, user_id),
                FOREIGN KEY(room_id) REFERENCES rooms(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_room_members_user
            ON room_members(user_id, room_id)
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS uploads (
                id TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                stored_name TEXT NOT NULL UNIQUE,
                original_name TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(owner_id) REFERENCES users(id)
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_uploads_owner
            ON uploads(owner_id, created_at)
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room TEXT NOT NULL,
                username TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                edited_at TEXT,
                deleted_at TEXT,
                attachment_id TEXT,
                reply_to_id INTEGER
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS direct_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user1_id INTEGER NOT NULL,
                user2_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user1_id, user2_id),
                FOREIGN KEY(user1_id) REFERENCES users(id),
                FOREIGN KEY(user2_id) REFERENCES users(id)
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS direct_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                read_at TEXT,
                edited_at TEXT,
                deleted_at TEXT,
                attachment_id TEXT,
                reply_to_id INTEGER,
                FOREIGN KEY(conversation_id)
                    REFERENCES direct_conversations(id),
                FOREIGN KEY(sender_id)
                    REFERENCES users(id)
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS message_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                emoji TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(message_id, user_id),
                FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS direct_message_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                emoji TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(message_id, user_id),
                FOREIGN KEY(message_id) REFERENCES direct_messages(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        message_columns = _table_columns(
            connection,
            "messages"
        )

        if "edited_at" not in message_columns:
            print("Migrating messages: adding edited_at")
            connection.execute(
                """
                ALTER TABLE messages
                ADD COLUMN edited_at TEXT
                """
            )

        if "deleted_at" not in message_columns:
            print("Migrating messages: adding deleted_at")
            connection.execute(
                """
                ALTER TABLE messages
                ADD COLUMN deleted_at TEXT
                """
            )

        if "attachment_id" not in message_columns:
            print("Migrating messages: adding attachment_id")
            connection.execute(
                """
                ALTER TABLE messages
                ADD COLUMN attachment_id TEXT
                """
            )

        if "reply_to_id" not in message_columns:
            print("Migrating messages: adding reply_to_id")
            connection.execute(
                """
                ALTER TABLE messages
                ADD COLUMN reply_to_id INTEGER
                """
            )

        dm_columns = _table_columns(
            connection,
            "direct_messages"
        )

        if "read_at" not in dm_columns:
            print("Migrating direct_messages: adding read_at")
            connection.execute(
                """
                ALTER TABLE direct_messages
                ADD COLUMN read_at TEXT
                """
            )
            connection.execute(
                """
                UPDATE direct_messages
                SET read_at = created_at
                WHERE read_at IS NULL
                """
            )

        if "edited_at" not in dm_columns:
            print("Migrating direct_messages: adding edited_at")
            connection.execute(
                """
                ALTER TABLE direct_messages
                ADD COLUMN edited_at TEXT
                """
            )

        if "deleted_at" not in dm_columns:
            print("Migrating direct_messages: adding deleted_at")
            connection.execute(
                """
                ALTER TABLE direct_messages
                ADD COLUMN deleted_at TEXT
                """
            )

        if "attachment_id" not in dm_columns:
            print("Migrating direct_messages: adding attachment_id")
            connection.execute(
                """
                ALTER TABLE direct_messages
                ADD COLUMN attachment_id TEXT
                """
            )

        if "reply_to_id" not in dm_columns:
            print("Migrating direct_messages: adding reply_to_id")
            connection.execute(
                """
                ALTER TABLE direct_messages
                ADD COLUMN reply_to_id INTEGER
                """
            )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_room_id
            ON messages(room, id)
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_direct_messages_conversation
            ON direct_messages(conversation_id, id)
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_message_reactions_message
            ON message_reactions(message_id)
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_dm_reactions_message
            ON direct_message_reactions(message_id)
            """
        )

        # ----------------------------------------------------
        # ROOM MANAGEMENT MIGRATION
        # ----------------------------------------------------
        # Keep every room that existed before room management.
        # Legacy rooms are public and have no owner, so existing
        # chat history remains accessible without deleting data.
        legacy_rooms = connection.execute(
            """
            SELECT DISTINCT room
            FROM messages
            WHERE TRIM(room) != ''
            """
        ).fetchall()

        for legacy_room in legacy_rooms:
            legacy_name = legacy_room["room"].strip()
            connection.execute(
                """
                INSERT OR IGNORE INTO rooms (
                    slug,
                    name,
                    description,
                    visibility,
                    owner_id,
                    created_at
                )
                VALUES (?, ?, ?, 'public', NULL, ?)
                """,
                (
                    legacy_name,
                    legacy_name,
                    "Existing room migrated from the earlier chat version.",
                    now_iso(),
                )
            )

        # Always provide a default public room for a fresh database.
        connection.execute(
            """
            INSERT OR IGNORE INTO rooms (
                slug,
                name,
                description,
                visibility,
                owner_id,
                created_at
            )
            VALUES ('general', 'general', 'Default public room', 'public', NULL, ?)
            """,
            (now_iso(),)
        )

        # Search/pagination indexes. Safe to run every startup.
        connection.execute("CREATE INDEX IF NOT EXISTS idx_messages_room_id ON messages(room, id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_messages_message ON messages(message)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_dm_conversation_id_id ON direct_messages(conversation_id, id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_dm_message ON direct_messages(message)")

        connection.commit()

    finally:
        connection.close()


initialize_database()


# ============================================================
# USERS
# ============================================================

def get_user_by_username(username: str):
    connection = get_database_connection()

    try:
        return connection.execute(
            """
            SELECT
                id,
                username,
                email,
                password_hash,
                created_at
            FROM users
            WHERE username = ?
            """,
            (username,)
        ).fetchone()
    finally:
        connection.close()


def get_user_by_email(email: str):
    connection = get_database_connection()

    try:
        return connection.execute(
            """
            SELECT id
            FROM users
            WHERE email = ?
            """,
            (email,)
        ).fetchone()
    finally:
        connection.close()


def get_all_usernames():
    connection = get_database_connection()

    try:
        rows = connection.execute(
            """
            SELECT username
            FROM users
            ORDER BY username
            """
        ).fetchall()

        return [row["username"] for row in rows]
    finally:
        connection.close()


def create_user(
    username: str,
    email: str,
    hashed_password: str
):
    connection = get_database_connection()

    try:
        cursor = connection.execute(
            """
            INSERT INTO users (
                username,
                email,
                password_hash,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                username,
                email,
                hashed_password,
                now_iso(),
            )
        )

        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


# ============================================================
# ROOM MANAGEMENT
# ============================================================

def make_room_slug(name: str):
    slug = re.sub(
        r"[^a-z0-9]+",
        "-",
        name.strip().lower()
    ).strip("-")

    return slug[:60]


def get_room_by_slug(slug: str):
    connection = get_database_connection()

    try:
        return connection.execute(
            """
            SELECT
                rooms.id,
                rooms.slug,
                rooms.name,
                rooms.description,
                rooms.visibility,
                rooms.owner_id,
                owner.username AS owner_username,
                rooms.created_at
            FROM rooms
            LEFT JOIN users owner
                ON owner.id = rooms.owner_id
            WHERE rooms.slug = ?
            """,
            (slug,)
        ).fetchone()
    finally:
        connection.close()


def get_room_membership(
    username: str,
    slug: str
):
    connection = get_database_connection()

    try:
        return connection.execute(
            """
            SELECT
                room_members.role,
                room_members.joined_at
            FROM room_members
            JOIN rooms
                ON rooms.id = room_members.room_id
            JOIN users
                ON users.id = room_members.user_id
            WHERE rooms.slug = ?
              AND users.username = ?
            """,
            (slug, username)
        ).fetchone()
    finally:
        connection.close()


def add_room_membership(
    room_id: int,
    user_id: int,
    role: str = "member"
):
    connection = get_database_connection()

    try:
        connection.execute(
            """
            INSERT OR IGNORE INTO room_members (
                room_id,
                user_id,
                role,
                joined_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                room_id,
                user_id,
                role,
                now_iso(),
            )
        )
        connection.commit()
    finally:
        connection.close()


def create_room_record(
    username: str,
    name: str,
    description: str,
    visibility: str
):
    owner = get_user_by_username(username)

    if not owner:
        return {
            "success": False,
            "message": "User does not exist."
        }

    clean_name = name.strip()
    clean_description = description.strip()[:300]
    clean_visibility = visibility.strip().lower()

    if len(clean_name) < 2:
        return {
            "success": False,
            "message": "Room name must contain at least 2 characters."
        }

    if len(clean_name) > 60:
        return {
            "success": False,
            "message": "Room name cannot be longer than 60 characters."
        }

    if clean_visibility not in {"public", "private"}:
        return {
            "success": False,
            "message": "Visibility must be public or private."
        }

    base_slug = make_room_slug(clean_name)

    if not base_slug:
        return {
            "success": False,
            "message": "Room name must contain letters or numbers."
        }

    connection = get_database_connection()

    try:
        slug = base_slug
        suffix = 2

        while connection.execute(
            "SELECT 1 FROM rooms WHERE slug = ?",
            (slug,)
        ).fetchone():
            slug = f"{base_slug[:54]}-{suffix}"
            suffix += 1

        cursor = connection.execute(
            """
            INSERT INTO rooms (
                slug,
                name,
                description,
                visibility,
                owner_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                slug,
                clean_name,
                clean_description,
                clean_visibility,
                owner["id"],
                now_iso(),
            )
        )

        room_id = cursor.lastrowid

        connection.execute(
            """
            INSERT INTO room_members (
                room_id,
                user_id,
                role,
                joined_at
            )
            VALUES (?, ?, 'owner', ?)
            """,
            (
                room_id,
                owner["id"],
                now_iso(),
            )
        )

        connection.commit()

        return {
            "success": True,
            "room": {
                "id": room_id,
                "slug": slug,
                "name": clean_name,
                "description": clean_description,
                "visibility": clean_visibility,
                "owner_username": username,
                "role": "owner",
                "member_count": 1,
            }
        }
    finally:
        connection.close()


def join_room_record(
    username: str,
    slug: str
):
    user = get_user_by_username(username)
    room = get_room_by_slug(slug)

    if not user or not room:
        return {
            "success": False,
            "message": "Room does not exist."
        }

    membership = get_room_membership(
        username,
        slug
    )

    if membership:
        return {
            "success": True,
            "role": membership["role"],
            "room": dict(room),
        }

    if room["visibility"] == "private":
        return {
            "success": False,
            "message": "This is a private room. The room owner must add you first."
        }

    add_room_membership(
        room["id"],
        user["id"],
        "member"
    )

    return {
        "success": True,
        "role": "member",
        "room": dict(room),
    }


def can_access_room(
    username: str,
    slug: str,
    auto_join_public: bool = False
):
    room = get_room_by_slug(slug)

    if not room:
        return {
            "success": False,
            "reason": "not_found",
            "message": "Room does not exist."
        }

    membership = get_room_membership(
        username,
        slug
    )

    if membership:
        return {
            "success": True,
            "room": dict(room),
            "role": membership["role"],
        }

    if room["visibility"] == "public" and auto_join_public:
        join_result = join_room_record(
            username,
            slug
        )

        if join_result["success"]:
            return {
                "success": True,
                "room": join_result["room"],
                "role": join_result["role"],
            }

    return {
        "success": False,
        "reason": "forbidden",
        "message": "You do not have access to this room."
    }


def list_rooms_for_user(
    username: str
):
    user = get_user_by_username(username)

    if not user:
        return []

    connection = get_database_connection()

    try:
        rows = connection.execute(
            """
            SELECT
                rooms.id,
                rooms.slug,
                rooms.name,
                rooms.description,
                rooms.visibility,
                owner.username AS owner_username,
                room_members.role AS role,
                (
                    SELECT COUNT(*)
                    FROM room_members members_count
                    WHERE members_count.room_id = rooms.id
                ) AS member_count
            FROM rooms
            LEFT JOIN users owner
                ON owner.id = rooms.owner_id
            LEFT JOIN room_members
                ON room_members.room_id = rooms.id
               AND room_members.user_id = ?
            WHERE rooms.visibility = 'public'
               OR room_members.user_id IS NOT NULL
            ORDER BY
                CASE WHEN room_members.user_id IS NOT NULL THEN 0 ELSE 1 END,
                LOWER(rooms.name)
            """,
            (user["id"],)
        ).fetchall()

        return [
            {
                "id": row["id"],
                "slug": row["slug"],
                "name": row["name"],
                "description": row["description"],
                "visibility": row["visibility"],
                "owner_username": row["owner_username"],
                "role": row["role"],
                "member_count": row["member_count"],
            }
            for row in rows
        ]
    finally:
        connection.close()


def get_room_members(
    requester_username: str,
    slug: str
):
    access = can_access_room(
        requester_username,
        slug,
        auto_join_public=False
    )

    if not access["success"]:
        return {
            "success": False,
            "message": access["message"],
            "members": [],
        }

    room = access["room"]
    connection = get_database_connection()

    try:
        rows = connection.execute(
            """
            SELECT
                users.username,
                room_members.role,
                room_members.joined_at
            FROM room_members
            JOIN users
                ON users.id = room_members.user_id
            WHERE room_members.room_id = ?
            ORDER BY
                CASE room_members.role
                    WHEN 'owner' THEN 0
                    WHEN 'moderator' THEN 1
                    ELSE 2
                END,
                LOWER(users.username)
            """,
            (room["id"],)
        ).fetchall()

        return {
            "success": True,
            "role": access["role"],
            "room": room,
            "members": [
                {
                    "username": row["username"],
                    "role": row["role"],
                    "joined_at": row["joined_at"],
                }
                for row in rows
            ],
        }
    finally:
        connection.close()


def add_member_to_room(
    requester_username: str,
    slug: str,
    target_username: str
):
    room = get_room_by_slug(slug)
    requester = get_user_by_username(requester_username)
    target = get_user_by_username(target_username)

    if not room:
        return {
            "success": False,
            "message": "Room does not exist."
        }

    if not requester or not target:
        return {
            "success": False,
            "message": "User does not exist."
        }

    requester_membership = get_room_membership(
        requester_username,
        slug
    )

    if not requester_membership or requester_membership["role"] != "owner":
        return {
            "success": False,
            "message": "Only the room owner can add members."
        }

    target_membership = get_room_membership(
        target_username,
        slug
    )

    if target_membership:
        return {
            "success": False,
            "message": f"{target_username} is already a room member."
        }

    add_room_membership(
        room["id"],
        target["id"],
        "member"
    )

    return {
        "success": True,
        "message": f"{target_username} was added to the room."
    }


def remove_member_from_room(
    requester_username: str,
    slug: str,
    target_username: str
):
    room = get_room_by_slug(slug)

    if not room:
        return {
            "success": False,
            "message": "Room does not exist."
        }

    requester_membership = get_room_membership(
        requester_username,
        slug
    )

    if not requester_membership or requester_membership["role"] != "owner":
        return {
            "success": False,
            "message": "Only the room owner can remove members."
        }

    target_membership = get_room_membership(
        target_username,
        slug
    )

    if not target_membership:
        return {
            "success": False,
            "message": "That user is not a room member."
        }

    if target_membership["role"] == "owner":
        return {
            "success": False,
            "message": "The room owner cannot be removed."
        }

    target = get_user_by_username(target_username)

    connection = get_database_connection()

    try:
        connection.execute(
            """
            DELETE FROM room_members
            WHERE room_id = ?
              AND user_id = ?
            """,
            (room["id"], target["id"])
        )
        connection.commit()
    finally:
        connection.close()

    return {
        "success": True,
        "message": f"{target_username} was removed from the room."
    }


def leave_room_record(
    username: str,
    slug: str
):
    room = get_room_by_slug(slug)
    membership = get_room_membership(
        username,
        slug
    )

    if not room or not membership:
        return {
            "success": False,
            "message": "You are not a member of this room."
        }

    if membership["role"] == "owner":
        return {
            "success": False,
            "message": "The room owner cannot leave the room."
        }

    user = get_user_by_username(username)
    connection = get_database_connection()

    try:
        connection.execute(
            """
            DELETE FROM room_members
            WHERE room_id = ?
              AND user_id = ?
            """,
            (room["id"], user["id"])
        )
        connection.commit()
    finally:
        connection.close()

    return {
        "success": True,
        "message": "You left the room."
    }


# ============================================================
# UPLOAD / ATTACHMENT FUNCTIONS
# ============================================================

def save_upload_record(
    upload_id: str,
    owner_id: int,
    stored_name: str,
    original_name: str,
    mime_type: str,
    size: int,
):
    connection = get_database_connection()

    try:
        connection.execute(
            """
            INSERT INTO uploads (
                id,
                owner_id,
                stored_name,
                original_name,
                mime_type,
                size,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                upload_id,
                owner_id,
                stored_name,
                original_name,
                mime_type,
                size,
                now_iso(),
            )
        )
        connection.commit()
    finally:
        connection.close()


def get_upload_by_id(upload_id: str | None):
    if not upload_id:
        return None

    connection = get_database_connection()

    try:
        row = connection.execute(
            """
            SELECT
                id,
                owner_id,
                stored_name,
                original_name,
                mime_type,
                size,
                created_at
            FROM uploads
            WHERE id = ?
            """,
            (upload_id,)
        ).fetchone()

        if not row:
            return None

        return {
            "id": row["id"],
            "original_name": row["original_name"],
            "mime_type": row["mime_type"],
            "size": row["size"],
            "url": f"/uploads/{row['stored_name']}",
        }
    finally:
        connection.close()


def get_owned_upload(
    upload_id: str | None,
    username: str,
):
    if not upload_id:
        return None

    connection = get_database_connection()

    try:
        row = connection.execute(
            """
            SELECT
                uploads.id,
                uploads.stored_name,
                uploads.original_name,
                uploads.mime_type,
                uploads.size
            FROM uploads
            JOIN users
                ON users.id = uploads.owner_id
            WHERE uploads.id = ?
              AND users.username = ?
            """,
            (upload_id, username)
        ).fetchone()

        if not row:
            return None

        return {
            "id": row["id"],
            "original_name": row["original_name"],
            "mime_type": row["mime_type"],
            "size": row["size"],
            "url": f"/uploads/{row['stored_name']}",
        }
    finally:
        connection.close()


# ============================================================
# GROUP MESSAGES
# ============================================================

def save_message(
    room: str,
    username: str,
    message: str,
    created_at: str,
    attachment_id: str | None = None,
    reply_to_id: int | None = None,
):
    connection = get_database_connection()

    try:
        cursor = connection.execute(
            """
            INSERT INTO messages (
                room,
                username,
                message,
                created_at,
                attachment_id,
                reply_to_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                room,
                username,
                message,
                created_at,
                attachment_id,
                reply_to_id,
            )
        )

        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def get_message_history(
    room: str,
    limit: int = 50
):
    connection = get_database_connection()

    try:
        rows = connection.execute(
            """
            SELECT
                id,
                username,
                message,
                created_at,
                edited_at,
                deleted_at,
                attachment_id,
                reply_to_id
            FROM messages
            WHERE room = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (room, limit)
        ).fetchall()

        rows = list(reversed(rows))

        return [
            {
                "id": row["id"],
                "username": row["username"],
                "message": (
                    "This message was deleted"
                    if row["deleted_at"]
                    else row["message"]
                ),
                "timestamp": row["created_at"],
                "edited_at": row["edited_at"],
                "deleted_at": row["deleted_at"],
                "attachment": (
                    None
                    if row["deleted_at"]
                    else get_upload_by_id(row["attachment_id"])
                ),
                "reply_to": get_group_reply_target(
                    row["reply_to_id"],
                    room
                ),
                "reactions": (
                    []
                    if row["deleted_at"]
                    else get_group_reactions(row["id"])
                ),
            }
            for row in rows
        ]
    finally:
        connection.close()


def edit_group_message(
    message_id: int,
    room: str,
    username: str,
    new_message: str
):
    connection = get_database_connection()

    try:
        row = connection.execute(
            """
            SELECT id, room, username, deleted_at
            FROM messages
            WHERE id = ?
            """,
            (message_id,)
        ).fetchone()

        if not row:
            return {
                "success": False,
                "message": "Message does not exist."
            }

        if row["room"] != room:
            return {
                "success": False,
                "message": "Message does not belong to this room."
            }

        if row["username"] != username:
            return {
                "success": False,
                "message": "You can only edit your own messages."
            }

        if row["deleted_at"]:
            return {
                "success": False,
                "message": "Deleted messages cannot be edited."
            }

        edited_at = now_iso()

        connection.execute(
            """
            UPDATE messages
            SET message = ?, edited_at = ?
            WHERE id = ?
            """,
            (
                new_message,
                edited_at,
                message_id,
            )
        )

        connection.commit()

        return {
            "success": True,
            "edited_at": edited_at,
        }
    finally:
        connection.close()


def delete_group_message(
    message_id: int,
    room: str,
    username: str
):
    connection = get_database_connection()

    try:
        row = connection.execute(
            """
            SELECT id, room, username, deleted_at
            FROM messages
            WHERE id = ?
            """,
            (message_id,)
        ).fetchone()

        if not row:
            return {
                "success": False,
                "message": "Message does not exist."
            }

        if row["room"] != room:
            return {
                "success": False,
                "message": "Message does not belong to this room."
            }

        if row["username"] != username:
            return {
                "success": False,
                "message": "You can only delete your own messages."
            }

        if row["deleted_at"]:
            return {
                "success": True,
                "deleted_at": row["deleted_at"],
            }

        deleted_at = now_iso()

        connection.execute(
            """
            UPDATE messages
            SET
                message = '',
                deleted_at = ?
            WHERE id = ?
            """,
            (
                deleted_at,
                message_id,
            )
        )

        connection.execute(
            "DELETE FROM message_reactions WHERE message_id = ?",
            (message_id,)
        )

        connection.commit()

        return {
            "success": True,
            "deleted_at": deleted_at,
        }
    finally:
        connection.close()


# ============================================================
# DIRECT CONVERSATIONS / MESSAGES
# ============================================================

def get_conversation_id(
    user_a_id: int,
    user_b_id: int,
    create_if_missing: bool = False
):
    user1_id = min(user_a_id, user_b_id)
    user2_id = max(user_a_id, user_b_id)

    connection = get_database_connection()

    try:
        row = connection.execute(
            """
            SELECT id
            FROM direct_conversations
            WHERE user1_id = ?
              AND user2_id = ?
            """,
            (
                user1_id,
                user2_id,
            )
        ).fetchone()

        if row:
            return row["id"]

        if not create_if_missing:
            return None

        cursor = connection.execute(
            """
            INSERT INTO direct_conversations (
                user1_id,
                user2_id,
                created_at
            )
            VALUES (?, ?, ?)
            """,
            (
                user1_id,
                user2_id,
                now_iso(),
            )
        )

        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def save_direct_message(
    sender_username: str,
    recipient_username: str,
    message: str,
    created_at: str,
    attachment_id: str | None = None,
    reply_to_id: int | None = None,
):
    sender = get_user_by_username(
        sender_username
    )

    recipient = get_user_by_username(
        recipient_username
    )

    if not sender or not recipient:
        return None

    conversation_id = get_conversation_id(
        sender["id"],
        recipient["id"],
        create_if_missing=True
    )

    connection = get_database_connection()

    try:
        cursor = connection.execute(
            """
            INSERT INTO direct_messages (
                conversation_id,
                sender_id,
                message,
                created_at,
                read_at,
                attachment_id,
                reply_to_id
            )
            VALUES (?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                conversation_id,
                sender["id"],
                message,
                created_at,
                attachment_id,
                reply_to_id,
            )
        )

        connection.commit()

        return {
            "message_id": cursor.lastrowid,
            "conversation_id": conversation_id,
        }
    finally:
        connection.close()


def get_direct_message_history(
    username: str,
    other_username: str,
    limit: int = 50
):
    user = get_user_by_username(
        username
    )

    other_user = get_user_by_username(
        other_username
    )

    if not user or not other_user:
        return []

    conversation_id = get_conversation_id(
        user["id"],
        other_user["id"],
        create_if_missing=False
    )

    if not conversation_id:
        return []

    connection = get_database_connection()

    try:
        rows = connection.execute(
            """
            SELECT
                dm.id,
                u.username AS sender,
                dm.message,
                dm.created_at,
                dm.read_at,
                dm.edited_at,
                dm.deleted_at,
                dm.attachment_id,
                dm.reply_to_id
            FROM direct_messages dm
            JOIN users u
                ON u.id = dm.sender_id
            WHERE dm.conversation_id = ?
            ORDER BY dm.id DESC
            LIMIT ?
            """,
            (
                conversation_id,
                limit,
            )
        ).fetchall()

        rows = list(reversed(rows))

        return [
            {
                "id": row["id"],
                "sender": row["sender"],
                "message": (
                    "This message was deleted"
                    if row["deleted_at"]
                    else row["message"]
                ),
                "timestamp": row["created_at"],
                "read_at": row["read_at"],
                "edited_at": row["edited_at"],
                "deleted_at": row["deleted_at"],
                "attachment": (
                    None
                    if row["deleted_at"]
                    else get_upload_by_id(row["attachment_id"])
                ),
                "reply_to": get_dm_reply_target(
                    row["reply_to_id"],
                    conversation_id
                ),
                "reactions": (
                    []
                    if row["deleted_at"]
                    else get_dm_reactions(row["id"])
                ),
            }
            for row in rows
        ]
    finally:
        connection.close()


def mark_direct_messages_read(
    reader_username: str,
    other_username: str
):
    reader = get_user_by_username(
        reader_username
    )

    other_user = get_user_by_username(
        other_username
    )

    if not reader or not other_user:
        return {
            "message_ids": [],
            "read_at": None
        }

    conversation_id = get_conversation_id(
        reader["id"],
        other_user["id"],
        create_if_missing=False
    )

    if not conversation_id:
        return {
            "message_ids": [],
            "read_at": None
        }

    connection = get_database_connection()

    try:
        rows = connection.execute(
            """
            SELECT id
            FROM direct_messages
            WHERE conversation_id = ?
              AND sender_id = ?
              AND read_at IS NULL
              AND deleted_at IS NULL
            """,
            (
                conversation_id,
                other_user["id"],
            )
        ).fetchall()

        message_ids = [
            row["id"]
            for row in rows
        ]

        if not message_ids:
            return {
                "message_ids": [],
                "read_at": None
            }

        read_at = now_iso()

        connection.execute(
            """
            UPDATE direct_messages
            SET read_at = ?
            WHERE conversation_id = ?
              AND sender_id = ?
              AND read_at IS NULL
              AND deleted_at IS NULL
            """,
            (
                read_at,
                conversation_id,
                other_user["id"],
            )
        )

        connection.commit()

        return {
            "message_ids": message_ids,
            "read_at": read_at,
        }
    finally:
        connection.close()


def edit_direct_message(
    message_id: int,
    sender_username: str,
    new_message: str
):
    sender = get_user_by_username(
        sender_username
    )

    if not sender:
        return {
            "success": False,
            "message": "Sender does not exist."
        }

    connection = get_database_connection()

    try:
        row = connection.execute(
            """
            SELECT
                dm.id,
                dm.sender_id,
                dm.deleted_at,
                dc.user1_id,
                dc.user2_id
            FROM direct_messages dm
            JOIN direct_conversations dc
                ON dc.id = dm.conversation_id
            WHERE dm.id = ?
            """,
            (message_id,)
        ).fetchone()

        if not row:
            return {
                "success": False,
                "message": "Message does not exist."
            }

        if row["sender_id"] != sender["id"]:
            return {
                "success": False,
                "message": "You can only edit your own messages."
            }

        if row["deleted_at"]:
            return {
                "success": False,
                "message": "Deleted messages cannot be edited."
            }

        recipient_id = (
            row["user2_id"]
            if row["user1_id"] == sender["id"]
            else row["user1_id"]
        )

        recipient_row = connection.execute(
            """
            SELECT username
            FROM users
            WHERE id = ?
            """,
            (recipient_id,)
        ).fetchone()

        edited_at = now_iso()

        connection.execute(
            """
            UPDATE direct_messages
            SET message = ?, edited_at = ?
            WHERE id = ?
            """,
            (
                new_message,
                edited_at,
                message_id,
            )
        )

        connection.commit()

        return {
            "success": True,
            "recipient": recipient_row["username"],
            "edited_at": edited_at,
        }
    finally:
        connection.close()


def delete_direct_message(
    message_id: int,
    sender_username: str
):
    sender = get_user_by_username(
        sender_username
    )

    if not sender:
        return {
            "success": False,
            "message": "Sender does not exist."
        }

    connection = get_database_connection()

    try:
        row = connection.execute(
            """
            SELECT
                dm.id,
                dm.sender_id,
                dm.deleted_at,
                dc.user1_id,
                dc.user2_id
            FROM direct_messages dm
            JOIN direct_conversations dc
                ON dc.id = dm.conversation_id
            WHERE dm.id = ?
            """,
            (message_id,)
        ).fetchone()

        if not row:
            return {
                "success": False,
                "message": "Message does not exist."
            }

        if row["sender_id"] != sender["id"]:
            return {
                "success": False,
                "message": "You can only delete your own messages."
            }

        recipient_id = (
            row["user2_id"]
            if row["user1_id"] == sender["id"]
            else row["user1_id"]
        )

        recipient_row = connection.execute(
            """
            SELECT username
            FROM users
            WHERE id = ?
            """,
            (recipient_id,)
        ).fetchone()

        deleted_at = row["deleted_at"]

        if not deleted_at:
            deleted_at = now_iso()

            connection.execute(
                """
                UPDATE direct_messages
                SET
                    message = '',
                    deleted_at = ?
                WHERE id = ?
                """,
                (
                    deleted_at,
                    message_id,
                )
            )

            connection.execute(
                "DELETE FROM direct_message_reactions WHERE message_id = ?",
                (message_id,)
            )

            connection.commit()

        return {
            "success": True,
            "recipient": recipient_row["username"],
            "deleted_at": deleted_at,
        }
    finally:
        connection.close()


def get_conversation_summaries(
    username: str
):
    user = get_user_by_username(
        username
    )

    if not user:
        return []

    user_id = user["id"]

    connection = get_database_connection()

    try:
        rows = connection.execute(
            """
            SELECT
                dc.id AS conversation_id,

                CASE
                    WHEN dc.user1_id = ?
                    THEN user2.username
                    ELSE user1.username
                END AS other_username,

                latest_message.id
                    AS latest_message_id,

                latest_message.message
                    AS latest_message,

                latest_message.created_at
                    AS latest_timestamp,

                latest_message.deleted_at
                    AS latest_deleted_at,

                latest_message.attachment_id
                    AS latest_attachment_id,

                latest_sender.username
                    AS latest_sender,

                (
                    SELECT COUNT(*)
                    FROM direct_messages unread
                    WHERE unread.conversation_id = dc.id
                      AND unread.sender_id != ?
                      AND unread.read_at IS NULL
                      AND unread.deleted_at IS NULL
                ) AS unread_count

            FROM direct_conversations dc

            JOIN users user1
                ON user1.id = dc.user1_id

            JOIN users user2
                ON user2.id = dc.user2_id

            LEFT JOIN direct_messages latest_message
                ON latest_message.id = (
                    SELECT dm2.id
                    FROM direct_messages dm2
                    WHERE dm2.conversation_id = dc.id
                    ORDER BY dm2.id DESC
                    LIMIT 1
                )

            LEFT JOIN users latest_sender
                ON latest_sender.id =
                    latest_message.sender_id

            WHERE
                dc.user1_id = ?
                OR
                dc.user2_id = ?

            ORDER BY
                COALESCE(
                    latest_message.created_at,
                    dc.created_at
                ) DESC
            """,
            (
                user_id,
                user_id,
                user_id,
                user_id,
            )
        ).fetchall()

        return [
            {
                "conversation_id":
                    row["conversation_id"],

                "username":
                    row["other_username"],

                "latest_message":
                    (
                        "This message was deleted"
                        if row["latest_deleted_at"]
                        else (
                            row["latest_message"]
                            or (
                                "📎 " + upload["original_name"]
                                if (upload := get_upload_by_id(row["latest_attachment_id"]))
                                else None
                            )
                        )
                    ),

                "latest_sender":
                    row["latest_sender"],

                "latest_timestamp":
                    row["latest_timestamp"],

                "unread_count":
                    row["unread_count"],
            }
            for row in rows
        ]
    finally:
        connection.close()


# ============================================================
# REPLIES + REACTIONS
# ============================================================

def _reply_preview_text(message: str, deleted_at, attachment_id):
    if deleted_at:
        return "This message was deleted"

    clean = (message or "").strip()

    if clean:
        return clean

    attachment = get_upload_by_id(attachment_id)

    if attachment:
        return f"📎 {attachment['original_name']}"

    return "Message"


def get_group_reply_target(message_id, room: str):
    if not isinstance(message_id, int):
        return None

    connection = get_database_connection()

    try:
        row = connection.execute(
            """
            SELECT id, username, message, deleted_at, attachment_id
            FROM messages
            WHERE id = ? AND room = ?
            """,
            (message_id, room)
        ).fetchone()

        if not row:
            return None

        return {
            "id": row["id"],
            "sender": row["username"],
            "message": _reply_preview_text(
                row["message"],
                row["deleted_at"],
                row["attachment_id"]
            ),
            "deleted_at": row["deleted_at"],
        }
    finally:
        connection.close()


def get_dm_reply_target(message_id, conversation_id: int):
    if not isinstance(message_id, int) or not conversation_id:
        return None

    connection = get_database_connection()

    try:
        row = connection.execute(
            """
            SELECT
                dm.id,
                u.username AS sender,
                dm.message,
                dm.deleted_at,
                dm.attachment_id
            FROM direct_messages dm
            JOIN users u ON u.id = dm.sender_id
            WHERE dm.id = ? AND dm.conversation_id = ?
            """,
            (message_id, conversation_id)
        ).fetchone()

        if not row:
            return None

        return {
            "id": row["id"],
            "sender": row["sender"],
            "message": _reply_preview_text(
                row["message"],
                row["deleted_at"],
                row["attachment_id"]
            ),
            "deleted_at": row["deleted_at"],
        }
    finally:
        connection.close()


def _reaction_summary(connection, table_name: str, message_id: int):
    rows = connection.execute(
        f"""
        SELECT r.emoji, u.username
        FROM {table_name} r
        JOIN users u ON u.id = r.user_id
        WHERE r.message_id = ?
        ORDER BY r.id
        """,
        (message_id,)
    ).fetchall()

    grouped = {}

    for row in rows:
        grouped.setdefault(row["emoji"], []).append(row["username"])

    return [
        {
            "emoji": emoji,
            "count": len(users),
            "users": users,
        }
        for emoji, users in grouped.items()
    ]


def get_group_reactions(message_id: int):
    connection = get_database_connection()

    try:
        return _reaction_summary(
            connection,
            "message_reactions",
            message_id
        )
    finally:
        connection.close()


def get_dm_reactions(message_id: int):
    connection = get_database_connection()

    try:
        return _reaction_summary(
            connection,
            "direct_message_reactions",
            message_id
        )
    finally:
        connection.close()


def toggle_group_reaction(message_id: int, room: str, username: str, emoji: str):
    if emoji not in ALLOWED_REACTIONS:
        return {"success": False, "message": "Unsupported reaction."}

    user = get_user_by_username(username)

    if not user:
        return {"success": False, "message": "User does not exist."}

    connection = get_database_connection()

    try:
        message = connection.execute(
            """
            SELECT id, deleted_at
            FROM messages
            WHERE id = ? AND room = ?
            """,
            (message_id, room)
        ).fetchone()

        if not message:
            return {"success": False, "message": "Message does not exist."}

        if message["deleted_at"]:
            return {"success": False, "message": "Deleted messages cannot be reacted to."}

        existing = connection.execute(
            """
            SELECT emoji
            FROM message_reactions
            WHERE message_id = ? AND user_id = ?
            """,
            (message_id, user["id"])
        ).fetchone()

        if existing and existing["emoji"] == emoji:
            connection.execute(
                "DELETE FROM message_reactions WHERE message_id = ? AND user_id = ?",
                (message_id, user["id"])
            )
        elif existing:
            connection.execute(
                """
                UPDATE message_reactions
                SET emoji = ?, created_at = ?
                WHERE message_id = ? AND user_id = ?
                """,
                (emoji, now_iso(), message_id, user["id"])
            )
        else:
            connection.execute(
                """
                INSERT INTO message_reactions(message_id, user_id, emoji, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (message_id, user["id"], emoji, now_iso())
            )

        connection.commit()

        return {
            "success": True,
            "reactions": _reaction_summary(
                connection,
                "message_reactions",
                message_id
            )
        }
    finally:
        connection.close()


def toggle_dm_reaction(message_id: int, username: str, emoji: str):
    if emoji not in ALLOWED_REACTIONS:
        return {"success": False, "message": "Unsupported reaction."}

    user = get_user_by_username(username)

    if not user:
        return {"success": False, "message": "User does not exist."}

    connection = get_database_connection()

    try:
        row = connection.execute(
            """
            SELECT
                dm.id,
                dm.deleted_at,
                dc.user1_id,
                dc.user2_id,
                user1.username AS user1_name,
                user2.username AS user2_name
            FROM direct_messages dm
            JOIN direct_conversations dc ON dc.id = dm.conversation_id
            JOIN users user1 ON user1.id = dc.user1_id
            JOIN users user2 ON user2.id = dc.user2_id
            WHERE dm.id = ?
            """,
            (message_id,)
        ).fetchone()

        if not row:
            return {"success": False, "message": "Message does not exist."}

        if user["id"] not in (row["user1_id"], row["user2_id"]):
            return {"success": False, "message": "You are not part of this conversation."}

        if row["deleted_at"]:
            return {"success": False, "message": "Deleted messages cannot be reacted to."}

        existing = connection.execute(
            """
            SELECT emoji
            FROM direct_message_reactions
            WHERE message_id = ? AND user_id = ?
            """,
            (message_id, user["id"])
        ).fetchone()

        if existing and existing["emoji"] == emoji:
            connection.execute(
                "DELETE FROM direct_message_reactions WHERE message_id = ? AND user_id = ?",
                (message_id, user["id"])
            )
        elif existing:
            connection.execute(
                """
                UPDATE direct_message_reactions
                SET emoji = ?, created_at = ?
                WHERE message_id = ? AND user_id = ?
                """,
                (emoji, now_iso(), message_id, user["id"])
            )
        else:
            connection.execute(
                """
                INSERT INTO direct_message_reactions(message_id, user_id, emoji, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (message_id, user["id"], emoji, now_iso())
            )

        connection.commit()

        other_username = (
            row["user2_name"]
            if row["user1_id"] == user["id"]
            else row["user1_name"]
        )

        return {
            "success": True,
            "reactions": _reaction_summary(
                connection,
                "direct_message_reactions",
                message_id
            ),
            "other_username": other_username,
        }
    finally:
        connection.close()


# ============================================================
# JWT
# ============================================================

def create_access_token(
    username: str
):
    expire = datetime.now(
        timezone.utc
    ) + timedelta(
        minutes=ACCESS_TOKEN_EXPIRE_MINUTES
    )

    payload = {
        "sub": username,
        "exp": expire,
    }

    return jwt.encode(
        payload,
        SECRET_KEY,
        algorithm=ALGORITHM
    )


def decode_access_token(
    token: str
):
    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )

        return payload.get("sub")
    except jwt.InvalidTokenError:
        return None


# ============================================================
# CONNECTION MANAGER
# ============================================================

class ConnectionManager:
    def __init__(self):
        self.rooms = {}
        self.user_connections = {}

    async def connect(
        self,
        websocket: WebSocket,
        room: str,
        username: str
    ):
        already_in_room = (
            room in self.rooms
            and
            username in self.rooms[room].values()
        )

        await websocket.accept()

        if room not in self.rooms:
            self.rooms[room] = {}

        self.rooms[room][websocket] = username

        if username not in self.user_connections:
            self.user_connections[username] = set()

        self.user_connections[
            username
        ].add(websocket)

        print(
            f"{username} connected to room {room}"
        )

        return not already_in_room

    def disconnect(
        self,
        websocket: WebSocket,
        room: str
    ):
        username = None

        if room in self.rooms:
            username = self.rooms[
                room
            ].pop(
                websocket,
                None
            )

            if not self.rooms[room]:
                del self.rooms[room]

        if username:
            connections = (
                self.user_connections.get(
                    username
                )
            )

            if connections is not None:
                connections.discard(
                    websocket
                )

                if not connections:
                    del self.user_connections[
                        username
                    ]

        return username

    def is_user_online(
        self,
        username: str
    ):
        return username in self.user_connections

    def is_user_in_room(
        self,
        username: str,
        room: str
    ):
        if room not in self.rooms:
            return False

        return username in self.rooms[
            room
        ].values()

    def room_online_count(
        self,
        room: str
    ):
        if room not in self.rooms:
            return 0

        return len(
            set(
                self.rooms[room].values()
            )
        )

    async def send_room_directory(
        self,
        username: str
    ):
        rooms = list_rooms_for_user(
            username
        )

        for room in rooms:
            room["online_count"] = (
                self.room_online_count(
                    room["slug"]
                )
            )

        await self.send_to_user(
            username,
            {
                "type": "room_directory",
                "rooms": rooms,
            }
        )

    async def send_all_room_directories(
        self
    ):
        for username in list(
            self.user_connections.keys()
        ):
            await self.send_room_directory(
                username
            )

    async def kick_user_from_room(
        self,
        username: str,
        room: str,
        reason: str = "Your room access was removed."
    ):
        if room not in self.rooms:
            return

        targets = [
            connection
            for connection, connected_username
            in list(self.rooms[room].items())
            if connected_username == username
        ]

        for connection in targets:
            await self.safe_send(
                connection,
                {
                    "type": "room_access_revoked",
                    "room": room,
                    "message": reason,
                }
            )

            try:
                await connection.close(
                    code=4003,
                    reason=reason
                )
            except Exception:
                pass

    async def safe_send(
        self,
        websocket: WebSocket,
        data: dict
    ):
        try:
            await websocket.send_json(data)
            return True
        except Exception:
            return False

    async def broadcast(
        self,
        room: str,
        data: dict,
        exclude: WebSocket = None
    ):
        if room not in self.rooms:
            return

        for connection in list(
            self.rooms[room].keys()
        ):
            if connection == exclude:
                continue

            await self.safe_send(
                connection,
                data
            )

    async def send_to_user(
        self,
        username: str,
        data: dict
    ):
        connections = (
            self.user_connections.get(
                username,
                set()
            )
        )

        for connection in list(
            connections
        ):
            await self.safe_send(
                connection,
                data
            )

    async def broadcast_all(
        self,
        data: dict
    ):
        connections = set()

        for user_connections in (
            self.user_connections.values()
        ):
            connections.update(
                user_connections
            )

        for connection in connections:
            await self.safe_send(
                connection,
                data
            )

    async def send_online_users(
        self,
        room: str
    ):
        if room not in self.rooms:
            return

        users = list(
            dict.fromkeys(
                self.rooms[
                    room
                ].values()
            )
        )

        await self.broadcast(
            room,
            {
                "type": "users",
                "users": users,
            }
        )

    async def send_user_directory(self):
        usernames = get_all_usernames()
        online_users = set(
            self.user_connections.keys()
        )

        users = [
            {
                "username": username,
                "online":
                    username in online_users,
            }
            for username in usernames
        ]

        await self.broadcast_all(
            {
                "type": "registered_users",
                "users": users,
            }
        )

    async def send_conversation_list(
        self,
        username: str
    ):
        conversations = (
            get_conversation_summaries(
                username
            )
        )

        for conversation in conversations:
            conversation[
                "online"
            ] = self.is_user_online(
                conversation[
                    "username"
                ]
            )

        await self.send_to_user(
            username,
            {
                "type": "dm_conversations",
                "conversations": conversations,
            }
        )

    async def send_all_conversation_lists(
        self
    ):
        for username in list(
            self.user_connections.keys()
        ):
            await self.send_conversation_list(
                username
            )


manager = ConnectionManager()


# ============================================================
# HTTP
# ============================================================

def get_authenticated_http_user(
    authorization: str | None
):
    if (
        not authorization
        or not authorization.startswith("Bearer ")
    ):
        raise HTTPException(
            status_code=401,
            detail="Authentication required."
        )

    token = authorization[7:].strip()
    username = decode_access_token(token)

    if not username:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token."
        )

    user = get_user_by_username(username)

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User does not exist."
        )

    return user


@app.get("/")
async def home():
    return FileResponse(
        BASE_DIR / "index.html"
    )


@app.post("/api/register")
async def register(
    request: RegisterRequest
):
    username = request.username.strip()
    email = request.email.strip().lower()
    password = request.password

    if len(username) < 3:
        return {
            "success": False,
            "message":
                "Username must contain at least 3 characters."
        }

    if " " in username:
        return {
            "success": False,
            "message":
                "Username cannot contain spaces."
        }

    if len(password) < 6:
        return {
            "success": False,
            "message":
                "Password must contain at least 6 characters."
        }

    if "@" not in email:
        return {
            "success": False,
            "message":
                "Please enter a valid email."
        }

    if get_user_by_username(
        username
    ):
        return {
            "success": False,
            "message":
                "Username already exists."
        }

    if get_user_by_email(
        email
    ):
        return {
            "success": False,
            "message":
                "Email already registered."
        }

    hashed_password = (
        password_hash.hash(
            password
        )
    )

    create_user(
        username,
        email,
        hashed_password
    )

    await manager.send_user_directory()

    return {
        "success": True,
        "message":
            "Account created successfully."
    }


@app.post("/api/login")
async def login(
    request: LoginRequest
):
    username = request.username.strip()
    password = request.password

    user = get_user_by_username(
        username
    )

    if not user:
        return {
            "success": False,
            "message":
                "Invalid username or password."
        }

    if not password_hash.verify(
        password,
        user["password_hash"]
    ):
        return {
            "success": False,
            "message":
                "Invalid username or password."
        }

    token = create_access_token(
        user["username"]
    )

    return {
        "success": True,
        "access_token": token,
        "username": user["username"],
    }


# ============================================================
# ROOM MANAGEMENT API
# ============================================================

@app.get("/api/rooms")
async def api_list_rooms(
    authorization: str | None = Header(default=None),
):
    user = get_authenticated_http_user(
        authorization
    )

    rooms = list_rooms_for_user(
        user["username"]
    )

    for room in rooms:
        room["online_count"] = (
            manager.room_online_count(
                room["slug"]
            )
        )

    return {
        "success": True,
        "rooms": rooms,
    }


@app.post("/api/rooms")
async def api_create_room(
    request: RoomCreateRequest,
    authorization: str | None = Header(default=None),
):
    user = get_authenticated_http_user(
        authorization
    )

    result = create_room_record(
        username=user["username"],
        name=request.name,
        description=request.description,
        visibility=request.visibility,
    )

    if not result["success"]:
        raise HTTPException(
            status_code=400,
            detail=result["message"]
        )

    result["room"]["online_count"] = 0

    await manager.send_all_room_directories()

    return result


@app.post("/api/rooms/{slug}/join")
async def api_join_room(
    slug: str,
    authorization: str | None = Header(default=None),
):
    user = get_authenticated_http_user(
        authorization
    )

    result = join_room_record(
        user["username"],
        slug
    )

    if not result["success"]:
        status_code = (
            403
            if "private" in result["message"].lower()
            else 404
        )
        raise HTTPException(
            status_code=status_code,
            detail=result["message"]
        )

    room = dict(result["room"])
    room["role"] = result["role"]
    room["online_count"] = (
        manager.room_online_count(slug)
    )

    await manager.send_all_room_directories()

    return {
        "success": True,
        "room": room,
    }


@app.post("/api/rooms/{slug}/leave")
async def api_leave_room(
    slug: str,
    authorization: str | None = Header(default=None),
):
    user = get_authenticated_http_user(
        authorization
    )

    result = leave_room_record(
        user["username"],
        slug
    )

    if not result["success"]:
        raise HTTPException(
            status_code=400,
            detail=result["message"]
        )

    await manager.kick_user_from_room(
        user["username"],
        slug,
        "You left this room."
    )

    await manager.send_all_room_directories()

    return result


@app.get("/api/rooms/{slug}/members")
async def api_room_members(
    slug: str,
    authorization: str | None = Header(default=None),
):
    user = get_authenticated_http_user(
        authorization
    )

    result = get_room_members(
        user["username"],
        slug
    )

    if not result["success"]:
        raise HTTPException(
            status_code=403,
            detail=result["message"]
        )

    for member in result["members"]:
        member["online"] = (
            manager.is_user_online(
                member["username"]
            )
        )

    return result


@app.post("/api/rooms/{slug}/members")
async def api_add_room_member(
    slug: str,
    request: RoomMemberRequest,
    authorization: str | None = Header(default=None),
):
    user = get_authenticated_http_user(
        authorization
    )

    target_username = request.username.strip()

    result = add_member_to_room(
        user["username"],
        slug,
        target_username
    )

    if not result["success"]:
        raise HTTPException(
            status_code=400,
            detail=result["message"]
        )

    await manager.send_room_directory(
        target_username
    )
    await manager.send_room_directory(
        user["username"]
    )

    return result


@app.delete("/api/rooms/{slug}/members/{target_username}")
async def api_remove_room_member(
    slug: str,
    target_username: str,
    authorization: str | None = Header(default=None),
):
    user = get_authenticated_http_user(
        authorization
    )

    result = remove_member_from_room(
        user["username"],
        slug,
        target_username
    )

    if not result["success"]:
        raise HTTPException(
            status_code=400,
            detail=result["message"]
        )

    await manager.kick_user_from_room(
        target_username,
        slug,
        "The room owner removed you from this room."
    )

    await manager.send_all_room_directories()

    return result


# ============================================================
# FILE / IMAGE UPLOAD
# ============================================================

@app.post("/api/upload")
async def upload_attachment(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    if (
        not authorization
        or not authorization.startswith("Bearer ")
    ):
        raise HTTPException(
            status_code=401,
            detail="Authentication required."
        )

    token = authorization[7:].strip()
    username = decode_access_token(token)

    if not username:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token."
        )

    user = get_user_by_username(username)

    if not user:
        raise HTTPException(
            status_code=401,
            detail="User does not exist."
        )

    original_name = Path(
        file.filename or "attachment"
    ).name

    extension = Path(
        original_name
    ).suffix.lower()

    mime_type = (
        file.content_type
        or "application/octet-stream"
    ).lower()

    allowed_extensions = ALLOWED_UPLOAD_TYPES.get(
        mime_type
    )

    if (
        not allowed_extensions
        or extension not in allowed_extensions
    ):
        raise HTTPException(
            status_code=415,
            detail=(
                "Unsupported file type. Allowed: JPG, PNG, GIF, "
                "WEBP, PDF, TXT, CSV, DOCX, XLSX and PPTX."
            )
        )

    upload_id = uuid.uuid4().hex
    stored_name = upload_id + extension
    destination = UPLOAD_DIR / stored_name

    size = 0

    try:
        with destination.open("wb") as output:
            while True:
                chunk = await file.read(
                    1024 * 1024
                )

                if not chunk:
                    break

                size += len(chunk)

                if size > MAX_UPLOAD_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail="File is larger than 10 MB."
                    )

                output.write(chunk)

    except Exception:
        if destination.exists():
            destination.unlink()
        raise

    finally:
        await file.close()

    save_upload_record(
        upload_id=upload_id,
        owner_id=user["id"],
        stored_name=stored_name,
        original_name=original_name,
        mime_type=mime_type,
        size=size,
    )

    return get_upload_by_id(
        upload_id
    )



# ============================================================
# SEARCH + PAGINATION
# ============================================================

def get_room_messages_before(room: str, before_id: int | None, limit: int = 50):
    limit = max(1, min(int(limit), 100))
    connection = get_database_connection()
    try:
        if before_id is None:
            rows = connection.execute(
                """
                SELECT id, username, message, created_at, edited_at, deleted_at,
                       attachment_id, reply_to_id
                FROM messages
                WHERE room = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (room, limit)
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT id, username, message, created_at, edited_at, deleted_at,
                       attachment_id, reply_to_id
                FROM messages
                WHERE room = ? AND id < ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (room, before_id, limit)
            ).fetchall()

        rows = list(reversed(rows))
        result = []
        for row in rows:
            item = {
                "id": row["id"],
                "username": row["username"],
                "message": "This message was deleted" if row["deleted_at"] else row["message"],
                "timestamp": row["created_at"],
                "edited_at": row["edited_at"],
                "deleted_at": row["deleted_at"],
                "attachment": get_upload_by_id(row["attachment_id"]) if row["attachment_id"] else None,
                "reply_to": None,
                "reactions": [],
            }
            if row["reply_to_id"]:
                reply_row = connection.execute(
                    """
                    SELECT id, username, message, deleted_at, attachment_id
                    FROM messages
                    WHERE id = ?
                    """,
                    (row["reply_to_id"],)
                ).fetchone()
                if reply_row:
                    item["reply_to"] = {
                        "id": reply_row["id"],
                        "sender": reply_row["username"],
                        "message": (
                            "This message was deleted"
                            if reply_row["deleted_at"]
                            else reply_row["message"]
                        ),
                    }
            result.append(item)
        return result
    finally:
        connection.close()


@app.get("/api/rooms/{slug}/messages")
async def api_room_messages(
    slug: str,
    before_id: int | None = None,
    limit: int = 50,
    authorization: str | None = Header(default=None),
):
    user = get_authenticated_http_user(authorization)
    access = can_access_room(
        user["username"],
        slug,
        auto_join_public=True,
    )
    if not access["success"]:
        raise HTTPException(
            status_code=403 if access.get("reason") != "not_found" else 404,
            detail=access["message"],
        )

    messages = get_room_messages_before(slug, before_id, limit)
    return {
        "success": True,
        "messages": messages,
        "has_more": len(messages) == min(max(1, min(int(limit), 100)), len(messages)),
    }


@app.get("/api/search/messages")
async def api_search_messages(
    q: str = "",
    room: str | None = None,
    limit: int = 40,
    authorization: str | None = Header(default=None),
):
    user = get_authenticated_http_user(authorization)
    query = q.strip()
    if len(query) < 2:
        return {"success": True, "results": []}

    limit = max(1, min(int(limit), 100))
    connection = get_database_connection()
    try:
        if room:
            access = can_access_room(user["username"], room, auto_join_public=False)
            if not access["success"]:
                raise HTTPException(status_code=403, detail="You cannot search this room.")

            rows = connection.execute(
                """
                SELECT id, room, username, message, created_at, deleted_at
                FROM messages
                WHERE room = ?
                  AND message LIKE ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (room, f"%{query}%", limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT m.id, m.room, m.username, m.message, m.created_at, m.deleted_at
                FROM messages m
                INNER JOIN room_members rm ON rm.room_id = (
                    SELECT id FROM rooms WHERE slug = m.room
                )
                INNER JOIN users u ON u.id = rm.user_id
                WHERE u.username = ?
                  AND m.message LIKE ?
                ORDER BY m.id DESC
                LIMIT ?
                """,
                (user["username"], f"%{query}%", limit),
            ).fetchall()

        seen = set()
        results = []
        for row in rows:
            key = (row["room"], row["id"])
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "type": "room",
                "id": row["id"],
                "room": row["room"],
                "username": row["username"],
                "message": "This message was deleted" if row["deleted_at"] else row["message"],
                "timestamp": row["created_at"],
                "deleted": bool(row["deleted_at"]),
            })
        return {"success": True, "results": results}
    finally:
        connection.close()


@app.get("/api/search/dms")
async def api_search_dms(
    q: str = "",
    with_user: str | None = None,
    limit: int = 40,
    authorization: str | None = Header(default=None),
):
    user = get_authenticated_http_user(authorization)
    query = q.strip()
    if len(query) < 2:
        return {"success": True, "results": []}

    limit = max(1, min(int(limit), 100))
    connection = get_database_connection()
    try:
        me = connection.execute(
            "SELECT id FROM users WHERE username = ?",
            (user["username"],),
        ).fetchone()
        if not me:
            raise HTTPException(status_code=401, detail="User does not exist.")

        sql = """
            SELECT dm.id, dm.conversation_id, dm.sender_id, dm.message,
                   dm.created_at, dm.deleted_at,
                   u.username AS sender_username,
                   CASE
                       WHEN dc.user1_id = ? THEN u2.username
                       ELSE u1.username
                   END AS other_username
            FROM direct_messages dm
            INNER JOIN direct_conversations dc ON dc.id = dm.conversation_id
            INNER JOIN users u ON u.id = dm.sender_id
            INNER JOIN users u1 ON u1.id = dc.user1_id
            INNER JOIN users u2 ON u2.id = dc.user2_id
            WHERE (dc.user1_id = ? OR dc.user2_id = ?)
              AND dm.message LIKE ?
        """
        params = [me["id"], me["id"], me["id"], f"%{query}%"]
        if with_user:
            sql += """
                AND (
                    (dc.user1_id = ? AND u2.username = ?)
                    OR
                    (dc.user2_id = ? AND u1.username = ?)
                )
            """
            params.extend([me["id"], with_user, me["id"], with_user])
        sql += " ORDER BY dm.id DESC LIMIT ?"
        params.append(limit)

        rows = connection.execute(sql, tuple(params)).fetchall()
        return {
            "success": True,
            "results": [
                {
                    "type": "dm",
                    "id": row["id"],
                    "conversation_id": row["conversation_id"],
                    "sender": row["sender_username"],
                    "other_username": row["other_username"],
                    "message": "This message was deleted" if row["deleted_at"] else row["message"],
                    "timestamp": row["created_at"],
                    "deleted": bool(row["deleted_at"]),
                }
                for row in rows
            ],
        }
    finally:
        connection.close()


# ============================================================
# WEBSOCKET
# ============================================================

@app.websocket("/ws/{room}")
async def websocket_endpoint(
    websocket: WebSocket,
    room: str
):
    token = websocket.query_params.get(
        "token"
    )

    if not token:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Authentication required"
        )

    username = decode_access_token(
        token
    )

    if not username:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Invalid or expired token"
        )

    user = get_user_by_username(
        username
    )

    if not user:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="User does not exist"
        )

    room_access = can_access_room(
        username,
        room,
        auto_join_public=True
    )

    if not room_access["success"]:
        close_code = (
            4004
            if room_access.get("reason") == "not_found"
            else 4003
        )
        raise WebSocketException(
            code=close_code,
            reason=room_access["message"]
        )

    room_details = room_access["room"]
    room_role = room_access["role"]

    first_connection_in_room = (
        await manager.connect(
            websocket,
            room,
            username
        )
    )

    await websocket.send_json(
        {
            "type": "room_info",
            "room": {
                "id": room_details["id"],
                "slug": room_details["slug"],
                "name": room_details["name"],
                "description": room_details["description"],
                "visibility": room_details["visibility"],
                "owner_username": room_details["owner_username"],
                "role": room_role,
                "online_count": manager.room_online_count(room),
            }
        }
    )

    history = get_message_history(
        room,
        limit=50
    )

    await websocket.send_json(
        {
            "type": "history",
            "messages": history,
            "has_more": len(history) >= 50,
        }
    )

    if first_connection_in_room:
        await manager.broadcast(
            room,
            {
                "type": "system",
                "message":
                    f"{username} joined the room",
                "timestamp": now_iso(),
            }
        )

    await manager.send_online_users(
        room
    )

    await manager.send_user_directory()

    await manager.send_all_conversation_lists()

    await manager.send_all_room_directories()

    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")

            # =================================================
            # GROUP MESSAGE
            # =================================================

            if event_type == "message":
                message = (
                    data.get(
                        "message",
                        ""
                    )
                    .strip()
                )

                attachment_id = data.get(
                    "attachment_id"
                )

                attachment = None

                if attachment_id:
                    if not isinstance(
                        attachment_id,
                        str
                    ):
                        await websocket.send_json(
                            {
                                "type": "message_error",
                                "message":
                                    "Invalid attachment."
                            }
                        )
                        continue

                    attachment = get_owned_upload(
                        attachment_id,
                        username
                    )

                    if not attachment:
                        await websocket.send_json(
                            {
                                "type": "message_error",
                                "message":
                                    "Attachment does not exist or is not yours."
                            }
                        )
                        continue

                if not message and not attachment:
                    continue

                reply_to_id = data.get("reply_to_id")
                reply_to = None

                if reply_to_id is not None:
                    if not isinstance(reply_to_id, int):
                        await websocket.send_json(
                            {
                                "type": "message_error",
                                "message": "Invalid reply target."
                            }
                        )
                        continue

                    reply_to = get_group_reply_target(
                        reply_to_id,
                        room
                    )

                    if not reply_to or reply_to["deleted_at"]:
                        await websocket.send_json(
                            {
                                "type": "message_error",
                                "message": "Reply target is unavailable."
                            }
                        )
                        continue

                timestamp = now_iso()

                message_id = save_message(
                    room,
                    username,
                    message,
                    timestamp,
                    attachment_id=(
                        attachment["id"]
                        if attachment
                        else None
                    ),
                    reply_to_id=reply_to_id,
                )

                await manager.broadcast(
                    room,
                    {
                        "type": "message",
                        "id": message_id,
                        "username": username,
                        "message": message,
                        "timestamp": timestamp,
                        "edited_at": None,
                        "deleted_at": None,
                        "attachment": attachment,
                        "reply_to": reply_to,
                        "reactions": [],
                    }
                )

            # =================================================
            # GROUP EDIT
            # =================================================

            elif event_type == "edit_message":
                message_id = data.get(
                    "message_id"
                )

                new_message = (
                    data.get(
                        "message",
                        ""
                    )
                    .strip()
                )

                if not isinstance(
                    message_id,
                    int
                ):
                    await websocket.send_json(
                        {
                            "type":
                                "message_error",
                            "message":
                                "Invalid message id."
                        }
                    )
                    continue

                if not new_message:
                    await websocket.send_json(
                        {
                            "type":
                                "message_error",
                            "message":
                                "Edited message cannot be empty."
                        }
                    )
                    continue

                result = edit_group_message(
                    message_id,
                    room,
                    username,
                    new_message
                )

                if not result["success"]:
                    await websocket.send_json(
                        {
                            "type":
                                "message_error",
                            "message":
                                result["message"]
                        }
                    )
                    continue

                await manager.broadcast(
                    room,
                    {
                        "type":
                            "message_edited",
                        "id":
                            message_id,
                        "message":
                            new_message,
                        "edited_at":
                            result["edited_at"],
                    }
                )

            # =================================================
            # GROUP DELETE
            # =================================================

            elif event_type == "delete_message":
                message_id = data.get(
                    "message_id"
                )

                if not isinstance(
                    message_id,
                    int
                ):
                    await websocket.send_json(
                        {
                            "type":
                                "message_error",
                            "message":
                                "Invalid message id."
                        }
                    )
                    continue

                result = delete_group_message(
                    message_id,
                    room,
                    username
                )

                if not result["success"]:
                    await websocket.send_json(
                        {
                            "type":
                                "message_error",
                            "message":
                                result["message"]
                        }
                    )
                    continue

                await manager.broadcast(
                    room,
                    {
                        "type":
                            "message_deleted",
                        "id":
                            message_id,
                        "deleted_at":
                            result["deleted_at"],
                    }
                )

            # =================================================
            # GROUP REACTION
            # =================================================

            elif event_type == "message_reaction":
                message_id = data.get("message_id")
                emoji = data.get("emoji", "")

                if not isinstance(message_id, int):
                    await websocket.send_json(
                        {
                            "type": "message_error",
                            "message": "Invalid message id."
                        }
                    )
                    continue

                result = toggle_group_reaction(
                    message_id,
                    room,
                    username,
                    emoji
                )

                if not result["success"]:
                    await websocket.send_json(
                        {
                            "type": "message_error",
                            "message": result["message"]
                        }
                    )
                    continue

                await manager.broadcast(
                    room,
                    {
                        "type": "message_reactions_updated",
                        "id": message_id,
                        "reactions": result["reactions"],
                    }
                )

            # =================================================
            # GROUP TYPING
            # =================================================

            elif event_type == "typing":
                await manager.broadcast(
                    room,
                    {
                        "type": "typing",
                        "username": username,
                    },
                    exclude=websocket
                )

            elif event_type == "stop_typing":
                await manager.broadcast(
                    room,
                    {
                        "type": "stop_typing",
                        "username": username,
                    },
                    exclude=websocket
                )

            # =================================================
            # LOAD DM HISTORY
            # =================================================

            elif event_type == "load_dm_history":
                other_username = (
                    data.get(
                        "username",
                        ""
                    )
                    .strip()
                )

                if (
                    not other_username
                    or
                    other_username == username
                ):
                    await websocket.send_json(
                        {
                            "type": "dm_error",
                            "message":
                                "Invalid private-chat user."
                        }
                    )
                    continue

                other_user = (
                    get_user_by_username(
                        other_username
                    )
                )

                if not other_user:
                    await websocket.send_json(
                        {
                            "type": "dm_error",
                            "message":
                                "User does not exist."
                        }
                    )
                    continue

                read_result = (
                    mark_direct_messages_read(
                        reader_username=
                            username,
                        other_username=
                            other_username
                    )
                )

                dm_history = (
                    get_direct_message_history(
                        username,
                        other_username,
                        limit=50
                    )
                )

                await websocket.send_json(
                    {
                        "type": "dm_history",
                        "with_user":
                            other_username,
                        "messages":
                            dm_history,
                    }
                )

                if read_result[
                    "message_ids"
                ]:
                    await manager.send_to_user(
                        other_username,
                        {
                            "type": "dm_read",
                            "reader": username,
                            "message_ids":
                                read_result[
                                    "message_ids"
                                ],
                            "read_at":
                                read_result[
                                    "read_at"
                                ],
                        }
                    )

                await manager.send_conversation_list(
                    username
                )

                await manager.send_conversation_list(
                    other_username
                )

            # =================================================
            # SEND DM
            # =================================================

            elif event_type == "dm_message":
                recipient = (
                    data.get(
                        "recipient",
                        ""
                    )
                    .strip()
                )

                message = (
                    data.get(
                        "message",
                        ""
                    )
                    .strip()
                )

                attachment_id = data.get(
                    "attachment_id"
                )

                attachment = None

                if attachment_id:
                    if not isinstance(
                        attachment_id,
                        str
                    ):
                        await websocket.send_json(
                            {
                                "type": "dm_error",
                                "message":
                                    "Invalid attachment."
                            }
                        )
                        continue

                    attachment = get_owned_upload(
                        attachment_id,
                        username
                    )

                    if not attachment:
                        await websocket.send_json(
                            {
                                "type": "dm_error",
                                "message":
                                    "Attachment does not exist or is not yours."
                            }
                        )
                        continue

                if not recipient or (
                    not message and not attachment
                ):
                    continue

                if recipient == username:
                    await websocket.send_json(
                        {
                            "type": "dm_error",
                            "message":
                                "You cannot message yourself."
                        }
                    )
                    continue

                recipient_user = (
                    get_user_by_username(
                        recipient
                    )
                )

                if not recipient_user:
                    await websocket.send_json(
                        {
                            "type": "dm_error",
                            "message":
                                "Recipient does not exist."
                        }
                    )
                    continue

                reply_to_id = data.get("reply_to_id")
                reply_to = None

                if reply_to_id is not None:
                    if not isinstance(reply_to_id, int):
                        await websocket.send_json(
                            {
                                "type": "dm_error",
                                "message": "Invalid reply target."
                            }
                        )
                        continue

                    conversation_id = get_conversation_id(
                        user["id"],
                        recipient_user["id"],
                        create_if_missing=False
                    )

                    reply_to = (
                        get_dm_reply_target(
                            reply_to_id,
                            conversation_id
                        )
                        if conversation_id
                        else None
                    )

                    if not reply_to or reply_to["deleted_at"]:
                        await websocket.send_json(
                            {
                                "type": "dm_error",
                                "message": "Reply target is unavailable."
                            }
                        )
                        continue

                timestamp = now_iso()

                saved = save_direct_message(
                    sender_username=
                        username,
                    recipient_username=
                        recipient,
                    message=
                        message,
                    created_at=
                        timestamp,
                    attachment_id=(
                        attachment["id"]
                        if attachment
                        else None
                    ),
                    reply_to_id=reply_to_id,
                )

                payload = {
                    "type": "dm_message",
                    "id":
                        saved["message_id"],
                    "conversation_id":
                        saved["conversation_id"],
                    "sender":
                        username,
                    "recipient":
                        recipient,
                    "message":
                        message,
                    "timestamp":
                        timestamp,
                    "read_at":
                        None,
                    "edited_at":
                        None,
                    "deleted_at":
                        None,
                    "attachment":
                        attachment,
                    "reply_to":
                        reply_to,
                    "reactions":
                        [],
                }

                await manager.send_to_user(
                    username,
                    payload
                )

                await manager.send_to_user(
                    recipient,
                    payload
                )

                await manager.send_conversation_list(
                    username
                )

                await manager.send_conversation_list(
                    recipient
                )

            # =================================================
            # DM REACTION
            # =================================================

            elif event_type == "dm_reaction":
                message_id = data.get("message_id")
                emoji = data.get("emoji", "")

                if not isinstance(message_id, int):
                    await websocket.send_json(
                        {
                            "type": "dm_error",
                            "message": "Invalid message id."
                        }
                    )
                    continue

                result = toggle_dm_reaction(
                    message_id,
                    username,
                    emoji
                )

                if not result["success"]:
                    await websocket.send_json(
                        {
                            "type": "dm_error",
                            "message": result["message"]
                        }
                    )
                    continue

                payload = {
                    "type": "dm_reactions_updated",
                    "id": message_id,
                    "reactions": result["reactions"],
                }

                await manager.send_to_user(
                    username,
                    payload
                )

                await manager.send_to_user(
                    result["other_username"],
                    payload
                )

            # =================================================
            # EDIT DM
            # =================================================

            elif event_type == "dm_edit":
                message_id = data.get(
                    "message_id"
                )

                new_message = (
                    data.get(
                        "message",
                        ""
                    )
                    .strip()
                )

                if not isinstance(
                    message_id,
                    int
                ):
                    await websocket.send_json(
                        {
                            "type": "dm_error",
                            "message":
                                "Invalid message id."
                        }
                    )
                    continue

                if not new_message:
                    await websocket.send_json(
                        {
                            "type": "dm_error",
                            "message":
                                "Edited message cannot be empty."
                        }
                    )
                    continue

                result = edit_direct_message(
                    message_id,
                    username,
                    new_message
                )

                if not result["success"]:
                    await websocket.send_json(
                        {
                            "type": "dm_error",
                            "message":
                                result["message"]
                        }
                    )
                    continue

                payload = {
                    "type": "dm_edited",
                    "id": message_id,
                    "message": new_message,
                    "edited_at":
                        result["edited_at"],
                }

                await manager.send_to_user(
                    username,
                    payload
                )

                await manager.send_to_user(
                    result["recipient"],
                    payload
                )

                await manager.send_conversation_list(
                    username
                )

                await manager.send_conversation_list(
                    result["recipient"]
                )

            # =================================================
            # DELETE DM
            # =================================================

            elif event_type == "dm_delete":
                message_id = data.get(
                    "message_id"
                )

                if not isinstance(
                    message_id,
                    int
                ):
                    await websocket.send_json(
                        {
                            "type": "dm_error",
                            "message":
                                "Invalid message id."
                        }
                    )
                    continue

                result = delete_direct_message(
                    message_id,
                    username
                )

                if not result["success"]:
                    await websocket.send_json(
                        {
                            "type": "dm_error",
                            "message":
                                result["message"]
                        }
                    )
                    continue

                payload = {
                    "type": "dm_deleted",
                    "id": message_id,
                    "deleted_at":
                        result["deleted_at"],
                }

                await manager.send_to_user(
                    username,
                    payload
                )

                await manager.send_to_user(
                    result["recipient"],
                    payload
                )

                await manager.send_conversation_list(
                    username
                )

                await manager.send_conversation_list(
                    result["recipient"]
                )

            # =================================================
            # MARK DM READ
            # =================================================

            elif event_type == "dm_mark_read":
                other_username = (
                    data.get(
                        "username",
                        ""
                    )
                    .strip()
                )

                if (
                    not other_username
                    or
                    other_username == username
                ):
                    continue

                if not get_user_by_username(
                    other_username
                ):
                    continue

                read_result = (
                    mark_direct_messages_read(
                        reader_username=
                            username,
                        other_username=
                            other_username
                    )
                )

                if read_result[
                    "message_ids"
                ]:
                    await manager.send_to_user(
                        other_username,
                        {
                            "type": "dm_read",
                            "reader": username,
                            "message_ids":
                                read_result[
                                    "message_ids"
                                ],
                            "read_at":
                                read_result[
                                    "read_at"
                                ],
                        }
                    )

                await manager.send_conversation_list(
                    username
                )

                await manager.send_conversation_list(
                    other_username
                )

            # =================================================
            # DM TYPING
            # =================================================

            elif event_type == "dm_typing":
                recipient = data.get(
                    "recipient"
                )

                if recipient:
                    await manager.send_to_user(
                        recipient,
                        {
                            "type": "dm_typing",
                            "username": username,
                        }
                    )

            elif event_type == "dm_stop_typing":
                recipient = data.get(
                    "recipient"
                )

                if recipient:
                    await manager.send_to_user(
                        recipient,
                        {
                            "type":
                                "dm_stop_typing",
                            "username":
                                username,
                        }
                    )

    except WebSocketDisconnect:
        disconnected_username = (
            manager.disconnect(
                websocket,
                room
            )
        )

        if disconnected_username:
            if not manager.is_user_in_room(
                disconnected_username,
                room
            ):
                await manager.broadcast(
                    room,
                    {
                        "type":
                            "stop_typing",
                        "username":
                            disconnected_username,
                    }
                )

                await manager.broadcast(
                    room,
                    {
                        "type": "system",
                        "message":
                            f"{disconnected_username} left the room",
                        "timestamp":
                            now_iso(),
                    }
                )

            await manager.send_online_users(
                room
            )

            if not manager.is_user_online(
                disconnected_username
            ):
                await manager.broadcast_all(
                    {
                        "type":
                            "dm_stop_typing",
                        "username":
                            disconnected_username,
                    }
                )

            await manager.send_user_directory()

            await manager.send_all_conversation_lists()

            await manager.send_all_room_directories()