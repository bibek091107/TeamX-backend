"""
AI Team Brain — Chat Routes
Role-Based Channel Management, Restricted Channels, Member Approval Workflow,
Archiving, Reactions, Replies, File Upload, and AI Assistant.
"""

import os
import uuid as _uuid
from datetime import datetime
from flask import Blueprint, request, jsonify, g, current_app, send_from_directory
from werkzeug.utils import secure_filename
from models.database import db, ChatRoom, ChatMessage, TeamMember, ActivityLog, ChannelMember, User, MessageReaction, FileAttachment
from utils.auth_middleware import require_auth
from loguru import logger

chat_bp = Blueprint("chat", __name__)


def _is_team_member(team_id, user_id):
    """Membership that counts — pending join requests can't read the chat."""
    if not team_id or not user_id:
        return None
    m = TeamMember.query.filter_by(team_id=team_id, user_id=user_id).first()
    if m is None:
        return None
    return m if (m.status or "active") == "active" else None


def _is_team_admin(team_id, user_id):
    """Checks if the user is a team owner or admin, or has global admin privileges."""
    if not team_id or not user_id:
        return False
    u = User.query.get(user_id)
    if u and getattr(u, "role", None) == "admin":
        return True
    m = TeamMember.query.filter_by(team_id=team_id, user_id=user_id).first()
    if not m or (m.status or "active") != "active":
        return False
    return m.role in ("owner", "admin")


def _can_access_room(room, user_id):
    """Verify if user has permission to read and interact with room."""
    if not room or not user_id:
        return False
    if not _is_team_member(room.team_id, user_id):
        return False
    if not room.is_restricted:
        return True
    if _is_team_admin(room.team_id, user_id):
        return True
    cm = ChannelMember.query.filter_by(room_id=room.id, user_id=user_id, status="approved").first()
    return cm is not None


@chat_bp.route("/teams/<team_id>/rooms", methods=["GET"])
@require_auth
def get_rooms(team_id):
    if not _is_team_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403
    rooms = ChatRoom.query.filter_by(team_id=team_id).all()
    return jsonify({"rooms": [r.to_dict(user_id=g.current_user.id) for r in rooms]})


@chat_bp.route("/teams/<team_id>/rooms", methods=["POST"])
@require_auth
def create_room(team_id):
    if not _is_team_admin(team_id, g.current_user.id):
        return jsonify({"error": "Only team admins can create channels"}), 403
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Channel name required"}), 400

    is_restricted = bool(data.get("isRestricted", data.get("is_restricted", False)))

    room = ChatRoom(
        team_id=team_id,
        name=name.lower().replace(" ", "-"),
        description=data.get("description", ""),
        room_type=data.get("roomType", "general"),
        is_restricted=is_restricted,
        is_archived=False,
        created_by=g.current_user.id,
    )
    db.session.add(room)
    db.session.flush()

    # Creator is automatically an approved admin member of this channel
    cm = ChannelMember(
        room_id=room.id,
        user_id=g.current_user.id,
        role="admin",
        status="approved",
        approved_at=datetime.utcnow(),
        approved_by=g.current_user.id,
    )
    db.session.add(cm)
    db.session.commit()
    logger.info(f"Channel #{room.name} created by {g.current_user.email} (restricted={is_restricted})")
    return jsonify({"room": room.to_dict(user_id=g.current_user.id)}), 201


@chat_bp.route("/rooms/<room_id>", methods=["PUT"])
@require_auth
def update_room(room_id):
    """Admin updates channel details: rename, description, restricted, or archived."""
    room = ChatRoom.query.get_or_404(room_id)
    if not _is_team_admin(room.team_id, g.current_user.id):
        return jsonify({"error": "Only team admins can manage channels"}), 403

    data = request.get_json() or {}
    if "name" in data and data["name"].strip():
        room.name = data["name"].strip().lower().replace(" ", "-")
    if "description" in data:
        room.description = data["description"]
    if "isRestricted" in data:
        room.is_restricted = bool(data["isRestricted"])
    elif "is_restricted" in data:
        room.is_restricted = bool(data["is_restricted"])
    if "isArchived" in data:
        room.is_archived = bool(data["isArchived"])
    elif "is_archived" in data:
        room.is_archived = bool(data["is_archived"])

    db.session.commit()
    return jsonify({"room": room.to_dict(user_id=g.current_user.id)})


@chat_bp.route("/rooms/<room_id>/archive", methods=["POST"])
@require_auth
def toggle_archive_room(room_id):
    """Admin archives or unarchives channel."""
    room = ChatRoom.query.get_or_404(room_id)
    if not _is_team_admin(room.team_id, g.current_user.id):
        return jsonify({"error": "Only team admins can archive channels"}), 403

    data = request.get_json() or {}
    archive_state = data.get("archive", not room.is_archived)
    room.is_archived = bool(archive_state)
    db.session.commit()
    return jsonify({"room": room.to_dict(user_id=g.current_user.id), "message": "Channel archived" if room.is_archived else "Channel unarchived"})


