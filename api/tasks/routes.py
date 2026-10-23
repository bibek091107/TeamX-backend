"""
AI Team Brain — Tasks Routes
RBAC task creation (Admin-only), channel task association, status transitions,
and FCM / Socket notification dispatch.
"""

from datetime import datetime
from flask import Blueprint, request, jsonify, g
from models.database import db, Task, ActivityLog, TeamMember, Notification, ChatRoom, User, DeviceToken
from utils.auth_middleware import require_auth
from services.push_service import send_push_notification
from loguru import logger

tasks_bp = Blueprint("tasks", __name__)


def _check_member(team_id, user_id):
    if not team_id or not user_id:
        return None
    m = TeamMember.query.filter_by(team_id=team_id, user_id=user_id).first()
    return m if m and (m.status or "active") == "active" else None


def _is_admin(team_id, user_id):
    """Admin check: team owner, team admin, or global admin."""
    if not team_id or not user_id:
        return False
    u = User.query.get(user_id)
    if u and getattr(u, "role", None) == "admin":
        return True
    m = TeamMember.query.filter_by(team_id=team_id, user_id=user_id).first()
    if not m or (m.status or "active") != "active":
        return False
    return m.role in ("owner", "admin")


def _is_owner(team_id, user_id):
    return TeamMember.query.filter_by(team_id=team_id, user_id=user_id, role="owner").first()


def _is_valid_assignee(team_id, assignee_id):
    if not assignee_id:
        return True
    return TeamMember.query.filter_by(team_id=team_id, user_id=assignee_id).first() is not None


def _emit_socket(event: str, data: dict, room: str):
    """Emit a socket event to a room."""
    try:
        from app import socketio
        socketio.emit(event, data, room=room)
    except Exception as e:
        logger.warning(f"Socket emit failed ({event}): {e}")


def _create_notification(user_id, team_id, title, message, notif_type, entity_id=None, extra_data=None):
    import json
    extra_dict = extra_data or {}
    if entity_id and "entityId" not in extra_dict:
        extra_dict["entityId"] = entity_id
    if team_id and "teamId" not in extra_dict:
        extra_dict["teamId"] = team_id
    extra_dict["type"] = notif_type

    notif = Notification(
        user_id=user_id, team_id=team_id,
        title=title, message=message,
        notification_type=notif_type, entity_id=entity_id,
        extra_data=json.dumps(extra_dict) if extra_dict else None,
    )
    db.session.add(notif)
    db.session.flush()

    notif_dict = notif.to_dict()
    # 1. Socket emit for real-time in-app update
    _emit_socket("new_notification", notif_dict, f"user_{user_id}")

    # 2. FCM Push Notification for mobile device background/foreground delivery
    send_push_notification(
        user_id=user_id,
        title=title,
        body=message,
        extra_data=extra_dict,
    )
    return notif


@tasks_bp.route("/teams/<team_id>/tasks", methods=["GET"])
@require_auth
def get_tasks(team_id):
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403

    status      = request.args.get("status")
    priority    = request.args.get("priority")
    assignee_id = request.args.get("assigneeId")
    channel_id  = request.args.get("channelId") or request.args.get("channel_id")
    limit       = int(request.args.get("limit", 100))

    query = Task.query.filter_by(team_id=team_id)
    if status:      query = query.filter_by(status=status)
    if priority:    query = query.filter_by(priority=priority)
    if assignee_id: query = query.filter_by(assignee_id=assignee_id)
    if channel_id:  query = query.filter_by(channel_id=channel_id)

    tasks = query.order_by(Task.created_at.desc()).limit(limit).all()
    return jsonify({"tasks": [t.to_dict() for t in tasks]})


