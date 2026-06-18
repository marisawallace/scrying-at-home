#!/usr/bin/env python3
"""
Unified sync script for chat archives (Claude, ChatGPT, etc.)

This script extracts chat export zip files and organizes conversations
into a structured local archive organized by provider and user email.

Supports:
- Claude.ai exports (data-*.zip)
- ChatGPT exports ([hex]-YYYY-MM-DD-HH-MM-SS-[hex].zip)
"""

import argparse
import json
import re
import sys
import zipfile
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Set, Tuple

from scrying_at_home.config.paths import REPO_ROOT, add_config_arg, load_env_or_exit, resolve_data_dir, resolve_archived_exports_dir
from scrying_at_home.common.timestamps import to_utc_iso
from scrying_at_home.common.ansi import muted, warning, success, error


# ============================================================================
# Coloured terminal output
# ============================================================================
# Thin wrappers over the shared palette (scrying_at_home.common.ansi) so the
# many call sites below stay readable. ``emit_error``/``emit_warn`` also route
# to stderr — this module historically printed everything (errors included) to
# stdout, which was an oversight, not a deliberate choice.

def emit_error(msg: str) -> None:
    """Failure line → red, stderr (the nonzero exit code carries the signal)."""
    print(error(msg, stream=sys.stderr), file=sys.stderr)


def emit_warn(msg: str) -> None:
    """Non-fatal notice → orange, stderr."""
    print(warning(msg, stream=sys.stderr), file=sys.stderr)


def emit_info(msg: str) -> None:
    """De-emphasised status/progress line → dim, stdout."""
    print(muted(msg))


def emit_detail(msg: str) -> None:
    """Dim continuation line that accompanies an error/warning → stderr.

    Keeps a multi-line error block on one stream without painting every line
    red — only the leading ``emit_error`` line carries the alarm colour.
    """
    print(muted(msg, stream=sys.stderr), file=sys.stderr)


def emit_success(msg: str) -> None:
    """Completion line → green, stdout."""
    print(success(msg))


# ============================================================================
# Shared utility functions
# ============================================================================

def sanitize_name(name: str) -> str:
    """
    Convert a conversation/project name to a filesystem-safe name.
    Removes all non-alphanumeric characters and replaces spaces with hyphens.
    """
    if not name:
        return "untitled"
    # Replace spaces with hyphens
    name = name.replace(" ", "-")
    # Keep only alphanumeric characters and hyphens
    name = re.sub(r"[^a-zA-Z0-9-]", "", name)
    # Collapse multiple consecutive hyphens
    name = re.sub(r"-+", "-", name)
    # Strip leading/trailing hyphens
    name = name.strip("-")
    return name if name else "untitled"