@chat_bp.route("/rooms/<room_id>", methods=["DELETE"])
@require_auth
def delete_room(room_id):
    """Delete a channel — team admins or owners can do this."""
    room = ChatRoom.query.get_or_404(room_id)
    if not _is_team_admin(room.team_id, g.current_user.id):
        return jsonify({"error": "Only team admins can delete channels"}), 403

    room_count = ChatRoom.query.filter_by(team_id=room.team_id).count()
    if room_count <= 1:
        return jsonify({"error": "Cannot delete the last channel"}), 400

    try:
        ChatMessage.query.filter_by(room_id=room_id).delete()
        ChannelMember.query.filter_by(room_id=room_id).delete()
        db.session.delete(room)
        db.session.commit()
        logger.info(f"Channel #{room.name} deleted by {g.current_user.email}")
        return jsonify({"message": "Channel deleted"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@chat_bp.route("/rooms/<room_id>/messages", methods=["GET"])
@require_auth
def get_messages(room_id):
    room = ChatRoom.query.get_or_404(room_id)
    if not _can_access_room(room, g.current_user.id):
        return jsonify({"error": "Access denied: channel requires membership approval"}), 403

    limit  = int(request.args.get("limit", 60))
    before = request.args.get("before")
    query  = ChatMessage.query.filter_by(room_id=room_id)
    if before:
        try:
            query = query.filter(ChatMessage.created_at < datetime.fromisoformat(before))
        except Exception:
            pass
    messages = query.order_by(ChatMessage.created_at.desc()).limit(limit).all()
    messages.reverse()
    return jsonify({"messages": [m.to_dict() for m in messages]})


@chat_bp.route("/rooms/<room_id>/messages", methods=["POST"])
@require_auth
def send_message(room_id):
    room = ChatRoom.query.get_or_404(room_id)
    if not _can_access_room(room, g.current_user.id):
        return jsonify({"error": "Access denied: channel requires membership approval"}), 403
    if room.is_archived:
        return jsonify({"error": "Channel is archived and read-only"}), 400

    data = request.get_json() or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "Message content required"}), 400

    reply_to_id = data.get("replyToId") or data.get("reply_to_id")
    if reply_to_id:
        parent = ChatMessage.query.get(reply_to_id)
        if not parent or parent.room_id != room_id:
            reply_to_id = None

    msg = ChatMessage(
        room_id=room_id,
        user_id=g.current_user.id,
        content=content,
        message_type=data.get("messageType", "text"),
        reply_to_id=reply_to_id,
    )
    db.session.add(msg)
    db.session.commit()

    # Emit socket event for the new message
    msg_dict = msg.to_dict()
    try:
        from app import socketio
        socketio.emit("new_message", msg_dict, to=room_id)
    except Exception as e:
        logger.warning(f"Socket emit new_message failed: {e}")

    # Check for @mentions
    import re
    from models.database import Notification
    from services.push_service import send_push_notification
    mentions = re.findall(r"@([a-zA-Z0-9_.-]+)", content)
    if mentions:
        team_members = TeamMember.query.filter_by(team_id=room.team_id, status="active").all()
        for tm in team_members:
            if tm.user_id != g.current_user.id and tm.user:
                uname = (tm.user.display_name or "").lower().replace(" ", "")
                uemail = (tm.user.email or "").split("@")[0].lower()
                for m_query in mentions:
                    m_clean = m_query.lower()
                    if m_clean == uname or m_clean == uemail or m_clean in uname:
                        notif = Notification(
                            user_id=tm.user_id,
                            team_id=room.team_id,
                            title=f"Mentioned in #{room.name}",
                            message=f"{g.current_user.display_name}: {content[:120]}",
                            notification_type="mention",
                            entity_id=msg.id,
                        )
                        db.session.add(notif)
                        db.session.commit()
                        try:
                            from app import socketio
                            socketio.emit("new_notification", notif.to_dict(), room=f"user_{tm.user_id}")
                        except Exception:
                            pass
                        send_push_notification(
                            user_id=tm.user_id,
                            title=f"Mentioned in #{room.name}",
                            body=f"{g.current_user.display_name}: {content[:120]}",
                            extra_data={"roomId": room_id, "messageId": msg.id}
                        )
                        break

    return jsonify({"message": msg_dict}), 201


# ── Reactions ───────────────────────────────────────────────