@tasks_bp.route("/teams/<team_id>/tasks", methods=["POST"])
@require_auth
def create_task(team_id):
    # RBAC: Only team admins or owners can create and assign tasks
    if not _is_admin(team_id, g.current_user.id):
        return jsonify({"error": "Only team admins can create and assign tasks"}), 403

    data = request.get_json() or {}

    assignee_id = data.get("assigneeId")
    if not _is_valid_assignee(team_id, assignee_id):
        return jsonify({"error": "Assignee must be a member of this team"}), 400

    channel_id = data.get("channelId") or data.get("channel_id")
    if channel_id:
        room = ChatRoom.query.filter_by(id=channel_id, team_id=team_id).first()
        if not room:
            return jsonify({"error": "Specified channel not found in this team"}), 400

    deadline = None
    if data.get("deadline"):
        try:
            deadline = datetime.fromisoformat(data["deadline"].replace("Z", "+00:00"))
        except Exception:
            pass

    task = Task(
        team_id=team_id,
        creator_id=g.current_user.id,
        assignee_id=assignee_id,
        channel_id=channel_id,
        title=data.get("title", "Untitled task"),
        description=data.get("description", ""),
        status=data.get("status", "pending"),
        priority=data.get("priority", "medium"),
        deadline=deadline,
        deadline_text=data.get("deadlineText"),
        source=data.get("source", "manual"),
    )
    db.session.add(task)
    db.session.flush()

    db.session.add(ActivityLog(
        team_id=team_id, user_id=g.current_user.id,
        action="task_created", entity_type="task", entity_id=task.id,
        description=f"{g.current_user.display_name} created: {task.title}",
    ))

    # Notify assignee if assigned
    if task.assignee_id and task.assignee_id != g.current_user.id:
        _create_notification(
            user_id=task.assignee_id, team_id=team_id,
            title="📋 New task assigned to you",
            message=f"{g.current_user.display_name} assigned: {task.title}",
            notif_type="task", entity_id=task.id,
            extra_data={"taskId": task.id, "channelId": channel_id},
        )

    db.session.commit()

    task_dict = task.to_dict()
    # Broadcast new task to team
    _emit_socket("new_task", task_dict, f"team_{team_id}")

    return jsonify({"task": task_dict}), 201


@tasks_bp.route("/teams/<team_id>/tasks/<task_id>", methods=["PUT"])
@require_auth
def update_task(team_id, task_id):
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403

    task = Task.query.filter_by(id=task_id, team_id=team_id).first_or_404()
    data = request.get_json() or {}

    is_team_admin = _is_admin(team_id, g.current_user.id)
    is_assignee   = task.assignee_id == g.current_user.id

    # If user is not an admin, they can ONLY update status on tasks assigned to them
    if not is_team_admin:
        if not is_assignee:
            return jsonify({"error": "Members can only update status for tasks assigned to them"}), 403

        # Check if non-admin attempted to change restricted fields
        restricted_keys = ["title", "description", "priority", "assigneeId", "deadline", "channelId", "channel_id"]
        if any(k in data for k in restricted_keys):
            return jsonify({"error": "Only team admins can modify task details or reassign tasks"}), 403

    # Status update logic
    if "status" in data:
        new_status = data["status"]
        current = task.status
        allowed_transitions = {
            "pending":     ["in_progress"],
            "in_progress": ["completed", "pending"],
            "completed":   ["in_progress"],
        }
        allowed = allowed_transitions.get(current, [])
        if new_status not in allowed:
            return jsonify({
                "error": f"Cannot move task from '{current}' to '{new_status}'",
                "allowed": allowed,
            }), 400

        task.status = new_status
        if new_status == "completed":
            task.completed_at = datetime.utcnow()
        elif new_status == "in_progress":
            task.completed_at = None

    # Admin updates for title, description, priority, assignee, deadline, channel
    if is_team_admin:
        old_assignee = task.assignee_id
        if "title"       in data: task.title       = data["title"]
        if "description" in data: task.description = data["description"]
        if "priority"    in data: task.priority    = data["priority"]
        if "assigneeId"  in data:
            new_assignee_id = data["assigneeId"]
            if not _is_valid_assignee(team_id, new_assignee_id):
                return jsonify({"error": "Assignee must be a member of this team"}), 400
            task.assignee_id = new_assignee_id
        if "channelId" in data or "channel_id" in data:
            new_chan_id = data.get("channelId") or data.get("channel_id")
            if new_chan_id:
                if not ChatRoom.query.filter_by(id=new_chan_id, team_id=team_id).first():
                    return jsonify({"error": "Channel not found in this team"}), 400
            task.channel_id = new_chan_id
        if "deadline"    in data:
            try:
                task.deadline = datetime.fromisoformat(data["deadline"].replace("Z", "+00:00"))
            except Exception:
                pass

        # Notify new assignee
        new_assignee = task.assignee_id
        if new_assignee and new_assignee != old_assignee and new_assignee != g.current_user.id:
            _create_notification(
                user_id=new_assignee, team_id=team_id,
                title="📋 Task assigned to you",
                message=f"{g.current_user.display_name} assigned: {task.title}",
                notif_type="task", entity_id=task.id,
                extra_data={"taskId": task.id, "channelId": task.channel_id},
            )

    db.session.add(ActivityLog(
        team_id=team_id, user_id=g.current_user.id,
        action="task_updated", entity_type="task", entity_id=task.id,
        description=f"{g.current_user.display_name} updated: {task.title}",
    ))
    db.session.commit()

    task_dict = task.to_dict()
    _emit_socket("task_updated", task_dict, f"team_{team_id}")

    return jsonify({"task": task_dict})