def format_date(date_str: str) -> str:
    """
    Extract date in YYYY-MM-DD format from ISO timestamp or Unix timestamp.
    Handles both ISO format strings and numeric Unix timestamps.
    """
    try:
        # Handle Unix timestamp (float or int). UTC, to match the stored ISO
        # timestamps (which are UTC) so a filename date never disagrees with the
        # created_at it is derived from.
        if isinstance(date_str, (int, float)):
            dt = datetime.fromtimestamp(date_str, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d")

        # Handle ISO format string
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception as e:
        emit_warn(f"Warning: Could not parse date '{date_str}': {e}")
        return "unknown-date"


def build_filename(created_at: str, name: str) -> str:
    """
    Build a filename from creation date and name.
    Format: YYYY-MM-DD_sanitized-name
    """
    date_part = format_date(created_at)
    name_part = sanitize_name(name)
    return f"{date_part}_{name_part}"


def build_unique_filenames(items: List[Dict]) -> Dict[str, str]:
    """
    Build unique filenames for all items, adding numeric suffixes if needed.

    Returns a mapping from UUID to unique filename (without .json extension).
    """
    # First pass: group items by their base filename
    filename_to_items: Dict[str, List[Tuple[str, Dict]]] = {}

    for item in items:
        uuid = item.get("uuid")
        created_at = item.get("created_at")
        name = item.get("name", "")

        base_filename = build_filename(created_at, name)

        if base_filename not in filename_to_items:
            filename_to_items[base_filename] = []
        filename_to_items[base_filename].append((uuid, item))

    # Second pass: assign unique filenames
    uuid_to_filename: Dict[str, str] = {}

    for base_filename, items_list in filename_to_items.items():
        if len(items_list) == 1:
            # No conflict, use base filename
            uuid = items_list[0][0]
            uuid_to_filename[uuid] = base_filename
        else:
            # Conflict: add numeric suffixes
            # Sort by created_at to ensure consistent ordering
            items_list.sort(key=lambda x: x[1].get("created_at", ""))

            for i, (uuid, item) in enumerate(items_list, start=1):
                unique_filename = f"{base_filename}-{i}"
                uuid_to_filename[uuid] = unique_filename

    return uuid_to_filename


# ============================================================================
# Validation functions (kept at module level for test imports)
# ============================================================================

def validate_claude_export_format(users_data: List, conversations_data: List, projects_data: List) -> None:
    """
    Validate that the Claude export data matches expected format.

    Raises SystemExit with helpful message if format is unexpected.
    """
    # Check users.json structure
    if not users_data or not isinstance(users_data, list):
        emit_error("ERROR: Invalid export format - users.json should contain a list of users")
        emit_detail("This export file may be corrupted or from an incompatible Claude version.")
        sys.exit(1)

    user = users_data[0]
    if not isinstance(user, dict):
        emit_error("ERROR: Invalid export format - user data should be a dictionary")
        sys.exit(1)

    required_user_fields = ["email_address", "uuid"]
    missing_user_fields = [f for f in required_user_fields if f not in user]
    if missing_user_fields:
        emit_error(f"ERROR: Invalid export format - user data missing required fields: {', '.join(missing_user_fields)}")
        emit_detail("Expected fields: email_address, uuid")
        emit_detail("This export may be from an incompatible Claude version.")
        sys.exit(1)

    # Check conversations.json structure
    if not isinstance(conversations_data, list):
        emit_error("ERROR: Invalid export format - conversations.json should contain a list")
        sys.exit(1)

    if len(conversations_data) > 0:
        # Check first conversation has expected structure
        conv = conversations_data[0]
        if not isinstance(conv, dict):
            emit_error("ERROR: Invalid export format - conversation should be a dictionary")
            sys.exit(1)

        required_conv_fields = ["uuid", "name", "created_at", "account", "chat_messages"]
        missing_conv_fields = [f for f in required_conv_fields if f not in conv]
        if missing_conv_fields:
            emit_error(f"ERROR: Invalid export format - conversation missing required fields: {', '.join(missing_conv_fields)}")
            emit_detail("Expected fields: uuid, name, created_at, account, chat_messages")
            emit_detail("This export may be from an incompatible Claude version.")
            emit_detail("\nIf Claude.ai has changed their export format, please report this issue.")
            sys.exit(1)

    # Check projects.json structure
    if not isinstance(projects_data, list):
        emit_error("ERROR: Invalid export format - projects.json should contain a list")
        sys.exit(1)

    if len(projects_data) > 0:
        # Check first project has expected structure
        proj = projects_data[0]
        if not isinstance(proj, dict):
            emit_error("ERROR: Invalid export format - project should be a dictionary")
            sys.exit(1)

        required_proj_fields = ["uuid", "name", "created_at", "creator"]
        missing_proj_fields = [f for f in required_proj_fields if f not in proj]
        if missing_proj_fields:
            emit_error(f"ERROR: Invalid export format - project missing required fields: {', '.join(missing_proj_fields)}")
            emit_detail("Expected fields: uuid, name, created_at, creator, docs")
            emit_detail("This export may be from an incompatible Claude version.")
            emit_detail("\nIf Claude.ai has changed their export format, please report this issue.")
            sys.exit(1)


def validate_chatgpt_export_format(conversations_data: List) -> None:
    """
    Validate that the ChatGPT export data matches expected format.

    Raises SystemExit with helpful message if format is unexpected.
    """
    # Check conversations.json structure
    if not isinstance(conversations_data, list):
        emit_error("ERROR: Invalid export format - conversations.json should contain a list")
        emit_detail("This export file may be corrupted or from an incompatible ChatGPT version.")
        sys.exit(1)

    if len(conversations_data) > 0:
        # Check first conversation has expected structure
        conv = conversations_data[0]
        if not isinstance(conv, dict):
            emit_error("ERROR: Invalid export format - conversation should be a dictionary")
            sys.exit(1)

        # Check for required fields
        required_fields = ["id", "title", "create_time"]
        missing_fields = [f for f in required_fields if f not in conv]
        if missing_fields:
            emit_error(f"ERROR: Invalid export format - conversation missing required fields: {', '.join(missing_fields)}")
            emit_detail("Expected fields: id, title, create_time")
            emit_detail("This export may be from an incompatible ChatGPT version.")
            emit_detail("\nIf ChatGPT has changed their export format, please report this issue.")
            sys.exit(1)


# ============================================================================
# Provider abstract base class
# ============================================================================

class Provider(ABC):
    """Abstract base class for chat archive providers."""

    def __init__(self, script_dir: Path, config: Dict[str, str]):
        self.script_dir = script_dir
        self.config = config
        self.data_dir = resolve_data_dir(script_dir, config)
        self.archived_exports_base_dir = resolve_archived_exports_dir(script_dir, config)

    @abstractmethod
    def get_provider_name(self) -> str:
        """Return provider name (e.g., 'claude', 'chatgpt')."""
        pass

    @abstractmethod
    def find_zip_files(self, search_dir: Path) -> List[Path]:
        """Find and return list of export zip files in search_dir."""
        pass

    @abstractmethod
    def extract_export_data(self, zip_path: Path) -> Dict:
        """Extract and return export data from zip file."""
        pass

    @abstractmethod
    def validate_export_format(self, export_data: Dict) -> None:
        """Validate export format. Raises SystemExit if invalid."""
        pass

    @abstractmethod
    def get_user_email(self, export_data: Dict) -> str:
        """Extract user email from export data or config."""
        pass

    @abstractmethod
    def get_user_uuid(self, export_data: Dict) -> str:
        """Extract user UUID from export data."""
        pass

    @abstractmethod
    def save_user_data(self, user_dir: Path, export_data: Dict, email: str, user_uuid: str) -> None:
        """Save user.json file in user directory."""
        pass

    @abstractmethod
    def get_conversations(self, export_data: Dict) -> List[Dict]:
        """Extract conversations list from export data."""
        pass

    @abstractmethod
    def get_projects(self, export_data: Dict) -> List[Dict]:
        """Extract projects list from export data (may return empty list)."""
        pass

    @abstractmethod
    def should_delete_existing_conversation(self, existing_data: Dict,
                                           new_uuids: Set[str],
                                           user_uuid: str) -> bool:
        """Determine if an existing conversation file should be deleted."""
        pass

    @abstractmethod
    def should_delete_existing_project(self, existing_data: Dict,
                                       new_uuids: Set[str],
                                       user_uuid: str) -> bool:
        """Determine if an existing project file should be deleted."""
        pass

    def get_user_dir(self, email: str) -> Path:
        """Get user directory path."""
        return self.data_dir / self.get_provider_name() / email

    def get_archived_exports_dir(self, email: str = "") -> Path:
        """Get archived exports directory path, optionally under an email subfolder."""
        base = self.archived_exports_base_dir / self.get_provider_name()
        if email:
            return base / email
        return base


# ============================================================================
# Claude provider implementation
# ============================================================================

"""
Notes on the claude.ai export JSON structure:

{
        "uuid": "1e5b1004-a220-4026-baa1-4d8c3328296b",
        "name": "Local LLM chat multiplexing tools",
        "summary": "",
        "created_at": "2025-10-19T22:48:47.852498Z",
        "updated_at": "2025-10-19T22:49:08.616524Z",
        "account":
        {
            "uuid": "f0f1e818-2d8a-403e-917b-085326c0d358"
        },
        "chat_messages":
        [
            {
                "uuid": "94644b40-ced6-4262-9d31-e4d9ab6def28",
                "text": "what are some existing software tools that enable a user like me to talk to multiple LLMs simultaneously via the command line or a GUI running on my local machine? Ideally these have existing \"connectors\" or interfaces built out to interface with different LLM APIs and support customizing the system prompt, etc. as well as multiplexing chats and saving chats to files on my local filesystem",
                "content":
                [
                    {
                        "start_timestamp": "2025-10-19T22:48:49.355875Z",
                        "stop_timestamp": "2025-10-19T22:48:49.355875Z",
                        "flags": null,
                        "type": "text",
                        "text": "what are some existing software tools that enable a user like me to talk to multiple LLMs simultaneously via the command line or a GUI running on my local machine? Ideally these have existing \"connectors\" or interfaces built out to interface with different LLM APIs and support customizing the system prompt, etc. as well as multiplexing chats and saving chats to files on my local filesystem",
                        "citations":
                        []
                    }
                ],
                "sender": "human",
                "created_at": "2025-10-19T22:48:49.360161Z",
                "updated_at": "2025-10-19T22:48:49.360161Z",
                "attachments":
                [],
                "files":
                []
            },

why is the text duplicated in the export of the user's claude.ai data?

The text duplication you're seeing is due to the structure of Claude's data export format. Here's what's happening:

    text field (line 17): This is a legacy/simplified field that contains the plain text of the message for backward compatibility or quick access.
    content array (lines 18-30): This is the newer, more structured format that supports rich content types. Each message's content is represented as an array of content blocks, where each block has a type field.

In your export, the content array contains a single text block (type: "text") that holds the same message. This structure exists because:

    Extensibility: The content array can hold multiple types of blocks - text, images, files, citations, etc. A message could theoretically have multiple content blocks (e.g., text + image + code).
    Metadata: Each content block can carry additional metadata like timestamps, flags, and citations that wouldn't fit in a simple string field.
    API consistency: This structure mirrors how Claude's API represents messages internally and externally.

When you're parsing this data, you should typically use the content array as the authoritative source, since it contains the full structured representation. The top-level text field is essentially a convenience/compatibility field that flattens the content for simple use cases.

If you're building tools to work with this export format, iterate through the content array and handle each block according to its type rather than relying solely on the top-level text field.

"""

class ClaudeProvider(Provider):
    """Provider implementation for Claude.ai exports."""

    def get_provider_name(self) -> str:
        return "claude"

    def find_zip_files(self, search_dir: Path) -> List[Path]:
        """Find Claude export zip files (data-*.zip pattern)."""
        return list(search_dir.glob("data-*.zip"))

    def _read_projects(self, zf: zipfile.ZipFile) -> List[Dict]:
        """Read projects from a Claude export.

        Older exports ship a single ``projects.json`` list. Newer exports ship
        one ``projects/<uuid>.json`` file per project. Support both.
        """
        names = set(zf.namelist())
        if "projects.json" in names:
            return json.loads(zf.read("projects.json"))

        project_files = sorted(
            n for n in names
            if n.startswith("projects/") and n.endswith(".json")
        )
        return [json.loads(zf.read(n)) for n in project_files]

    def extract_export_data(self, zip_path: Path) -> Dict:
        """Extract users, conversations, and projects from a Claude export."""
        with zipfile.ZipFile(zip_path, "r") as zf:
            try:
                return {
                    "users": json.loads(zf.read("users.json")),
                    "conversations": json.loads(zf.read("conversations.json")),
                    "projects": self._read_projects(zf)
                }
            except KeyError as e:
                emit_error(f"ERROR: Missing expected file in zip: {e}")
                sys.exit(1)
            except json.JSONDecodeError as e:
                emit_error(f"ERROR: Invalid JSON in zip file: {e}")
                sys.exit(1)

    def validate_export_format(self, export_data: Dict) -> None:
        """Validate Claude export format."""
        validate_claude_export_format(
            export_data["users"],
            export_data["conversations"],
            export_data["projects"]
        )

    def get_user_email(self, export_data: Dict) -> str:
        """Extract user email from users.json."""
        users_data = export_data["users"]

        if not users_data or len(users_data) == 0:
            emit_error("ERROR: No user found in users.json")
            sys.exit(1)

        user = users_data[0]
        email = user.get("email_address")

        if not email:
            emit_error("ERROR: No email_address found in user data")
            sys.exit(1)

        return email

    def get_user_uuid(self, export_data: Dict) -> str:
        """Extract user UUID from users.json."""
        users_data = export_data["users"]

        if not users_data or len(users_data) == 0:
            emit_error("ERROR: No user found in users.json")
            sys.exit(1)

        user = users_data[0]
        user_uuid = user.get("uuid")

        if not user_uuid:
            emit_error("ERROR: No uuid found in user data")
            sys.exit(1)

        return user_uuid

    def save_user_data(self, user_dir: Path, export_data: Dict, email: str, user_uuid: str) -> None:
        """Save user.json from export data."""
        user = export_data["users"][0]
        user_json_path = user_dir / "user.json"
        with open(user_json_path, "w", encoding="utf-8") as f:
            json.dump(user, f, indent=2, ensure_ascii=False)
        emit_info(f"Saved user data to: {user_json_path}")

    def get_conversations(self, export_data: Dict) -> List[Dict]:
        """Extract conversations from export data."""
        return export_data["conversations"]

    def get_projects(self, export_data: Dict) -> List[Dict]:
        """Extract projects from export data."""
        return export_data["projects"]

    def should_delete_existing_conversation(self, existing_data: Dict,
                                           new_uuids: Set[str],
                                           user_uuid: str) -> bool:
        """
        Check if existing conversation should be deleted.
        Only delete if BOTH conversation UUID matches AND account UUID matches.
        """
        existing_uuid = existing_data.get("uuid")
        existing_account_uuid = existing_data.get("account", {}).get("uuid")

        # Only delete if BOTH the conversation UUID matches AND it belongs to the same user
        if existing_uuid in new_uuids and existing_account_uuid == user_uuid:
            emit_info(f"  Removing old version: UUID {existing_uuid}")
            return True
        elif existing_uuid in new_uuids and existing_account_uuid != user_uuid:
            # This should never happen, but log it as a warning
            emit_warn(f"  WARNING: Found conversation with UUID {existing_uuid} but different account UUID!")
            emit_detail(f"           Expected account: {user_uuid}, Found: {existing_account_uuid}")
            emit_detail(f"           Skipping deletion for safety.")
            return False

        return False

    def should_delete_existing_project(self, existing_data: Dict,
                                       new_uuids: Set[str],
                                       user_uuid: str) -> bool:
        """
        Check if existing project should be deleted.
        Only delete if BOTH project UUID matches AND creator UUID matches.
        """
        existing_uuid = existing_data.get("uuid")
        existing_creator_uuid = existing_data.get("creator", {}).get("uuid")

        # Only delete if BOTH the project UUID matches AND it belongs to the same user
        if existing_uuid in new_uuids and existing_creator_uuid == user_uuid:
            emit_info(f"  Removing old version: UUID {existing_uuid}")
            return True
        elif existing_uuid in new_uuids and existing_creator_uuid != user_uuid:
            # This should never happen, but log it as a warning
            emit_warn(f"  WARNING: Found project with UUID {existing_uuid} but different creator UUID!")
            emit_detail(f"           Expected creator: {user_uuid}, Found: {existing_creator_uuid}")
            emit_detail(f"           Skipping deletion for safety.")
            return False

        return False


# ============================================================================
# ChatGPT provider implementation
# ============================================================================

class ChatGPTProvider(Provider):
    """Provider implementation for ChatGPT exports."""

    def get_provider_name(self) -> str:
        return "chatgpt"

    def _normalize_conversation(self, conv: Dict) -> Dict:
        """
        Normalize ChatGPT conversation format to internal format.
        Converts: id → uuid, title → name, create_time → created_at
        """
        normalized = dict(conv)  # Make a copy

        # `id` addresses the conversation, so it is required. validate() only
        # checks the first conversation in an export, so a later malformed one
        # can still reach here — guard with .get() to give a clear error rather
        # than a raw KeyError.
        if not conv.get("id"):
            emit_error("ERROR: missing ID in ChatGPT conversation")
            sys.exit(1)

        # Map new field names to internal names. title is often null/empty for
        # untitled chats; sanitize_name() turns a blank name into "untitled".
        normalized["uuid"] = conv["id"]
        normalized["name"] = conv.get("title") or ""

        # Convert Unix timestamp to ISO format string. create_time can be null
        # on some exports; fall back to update_time before giving up (avoids
        # datetime.fromtimestamp(None) raising an opaque TypeError).
        timestamp = conv.get("create_time") or conv.get("update_time")
        if timestamp is None:
            emit_error(f"ERROR: ChatGPT conversation {conv['id']} has no usable timestamp")
            sys.exit(1)
        # UTC, not local: datetime.fromtimestamp(timestamp) would yield local
        # wall-clock and the trailing 'Z' would then mislabel it UTC — storing a
        # created_at wrong by the machine's offset on any non-UTC host.
        normalized["created_at"] = to_utc_iso(timestamp)

        return normalized

    def find_zip_files(self, search_dir: Path) -> List[Path]:
        """Find ChatGPT export zip files ([hex]-YYYY-MM-DD-HH-MM-SS-[hex].zip pattern)."""
        all_zips = list(search_dir.glob("*.zip"))

        # Filter for ChatGPT pattern: long hex string followed by date
        chatgpt_pattern = re.compile(r'^[a-f0-9]{64}-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-[a-f0-9]+\.zip$')
        return [z for z in all_zips if chatgpt_pattern.match(z.name)]

    def extract_export_data(self, zip_path: Path) -> Dict:
        """Extract conversations.json and user.json from ChatGPT export."""
        with zipfile.ZipFile(zip_path, "r") as zf:
            try:
                return {
                    "conversations": json.loads(zf.read("conversations.json")),
                    "user": json.loads(zf.read("user.json"))
                }
            except KeyError as e:
                emit_error(f"ERROR: Missing expected file in zip: {e}")
                sys.exit(1)
            except json.JSONDecodeError as e:
                emit_error(f"ERROR: Invalid JSON in zip file: {e}")
                sys.exit(1)

    def validate_export_format(self, export_data: Dict) -> None:
        """Validate ChatGPT export format."""
        validate_chatgpt_export_format(export_data["conversations"])

    def get_user_email(self, export_data: Dict) -> str:
        """Get user email from user.json."""
        user_data = export_data.get("user")
        if not user_data:
            emit_error("ERROR: No user data found in export")
            sys.exit(1)

        email = user_data.get("email")
        if not email:
            emit_error("ERROR: No email found in user data")
            sys.exit(1)

        return email

    def get_user_uuid(self, export_data: Dict) -> str:
        """Get user UUID from user.json."""
        user_data = export_data.get("user")
        if not user_data:
            emit_error("ERROR: No user data found in export")
            sys.exit(1)

        user_id = user_data.get("id")
        if not user_id:
            emit_error("ERROR: No id found in user data")
            sys.exit(1)

        return user_id

    def save_user_data(self, user_dir: Path, export_data: Dict, email: str, user_uuid: str) -> None:
        """Save user.json from export."""
        user_json_path = user_dir / "user.json"
        user_data = export_data.get("user")

        with open(user_json_path, "w", encoding="utf-8") as f:
            json.dump(user_data, f, indent=2, ensure_ascii=False)
        emit_info(f"Saved user data to: {user_json_path}")

    def get_conversations(self, export_data: Dict) -> List[Dict]:
        """Extract and normalize conversations from export data."""
        conversations = export_data["conversations"]
        return [self._normalize_conversation(conv) for conv in conversations]

    def get_projects(self, export_data: Dict) -> List[Dict]:
        """ChatGPT doesn't have projects, return empty list."""
        return []

    def should_delete_existing_conversation(self, existing_data: Dict,
                                           new_uuids: Set[str],
                                           user_uuid: str) -> bool:
        """
        Check if existing conversation should be deleted.
        Only checks UUID match (ChatGPT doesn't have account UUIDs).
        """
        existing_uuid = existing_data.get("uuid")

        # Only delete if the conversation UUID matches
        if existing_uuid in new_uuids:
            emit_info(f"  Removing old version: UUID {existing_uuid}")
            return True

        return False

    def should_delete_existing_project(self, existing_data: Dict,
                                       new_uuids: Set[str],
                                       user_uuid: str) -> bool:
        """ChatGPT doesn't have projects, never delete."""
        return False


# ============================================================================
# Common orchestration logic
# ============================================================================

def process_items(items: List[Dict], items_dir: Path, item_type: str,
                 user_uuid: str, provider: Provider,
                 should_delete_fn) -> None:
    """
    Common logic for processing conversations or projects.

    Args:
        items: List of conversation or project dictionaries
        items_dir: Directory to save items to
        item_type: "conversation" or "project" (for logging)
        user_uuid: User UUID for validation
        provider: Provider instance
        should_delete_fn: Function to determine if existing item should be deleted
    """
    if not items:
        return

    emit_info(f"\nProcessing {len(items)} {item_type}s...")

    # Build unique filenames for all items
    uuid_to_filename = build_unique_filenames(items)

    # Build UUID set from new export
    new_uuids: Set[str] = {item["uuid"] for item in items}

    # Delete existing files with matching UUIDs (if appropriate)
    existing_files = list(items_dir.glob("*.json"))
    for existing_file in existing_files:
        try:
            with open(existing_file, "r", encoding="utf-8") as f:
                existing_data = json.load(f)

                if should_delete_fn(existing_data, new_uuids, user_uuid):
                    existing_file.unlink()
        except Exception as e:
            emit_warn(f"  Warning: Could not check {existing_file.name}: {e}")

    # Write new item files
    for item in items:
        uuid = item["uuid"]
        filename = uuid_to_filename[uuid]
        filepath = items_dir / f"{filename}.json"

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(item, f, indent=2, ensure_ascii=False)

        emit_info(f"  Saved: {filename}.json (UUID: {uuid})")


def extract_and_organize(provider: Provider, zip_path: Path) -> str:
    """
    Extract and organize export using provider-specific methods.
    This function contains the common imperative orchestration logic.
    Returns the user email extracted from the export.
    """
    emit_info(f"Processing: {zip_path}")

    # Provider extracts data
    export_data = provider.extract_export_data(zip_path)

    # Provider validates format
    provider.validate_export_format(export_data)

    # Provider determines email and UUID
    email = provider.get_user_email(export_data)
    user_uuid = provider.get_user_uuid(export_data)

    emit_info(f"User email: {email}")
    emit_info(f"User UUID: {user_uuid}")

    # Create user directory under data/{provider}/
    user_dir = provider.get_user_dir(email)
    user_dir.mkdir(parents=True, exist_ok=True)

    conversations_dir = user_dir / "conversations"
    projects_dir = user_dir / "projects"
    conversations_dir.mkdir(exist_ok=True)
    projects_dir.mkdir(exist_ok=True)

    # Save user data
    provider.save_user_data(user_dir, export_data, email, user_uuid)

    # Process conversations
    conversations = provider.get_conversations(export_data)
    process_items(
        items=conversations,
        items_dir=conversations_dir,
        item_type="conversation",
        user_uuid=user_uuid,
        provider=provider,
        should_delete_fn=provider.should_delete_existing_conversation
    )

    # Process projects (if provider supports them)
    projects = provider.get_projects(export_data)
    if projects:
        process_items(
            items=projects,
            items_dir=projects_dir,
            item_type="project",
            user_uuid=user_uuid,
            provider=provider,
            should_delete_fn=provider.should_delete_existing_project
        )

    emit_success(f"\n✓ Successfully processed {zip_path.name}")

    return email


# ============================================================================
# Main entry point
# ============================================================================

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sync chat archives to local storage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process Claude export zips
  %(prog)s --claude

  # Process ChatGPT export zips
  %(prog)s --chatgpt

The script will:
  1. Find export zip files in configured ZIP_SEARCH_DIR
  2. Extract conversations and projects organized by provider and email
  3. Update existing conversations (matched by UUID)
  4. Preserve locally archived chats deleted from the provider
  5. Move processed zips to data/archived_exports/{provider}/{email}/

Configuration:
  Set ZIP_SEARCH_DIR in .env to the directory to search for export files (required).
  See .env.example for configuration options.

Export your data:
  Claude:  https://claude.ai/settings (Export data)
  ChatGPT: https://chatgpt.com/settings/data-controls (Export data)
        """
    )

    parser.add_argument(
        "--claude",
        action="store_true",
        help="Process Claude.ai exports (data-*.zip)"
    )
    parser.add_argument(
        "--chatgpt",
        action="store_true",
        help="Process ChatGPT exports ([hex]-YYYY-MM-DD-HH-MM-SS-[hex].zip)"
    )
    add_config_arg(parser)

    args = parser.parse_args()

    # Exactly one provider must be specified
    if not (args.claude or args.chatgpt):
        emit_error("ERROR: Must specify either --claude or --chatgpt")
        parser.print_help()
        sys.exit(1)
    if args.claude and args.chatgpt:
        emit_error("ERROR: Cannot specify both --claude and --chatgpt")
        sys.exit(1)

    # Get script directory
    script_dir = REPO_ROOT

    # Load configuration (shared parser also handles inline comments and quoted
    # values, unlike the previous bespoke split).
    config = load_env_or_exit(script_dir, args.config)

    # Create appropriate provider
    if args.claude:
        provider = ClaudeProvider(script_dir, config)
    else:  # args.chatgpt
        provider = ChatGPTProvider(script_dir, config)

    # Determine where to search for zip files (ZIP_SEARCH_DIR is required)
    if not config.get("ZIP_SEARCH_DIR"):
        emit_error("ERROR: ZIP_SEARCH_DIR is not set.")
        emit_detail(
            "Set it in your .env to the directory containing your export zip "
            "files (typically your browser's downloads folder), e.g.:\n"
            "  ZIP_SEARCH_DIR=~/Downloads"
        )
        sys.exit(1)

    # Use configured directory (expand ~ if present)
    search_dir = Path(config["ZIP_SEARCH_DIR"]).expanduser()

    # Find zip files
    zip_files = provider.find_zip_files(search_dir)

    if not zip_files:
        provider_name = provider.get_provider_name()
        emit_info(f"No {provider_name} export zip files found")
        emit_info(f"Searched in: {search_dir}")
        if provider_name == "claude":
            emit_info("Expected pattern: data-*.zip")
        elif provider_name == "chatgpt":
            emit_info("Expected pattern: [64-char-hex]-YYYY-MM-DD-HH-MM-SS-[hex].zip")
        sys.exit(0)

    emit_info(f"Found {len(zip_files)} zip file(s) to process\n")

    # Process each zip file
    for zip_path in sorted(zip_files):
        try:
            email = extract_and_organize(provider, zip_path)

            # Move the zip file to archived_exports/{provider}/{email}/
            archived_exports_dir = provider.get_archived_exports_dir(email)
            archived_exports_dir.mkdir(parents=True, exist_ok=True)
            archive_dest = archived_exports_dir / zip_path.name
            zip_path.rename(archive_dest)
            emit_info(f"Moved {zip_path.name} to {archived_exports_dir}/")
            print()
        except Exception as e:
            emit_error(f"\nERROR processing {zip_path.name}: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

    emit_success(f"All done! Data location: {provider.get_user_dir(email)}")


if __name__ == "__main__":
    main()