@chat_bp.route("/messages/<message_id>/reactions", methods=["POST"])
@require_auth
def toggle_reaction(message_id):
    """Toggle an emoji reaction on a message. If the user already reacted with
    the same emoji, remove it; otherwise add it."""
    msg = ChatMessage.query.get_or_404(message_id)
    room = ChatRoom.query.get(msg.room_id)
    if not room or not _can_access_room(room, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403

    data = request.get_json() or {}
    emoji = data.get("emoji", "").strip()
    if not emoji:
        return jsonify({"error": "Emoji required"}), 400

    existing = MessageReaction.query.filter_by(
        message_id=message_id, user_id=g.current_user.id, emoji=emoji
    ).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        action = "removed"
    else:
        reaction = MessageReaction(
            message_id=message_id,
            user_id=g.current_user.id,
            emoji=emoji,
        )
        db.session.add(reaction)
        db.session.commit()
        action = "added"

    msg_dict = msg.to_dict()
    try:
        from app import socketio
        socketio.emit("reaction_updated", msg_dict, to=msg.room_id)
    except Exception as e:
        logger.warning(f"Socket emit reaction_updated failed: {e}")

    status_code = 200 if action == "removed" else 201
    return jsonify({"action": action, "message": msg_dict}), status_code


# ── File Upload ─────────────────────────────────────────────

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "pdf", "doc", "docx", "xls", "xlsx", "txt", "csv", "zip", "mp4", "mp3"}

def _allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

@chat_bp.route("/rooms/<room_id>/upload", methods=["POST"])
@require_auth
def upload_file(room_id):
    """Upload a file attachment to a room."""
    room = ChatRoom.query.get_or_404(room_id)
    if not _can_access_room(room, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403
    if room.is_archived:
        return jsonify({"error": "Channel is archived"}), 400

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if file.filename == "" or not _allowed_file(file.filename):
        return jsonify({"error": "Invalid file type"}), 400

    original_name = secure_filename(file.filename)
    ext = original_name.rsplit(".", 1)[1].lower() if "." in original_name else "bin"
    saved_name = f"{_uuid.uuid4().hex[:12]}_{original_name}"
    upload_dir = os.path.join(current_app.config["UPLOAD_FOLDER"], room.team_id)
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, saved_name)
    file.save(filepath)

    content = request.form.get("content", f"📎 {original_name}").strip()

    msg = ChatMessage(
        room_id=room_id,
        user_id=g.current_user.id,
        content=content,
        message_type="file",
    )
    db.session.add(msg)
    db.session.flush()

    attachment = FileAttachment(
        message_id=msg.id,
        team_id=room.team_id,
        uploader_id=g.current_user.id,
        filename=saved_name,
        original_filename=original_name,
        file_path=filepath,
        file_type=ext,
        file_size=os.path.getsize(filepath),
    )
    db.session.add(attachment)
    db.session.commit()
    return jsonify({"message": msg.to_dict(), "attachment": attachment.to_dict()}), 201


@chat_bp.route("/files/<team_id>/<filename>", methods=["GET"])
@require_auth
def download_file(team_id, filename):
    if not _is_team_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403
    upload_dir = os.path.join(current_app.config["UPLOAD_FOLDER"], team_id)
    return send_from_directory(upload_dir, filename)


# ── AI Team Assistant ───────────────────────────────────────

@chat_bp.route("/rooms/<room_id>/ai-assist", methods=["POST"])
@require_auth
def ai_assistant(room_id):
    """Ask the AI assistant a question in the context of the current channel."""
    room = ChatRoom.query.get_or_404(room_id)
    if not _can_access_room(room, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403

    data = request.get_json() or {}
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "Question required"}), 400

    # Get recent messages for context
    recent = ChatMessage.query.filter_by(room_id=room_id)\
        .order_by(ChatMessage.created_at.desc()).limit(30).all()
    recent.reverse()
    context_lines = [
        f"{m.user.display_name if m.user else 'User'}: {m.content}"
        for m in recent if m.message_type == "text"
    ]
    context_str = "\n".join(context_lines[-20:])

    from services.ai.ai_service import summarize_content
    try:
        prompt_text = f"Channel context (recent messages):\n{context_str}\n\nUser question: {question}\n\nProvide a helpful, concise answer."
        result = summarize_content(prompt_text, "team assistant query")
        answer = result.get("summary", "I couldn't generate a response. Please try again.")
    except Exception as e:
        answer = f"AI service unavailable: {str(e)}"

    # Save the assistant response as a message
    ai_msg = ChatMessage(
        room_id=room_id,
        user_id=g.current_user.id,
        content=f"🤖 **AI Assistant**\n{answer}",
        message_type="ai_summary",
    )
    db.session.add(ai_msg)
    db.session.commit()
    return jsonify({"answer": answer, "message": ai_msg.to_dict()})


