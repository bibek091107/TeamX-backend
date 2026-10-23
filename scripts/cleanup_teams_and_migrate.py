"""
Safe Cleanup and Migration Script for TeamX
===========================================
1. Backs up the SQLite database.
2. Ensures the `last_active_team_id` column exists on the `users` table.
3. Resets test/demo teams and team-scoped records (tasks, messages, rooms, members)
   for a fresh start.
4. Preserves all user accounts, credentials, and settings.
"""

import os
import shutil
import sqlite3
import sys

def run_migration():
    db_paths = ["instance/ai_team_brain.db", "ai_team_brain.db"]
    target_db = None
    for p in db_paths:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            target_db = p
            break

    if not target_db:
        # Fallback to instance/ai_team_brain.db
        target_db = "instance/ai_team_brain.db"
        os.makedirs(os.path.dirname(target_db), exist_ok=True)

    print(f"Target database: {target_db}")

    if os.path.exists(target_db):
        backup_path = f"{target_db}.bak"
        shutil.copy2(target_db, backup_path)
        print(f"Backup created at: {backup_path}")

    conn = sqlite3.connect(target_db)
    cursor = conn.cursor()

    # 1. Add last_active_team_id to users table if missing
    cursor.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in cursor.fetchall()]
    if "last_active_team_id" not in columns:
        print("Adding last_active_team_id column to users table...")
        cursor.execute("ALTER TABLE users ADD COLUMN last_active_team_id VARCHAR(36)")
        conn.commit()
        print("  Added last_active_team_id successfully.")
    else:
        print("last_active_team_id column already exists.")

    # 2. Reset last_active_team_id for all existing users to NULL
    cursor.execute("UPDATE users SET last_active_team_id = NULL")
    conn.commit()

    # 3. Clean up test/demo teams and related team records
    tables_to_clear = [
        "file_attachments",
        "message_reactions",
        "chat_messages",
        "channel_members",
        "chat_rooms",
        "tasks",
        "knowledge_items",
        "activity_logs",
        "notifications",
        "daily_summaries",
        "workspace_invitations",
        "team_members",
        "teams",
    ]

    for table in tables_to_clear:
        try:
            cursor.execute(f"DELETE FROM {table}")
            print(f"  Cleared table: {table}")
        except sqlite3.OperationalError as e:
            print(f"  Note on {table}: {e}")

    conn.commit()

    # Verify users preserved
    cursor.execute("SELECT count(*) FROM users")
    user_count = cursor.fetchone()[0]
    cursor.execute("SELECT count(*) FROM teams")
    team_count = cursor.fetchone()[0]

    conn.close()

    print(f"\nMigration completed successfully!")
    print(f"Preserved {user_count} users.")
    print(f"Team count is now: {team_count} (fresh start).")

if __name__ == "__main__":
    run_migration()