@tasks_bp.route("/teams/<team_id>/tasks/<task_id>", methods=["DELETE"])
@require_auth
def delete_task(team_id, task_id):
    if not _is_admin(team_id, g.current_user.id):
        return jsonify({"error": "Only team admins can delete tasks"}), 403

    task = Task.query.filter_by(id=task_id, team_id=team_id).first_or_404()
    task_id_copy = task.id
    db.session.delete(task)
    db.session.commit()

    _emit_socket("task_deleted", {"taskId": task_id_copy, "teamId": team_id}, f"team_{team_id}")
    return jsonify({"message": "Task deleted"})


@tasks_bp.route("/teams/<team_id>/tasks/stats", methods=["GET"])
@require_auth
def get_task_stats(team_id):
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403

    all_tasks = Task.query.filter_by(team_id=team_id).all()
    now = datetime.utcnow()
    return jsonify({"stats": {
        "total":         len(all_tasks),
        "pending":       sum(1 for t in all_tasks if t.status == "pending"),
        "in_progress":   sum(1 for t in all_tasks if t.status == "in_progress"),
        "completed":     sum(1 for t in all_tasks if t.status == "completed"),
        "overdue":       sum(1 for t in all_tasks if t.deadline and t.deadline < now and t.status not in ("completed","cancelled")),
        "high_priority": sum(1 for t in all_tasks if t.priority in ("high","critical") and t.status != "completed"),
        "by_priority": {
            "critical": sum(1 for t in all_tasks if t.priority == "critical"),
            "high":     sum(1 for t in all_tasks if t.priority == "high"),
            "medium":   sum(1 for t in all_tasks if t.priority == "medium"),
            "low":      sum(1 for t in all_tasks if t.priority == "low"),
        }
    }})


@tasks_bp.route("/teams/<team_id>/notifications", methods=["GET"])
@require_auth
def get_notifications(team_id):
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403
    notifs = Notification.query.filter_by(
        user_id=g.current_user.id, team_id=team_id
    ).order_by(Notification.created_at.desc()).limit(30).all()
    return jsonify({"notifications": [n.to_dict() for n in notifs]})


@tasks_bp.route("/notifications/<notif_id>/read", methods=["POST"])
@require_auth
def mark_read(notif_id):
    notif = Notification.query.filter_by(id=notif_id, user_id=g.current_user.id).first_or_404()
    notif.is_read = True
    db.session.commit()
    return jsonify({"success": True})


# ─────────────────────────────────────────────
# FCM DEVICE TOKEN REGISTRATION ROUTES
# ─────────────────────────────────────────────

@tasks_bp.route("/notifications/device-token", methods=["POST"])
@require_auth
def register_device_token():
    """Register or update an FCM device token for the current authenticated user."""
    data = request.get_json() or {}
    token = data.get("token", "").strip()
    platform = data.get("platform", "android").strip()

    if not token:
        return jsonify({"error": "Device token is required"}), 400

    existing = DeviceToken.query.filter_by(token=token).first()
    if existing:
        existing.user_id = g.current_user.id
        existing.platform = platform
        existing.updated_at = datetime.utcnow()
    else:
        dt = DeviceToken(
            user_id=g.current_user.id,
            token=token,
            platform=platform,
        )
        db.session.add(dt)

    db.session.commit()
    logger.info(f"Registered device token for user {g.current_user.email} (platform: {platform})")
    return jsonify({"success": True, "message": "Device token registered"})


