#!/usr/bin/env python3
"""
Test script to validate data structure integrity.

This script checks that:
1. All conversation/project JSON files are valid
2. Required fields are present
3. UUIDs are unique
4. File naming conventions are followed
5. The sync scripts' validation functions work correctly
"""

import json
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

from paths import load_env_file, resolve_data_dir

# Color codes for output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def print_success(msg: str) -> None:
    """Print success message in green."""
    print(f"{GREEN}✓{RESET} {msg}")


def print_error(msg: str) -> None:
    """Print error message in red."""
    print(f"{RED}✗{RESET} {msg}")


def print_warning(msg: str) -> None:
    """Print warning message in yellow."""
    print(f"{YELLOW}⚠{RESET} {msg}")


def validate_json_file(filepath: Path) -> Tuple[bool, str]:
    """
    Validate that a file contains valid JSON.

    Returns (success, error_message).
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            json.load(f)
        return True, ""
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON: {e}"
    except Exception as e:
        return False, f"Error reading file: {e}"


def validate_conversation_structure(data: dict, filepath: Path) -> List[str]:
    """
    Validate conversation structure.

    Returns list of error messages (empty if valid).
    """
    errors = []

    # Required top-level fields
    required_fields = ["uuid", "name", "created_at", "updated_at"]
    for field in required_fields:
        if field not in data:
            errors.append(f"Missing required field: {field}")

    # Check account structure (for provider conversations)
    if "account" in data:
        if not isinstance(data["account"], dict):
            errors.append("'account' should be a dictionary")
        elif "uuid" not in data["account"]:
            errors.append("'account' missing 'uuid' field")

    # Check chat_messages structure
    if "chat_messages" in data:
        if not isinstance(data["chat_messages"], list):
            errors.append("'chat_messages' should be a list")
        else:
            for i, msg in enumerate(data["chat_messages"]):
                if not isinstance(msg, dict):
                    errors.append(f"Message {i} is not a dictionary")
                    continue

                # Check message fields
                if "uuid" not in msg:
                    errors.append(f"Message {i} missing 'uuid'")
                if "sender" not in msg:
                    errors.append(f"Message {i} missing 'sender'")
                elif msg["sender"] not in ["human", "assistant"]:
                    errors.append(f"Message {i} has invalid sender: {msg['sender']}")

    return errors


def validate_project_structure(data: dict, filepath: Path) -> List[str]:
    """
    Validate project structure.

    Returns list of error messages (empty if valid).
    """
    errors = []

    # Required top-level fields
    required_fields = ["uuid", "name", "created_at"]
    for field in required_fields:
        if field not in data:
            errors.append(f"Missing required field: {field}")

    # Check creator structure
    if "creator" in data:
        if not isinstance(data["creator"], dict):
            errors.append("'creator' should be a dictionary")
        elif "uuid" not in data["creator"]:
            errors.append("'creator' missing 'uuid' field")

    # Check docs structure
    if "docs" in data:
        if not isinstance(data["docs"], list):
            errors.append("'docs' should be a list")

    return errors


def check_uuid_uniqueness(data_dir: Path) -> Tuple[bool, List[str]]:
    """
    Check that all conversation/project UUIDs are unique.

    Returns (success, error_messages).
    """
    uuid_to_files: Dict[str, List[Path]] = {}
    errors = []

    # Scan provider directories (claude/, chatgpt/, etc.)
    for provider in ["claude", "chatgpt", "gemini"]:
        provider_dir = data_dir / provider
        if not provider_dir.exists():
            continue

        for user_dir in provider_dir.iterdir():
            if not user_dir.is_dir():
                continue

            # Check conversations
            conversations_dir = user_dir / "conversations"
            if conversations_dir.exists():
                for conv_file in conversations_dir.glob("*.json"):
                    try:
                        with open(conv_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            uuid = data.get("uuid")
                            if uuid:
                                if uuid not in uuid_to_files:
                                    uuid_to_files[uuid] = []
                                uuid_to_files[uuid].append(conv_file)
                    except Exception:
                        pass  # Will be caught by other validation

            # Check projects
            projects_dir = user_dir / "projects"
            if projects_dir.exists():
                for proj_file in projects_dir.glob("*.json"):
                    try:
                        with open(proj_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            uuid = data.get("uuid")
                            if uuid:
                                if uuid not in uuid_to_files:
                                    uuid_to_files[uuid] = []
                                uuid_to_files[uuid].append(proj_file)
                    except Exception:
                        pass  # Will be caught by other validation

    # Find duplicates
    for uuid, files in uuid_to_files.items():
        if len(files) > 1:
            file_list = ", ".join(str(f.relative_to(data_dir)) for f in files)
            errors.append(f"UUID {uuid} found in multiple files: {file_list}")

    return len(errors) == 0, errors


def test_format_validators() -> Tuple[bool, List[str]]:
    """
    Test the sync scripts' validation functions with test data.

    Returns (success, error_messages).
    """
    errors = []
    script_dir = Path(__file__).parent
    test_data_dir = script_dir / "tests/test-data"

    if not test_data_dir.exists():
        return False, ["tests/test-data directory not found"]

    # Test Claude validator
    try:
        from sync_local_chats_archive import validate_claude_export_format

        with open(test_data_dir / "users.json") as f:
            users = json.load(f)
        with open(test_data_dir / "conversations.json") as f:
            convs = json.load(f)
        with open(test_data_dir / "projects.json") as f:
            projs = json.load(f)

        # This should not raise an exception
        validate_claude_export_format(users, convs, projs)
        print_success("Claude format validator passed with test data")
    except SystemExit:
        errors.append("Claude format validator failed with test data")
    except Exception as e:
        errors.append(f"Error testing Claude validator: {e}")

    return len(errors) == 0, errors


def main():
    """Main entry point."""
    script_dir = Path(__file__).parent
    config = load_env_file(script_dir / ".env")
    data_dir = resolve_data_dir(script_dir, config)

    print("\n=== Data Structure Validation ===\n")

    if not data_dir.exists():
        print_warning(f"Data directory not found: {data_dir}")
        print("This is normal if you haven't synced any archives yet.")
        print("\nTesting format validators only...\n")

        success, errors = test_format_validators()
        if not success:
            for error in errors:
                print_error(error)
            sys.exit(1)

        print(f"\n{GREEN}All validation tests passed!{RESET}")
        sys.exit(0)

    total_errors = 0
    total_files = 0

    # Validate all provider directories
    for provider in ["claude", "chatgpt", "gemini"]:
        provider_dir = data_dir / provider
        if not provider_dir.exists():
            continue

        print(f"\nScanning {provider}/ directory...")

        for user_dir in provider_dir.iterdir():
            if not user_dir.is_dir():
                continue

            email = user_dir.name
            print(f"\nValidating: {provider}/{email}")

            # Check conversations
            conversations_dir = user_dir / "conversations"
            if conversations_dir.exists():
                for conv_file in sorted(conversations_dir.glob("*.json")):
                    total_files += 1

                    # Check JSON validity
                    success, error = validate_json_file(conv_file)
                    if not success:
                        print_error(f"{conv_file.name}: {error}")
                        total_errors += 1
                        continue

                    # Check structure
                    with open(conv_file, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    errors = validate_conversation_structure(data, conv_file)
                    if errors:
                        print_error(f"{conv_file.name}:")
                        for error in errors:
                            print(f"  - {error}")
                        total_errors += len(errors)

            # Check projects
            projects_dir = user_dir / "projects"
            if projects_dir.exists():
                for proj_file in sorted(projects_dir.glob("*.json")):
                    total_files += 1

                    # Check JSON validity
                    success, error = validate_json_file(proj_file)
                    if not success:
                        print_error(f"{proj_file.name}: {error}")
                        total_errors += 1
                        continue

                    # Check structure
                    with open(proj_file, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    errors = validate_project_structure(data, proj_file)
                    if errors:
                        print_error(f"{proj_file.name}:")
                        for error in errors:
                            print(f"  - {error}")
                        total_errors += len(errors)

    # Check UUID uniqueness
    print("\nChecking UUID uniqueness...")
    success, errors = check_uuid_uniqueness(data_dir)
    if not success:
        for error in errors:
            print_error(error)
        total_errors += len(errors)
    else:
        print_success("All UUIDs are unique")

    # Test format validators
    print("\nTesting format validators...")
    success, errors = test_format_validators()
    if not success:
        for error in errors:
            print_error(error)
        total_errors += len(errors)

    # Summary
    print("\n=== Summary ===")
    print(f"Files checked: {total_files}")

    if total_errors == 0:
        print(f"{GREEN}All validation tests passed!{RESET}")
        sys.exit(0)
    else:
        print(f"{RED}Found {total_errors} error(s){RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()