@chat_bp.route("/rooms/<room_id>/members", methods=["GET"])
@require_auth
def get_room_members(room_id):
    room = ChatRoom.query.get_or_404(room_id)
    if not _is_team_member(room.team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403

    is_admin = _is_team_admin(room.team_id, g.current_user.id)
    if is_admin:
        members = ChannelMember.query.filter_by(room_id=room_id).all()
    else:
        members = ChannelMember.query.filter_by(room_id=room_id, status="approved").all()
    return jsonify({"members": [m.to_dict() for m in members]})


@chat_bp.route("/rooms/<room_id>/join", methods=["POST"])
@require_auth
def request_join_room(room_id):
    room = ChatRoom.query.get_or_404(room_id)
    if not _is_team_member(room.team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403
    if room.is_archived:
        return jsonify({"error": "Channel is archived"}), 400

    is_admin = _is_team_admin(room.team_id, g.current_user.id)
    existing = ChannelMember.query.filter_by(room_id=room_id, user_id=g.current_user.id).first()

    if existing:
        if existing.status == "approved":
            return jsonify({"message": "Already an approved member", "member": existing.to_dict()})
        elif existing.status == "pending":
            return jsonify({"message": "Join request is pending admin approval", "member": existing.to_dict()})
        else:
            existing.status = "approved" if (is_admin or not room.is_restricted) else "pending"
            existing.requested_at = datetime.utcnow()
            db.session.commit()
            return jsonify({"message": "Join request submitted", "member": existing.to_dict()})

    status = "approved" if (is_admin or not room.is_restricted) else "pending"
    cm = ChannelMember(
        room_id=room_id,
        user_id=g.current_user.id,
        role="admin" if is_admin else "member",
        status=status,
        requested_at=datetime.utcnow(),
        approved_at=datetime.utcnow() if status == "approved" else None,
        approved_by=g.current_user.id if status == "approved" else None,
    )
    db.session.add(cm)
    db.session.commit()
    msg = "Joined channel" if status == "approved" else "Join request submitted for admin approval"
    return jsonify({"message": msg, "member": cm.to_dict()}), 201


@chat_bp.route("/rooms/<room_id>/requests/<membership_id>/approve", methods=["POST"])
@require_auth
def approve_room_request(room_id, membership_id):
    room = ChatRoom.query.get_or_404(room_id)
    if not _is_team_admin(room.team_id, g.current_user.id):
        return jsonify({"error": "Only team admins can approve channel requests"}), 403

    cm = ChannelMember.query.filter_by(id=membership_id, room_id=room_id).first_or_404()
    cm.status = "approved"
    cm.approved_at = datetime.utcnow()
    cm.approved_by = g.current_user.id
    db.session.commit()
    logger.info(f"Channel #{room.name} member request {membership_id} approved by {g.current_user.email}")
    return jsonify({"message": "Request approved", "member": cm.to_dict()})


@chat_bp.route("/rooms/<room_id>/requests/<membership_id>/reject", methods=["POST"])
@require_auth
def reject_room_request(room_id, membership_id):
    room = ChatRoom.query.get_or_404(room_id)
    if not _is_team_admin(room.team_id, g.current_user.id):
        return jsonify({"error": "Only team admins can reject channel requests"}), 403

    cm = ChannelMember.query.filter_by(id=membership_id, room_id=room_id).first_or_404()
    cm.status = "rejected"
    db.session.commit()
    logger.info(f"Channel #{room.name} member request {membership_id} rejected by {g.current_user.email}")
    return jsonify({"message": "Request rejected", "member": cm.to_dict()})


@chat_bp.route("/rooms/<room_id>/catchup", methods=["GET"])
@require_auth
def catch_me_up(room_id):
    room = ChatRoom.query.get_or_404(room_id)
    if not _can_access_room(room, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403

    messages = ChatMessage.query.filter_by(room_id=room_id)\
        .order_by(ChatMessage.created_at.desc()).limit(50).all()
    messages.reverse()

    if not messages:
        return jsonify({"summary": "No messages yet in this channel."})

    from services.ai.ai_service import generate_catchup_summary
    try:
        summary = generate_catchup_summary(
            [m.to_dict() for m in messages],
            g.current_user.display_name or "there"
        )
        return jsonify({"summary": summary, "messageCount": len(messages)})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503


@chat_bp.route("/rooms/<room_id>/summarize", methods=["POST"])
@require_auth
def summarize_chat(room_id):
    room = ChatRoom.query.get_or_404(room_id)
    if not _can_access_room(room, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403

    messages = ChatMessage.query.filter_by(room_id=room_id)\
        .order_by(ChatMessage.created_at.desc()).limit(100).all()
    messages.reverse()

    chat_text = "\n".join([
        f"{m.user.display_name if m.user else 'User'}: {m.content}"
        for m in messages if m.message_type == "text"
    ])

    from services.ai.ai_service import summarize_content
    try:
        result = summarize_content(chat_text, "team chat conversation")
        return jsonify(result)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