@tasks_bp.route("/notifications/device-token", methods=["DELETE"])
@require_auth
def unregister_device_token():
    """Unregister an FCM device token (e.g. on user logout)."""
    data = request.get_json() or {}
    token = data.get("token", "").strip()
    if not token:
        return jsonify({"error": "Device token is required"}), 400

    DeviceToken.query.filter_by(token=token, user_id=g.current_user.id).delete()
    db.session.commit()
    logger.info(f"Unregistered device token for user {g.current_user.email}")
    return jsonify({"success": True, "message": "Device token removed"})


# ─────────────────────────────────────────────
# CALENDAR & DEADLINE REMINDERS
# ─────────────────────────────────────────────

def _check_team_deadline_reminders(team_id):
    """Check tasks for impending deadlines or overdue status and send deduplicated notifications."""
    from datetime import timedelta
    now = datetime.utcnow()
    in_24h = now + timedelta(hours=24)
    tasks = Task.query.filter(
        Task.team_id == team_id,
        Task.deadline != None,
        Task.status.in_(["pending", "in_progress"])
    ).all()

    dispatched = 0
    for task in tasks:
        target_user = task.assignee_id or task.creator_id
        if not target_user:
            continue

        if task.deadline < now:
            notif_type = "deadline_overdue"
            title = f"⚠️ Task Overdue: {task.title}"
            body = f"This task was due on {task.deadline.strftime('%b %d, %H:%M')}. Please update its status."
        elif task.deadline <= in_24h:
            notif_type = "deadline_reminder"
            hours_left = max(1, int((task.deadline - now).total_seconds() // 3600))
            title = f"⏰ Task Due in {hours_left}h: {task.title}"
            body = f"Reminder: This task is due soon ({task.deadline.strftime('%b %d, %H:%M')})."
        else:
            continue

        cutoff = now - timedelta(hours=20)
        existing = Notification.query.filter(
            Notification.user_id == target_user,
            Notification.entity_id == task.id,
            Notification.notification_type == notif_type,
            Notification.created_at >= cutoff
        ).first()

        if not existing:
            _create_notification(
                user_id=target_user,
                team_id=team_id,
                title=title,
                message=body,
                notif_type=notif_type,
                entity_id=task.id,
                extra_data={"taskId": task.id, "deadline": task.deadline.isoformat()}
            )
            dispatched += 1

    if dispatched > 0:
        db.session.commit()
        logger.info(f"Dispatched {dispatched} deadline reminder notifications for team {team_id}")
    return dispatched


@tasks_bp.route("/teams/<team_id>/calendar", methods=["GET"])
@require_auth
def get_calendar_tasks(team_id):
    """Retrieve all tasks with deadlines for calendar view, grouped and filtered."""
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403

    now = datetime.utcnow()
    tasks = Task.query.filter(
        Task.team_id == team_id,
        Task.deadline != None
    ).order_by(Task.deadline.asc()).all()

    # Trigger deadline checks in background
    try:
        _check_team_deadline_reminders(team_id)
    except Exception as e:
        logger.warning(f"Error checking deadline reminders: {e}")

    calendar_data = []
    for t in tasks:
        t_dict = t.to_dict()
        is_overdue = t.deadline and t.deadline < now and t.status not in ("completed", "cancelled")
        t_dict["isOverdue"] = is_overdue
        t_dict["deadlineDate"] = t.deadline.strftime("%Y-%m-%d") if t.deadline else None
        calendar_data.append(t_dict)

    return jsonify({
        "tasks": calendar_data,
        "count": len(calendar_data),
        "overdueCount": sum(1 for t in calendar_data if t.get("isOverdue"))
    })


@tasks_bp.route("/teams/<team_id>/reminders/check", methods=["POST"])
@require_auth
def check_deadlines_manual(team_id):
    """Manually trigger deadline reminders check."""
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403

    dispatched = _check_team_deadline_reminders(team_id)
    return jsonify({"success": True, "remindersDispatched": dispatched})
