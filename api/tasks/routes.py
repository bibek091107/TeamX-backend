"""
AI Team Brain — Tasks Routes
Full Kanban + Focus Mode implementation
"""

from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, g
from models.database import db, Task, ActivityLog, TaskActivity, TaskSubtask, TaskLabel, TaskPin, TeamMember, Notification, ChatRoom, User, DeviceToken, ChatMessage
from utils.auth_middleware import require_auth
from services.push_service import send_push_notification
from loguru import logger
import json

tasks_bp = Blueprint("tasks", __name__)


def _check_member(team_id, user_id):
    if not team_id or not user_id:
        return None
    m = TeamMember.query.filter_by(team_id=team_id, user_id=user_id).first()
    if not m:
        return None
    return m if (m.status or "active") == "active" else None


def _is_admin(team_id, user_id):
    if not team_id or not user_id:
        return False
    u = User.query.get(user_id)
    if u and getattr(u, "role", None) == "admin":
        return True
    m = TeamMember.query.filter_by(team_id=team_id, user_id=user_id).first()
    if not m or (m.status or "active") != "active":
        return False
    return m.role in ("owner", "admin")


def _is_valid_assignee(team_id, assignee_id):
    if not assignee_id:
        return True
    return TeamMember.query.filter_by(team_id=team_id, user_id=assignee_id).first() is not None


def _emit_socket(event: str, data: dict, room: str):
    try:
        from app import socketio
        socketio.emit(event, data, room=room)
    except Exception as e:
        logger.warning(f"Socket emit failed ({event}): {e}")


def _create_notification(user_id, team_id, title, message, notif_type, entity_id=None, extra_data=None):
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
    _emit_socket("new_notification", notif_dict, f"user_{user_id}")
    try:
        send_push_notification(user_id=user_id, title=title, body=message, extra_data=extra_dict)
    except Exception:
        pass
    return notif


def _log_activity(task, user_id, team_id, action, previous_status=None, new_status=None, details=None):
    activity = TaskActivity(
        task_id=task.id,
        team_id=team_id,
        user_id=user_id,
        action=action,
        previous_status=previous_status,
        new_status=new_status,
        details=json.dumps(details) if details else None
    )
    db.session.add(activity)
    return activity


VALID_STATUSES = ["backlog", "to_do", "in_progress", "in_review", "done"]
STATUS_ALIASES = {"pending": "to_do", "completed": "done"}


def _normalize_status(status):
    if not status:
        return "backlog"
    s = status.lower().replace(" ", "_")
    if s in STATUS_ALIASES:
        s = STATUS_ALIASES[s]
    return s if s in VALID_STATUSES else status


@tasks_bp.route("/teams/<team_id>/tasks", methods=["GET"])
@require_auth
def get_tasks(team_id):
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403

    status      = request.args.get("status")
    priority    = request.args.get("priority")
    assignee_id = request.args.get("assigneeId")
    channel_id  = request.args.get("channelId") or request.args.get("channel_id")
    search      = request.args.get("search") or request.args.get("q")
    sort_by     = request.args.get("sortBy") or request.args.get("sort")
    limit       = int(request.args.get("limit", 200))

    query = Task.query.filter_by(team_id=team_id)
    if status:
        status = _normalize_status(status)
        query = query.filter_by(status=status)
    if priority:    query = query.filter_by(priority=priority)
    if assignee_id: query = query.filter_by(assignee_id=assignee_id)
    if channel_id:  query = query.filter_by(channel_id=channel_id)
    if search:
        search_term = f"%{search}%"
        query = query.filter(db.or_(Task.title.ilike(search_term), Task.description.ilike(search_term)))

    if sort_by == "priority":
        priority_order = db.case(
            (Task.priority == 'critical', 1),
            (Task.priority == 'high', 2),
            (Task.priority == 'medium', 3),
            (Task.priority == 'low', 4),
            else_=5
        )
        query = query.order_by(priority_order.asc(), Task.created_at.desc())
    elif sort_by == "due_date" or sort_by == "deadline":
        query = query.order_by(Task.deadline.asc().nullslast(), Task.created_at.desc())
    else:
        query = query.order_by(Task.created_at.desc())

    tasks = query.limit(limit).all()
    result = []
    for t in tasks:
        d = t.to_dict()
        d["subtasks"] = [s.to_dict() for s in t.subtasks.order_by(TaskSubtask.position).all()]
        d["labels"] = [l.to_dict() for l in t.labels.all()]
        d["overdue"] = bool(t.deadline and t.deadline < datetime.utcnow() and t.status != 'done')
        result.append(d)
    return jsonify({"tasks": result})


@tasks_bp.route("/teams/<team_id>/tasks/assigned/me", methods=["GET"])
@require_auth
def get_my_tasks(team_id):
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403
    tasks = Task.query.filter_by(team_id=team_id, assignee_id=g.current_user.id).order_by(Task.created_at.desc()).all()
    result = []
    for t in tasks:
        d = t.to_dict()
        d["subtasks"] = [s.to_dict() for s in t.subtasks.order_by(TaskSubtask.position).all()]
        d["labels"] = [l.to_dict() for l in t.labels.all()]
        d["overdue"] = bool(t.deadline and t.deadline < datetime.utcnow() and t.status != 'done')
        result.append(d)
    return jsonify({"tasks": result})


@tasks_bp.route("/tasks/assigned/me", methods=["GET"])
@require_auth
def get_my_tasks_all():
    user_id = g.current_user.id
    # Get all teams user is member of
    memberships = TeamMember.query.filter_by(user_id=user_id).all()
    team_ids = [m.team_id for m in memberships if (m.status or "active") == "active"]
    if not team_ids:
        return jsonify({"tasks": []})
    tasks = Task.query.filter(Task.team_id.in_(team_ids), Task.assignee_id == user_id).order_by(Task.created_at.desc()).all()
    result = []
    for t in tasks:
        d = t.to_dict()
        d["subtasks"] = [s.to_dict() for s in t.subtasks.order_by(TaskSubtask.position).all()]
        d["labels"] = [l.to_dict() for l in t.labels.all()]
        d["overdue"] = bool(t.deadline and t.deadline < datetime.utcnow() and t.status != 'done')
        result.append(d)
    return jsonify({"tasks": result})


@tasks_bp.route("/teams/<team_id>/tasks", methods=["POST"])
@require_auth
def create_task(team_id):
    if not _is_admin(team_id, g.current_user.id):
        return jsonify({"error": "Only team admins can create and assign tasks"}), 403

    data = request.get_json() or {}

    assignee_id = data.get("assigneeId") or data.get("assignee_id")
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
            dl = data["deadline"]
            if isinstance(dl, str) and dl:
                deadline = datetime.fromisoformat(dl.replace("Z", "+00:00").replace("+00:00", ""))
        except Exception:
            pass

    status = _normalize_status(data.get("status", "backlog"))
    if status not in VALID_STATUSES:
        status = "backlog"

    priority = (data.get("priority") or "medium").lower()
    if priority not in ["low", "medium", "high", "critical"]:
        priority = "medium"

    source_message_id = data.get("sourceMessageId") or data.get("source_message_id")

    task = Task(
        team_id=team_id,
        creator_id=g.current_user.id,
        assignee_id=assignee_id,
        channel_id=channel_id,
        title=data.get("title", "Untitled task")[:500],
        description=data.get("description", "") or "",
        status=status,
        priority=priority,
        deadline=deadline,
        deadline_text=data.get("deadlineText") or data.get("deadline_text"),
        source=data.get("source", "manual"),
        source_message_id=source_message_id,
    )
    db.session.add(task)
    db.session.flush()

    _log_activity(task, g.current_user.id, team_id, "task_created", details={"title": task.title})
    db.session.add(ActivityLog(
        team_id=team_id, user_id=g.current_user.id,
        action="task_created", entity_type="task", entity_id=task.id,
        description=f"{g.current_user.display_name} created: {task.title}",
    ))

    if task.assignee_id and task.assignee_id != g.current_user.id:
        try:
            _create_notification(
                user_id=task.assignee_id, team_id=team_id,
                title="📋 New task assigned to you",
                message=f"{g.current_user.display_name} assigned: {task.title}",
                notif_type="task", entity_id=task.id,
                extra_data={"taskId": task.id, "channelId": channel_id},
            )
        except Exception:
            pass

    db.session.commit()
    task_dict = task.to_dict()
    task_dict["subtasks"] = []
    task_dict["labels"] = []
    _emit_socket("new_task", task_dict, f"team_{team_id}")
    return jsonify({"task": task_dict}), 201


@tasks_bp.route("/teams/<team_id>/tasks/<task_id>", methods=["GET"])
@require_auth
def get_task_detail(team_id, task_id):
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403
    task = Task.query.filter_by(id=task_id, team_id=team_id).first_or_404()
    d = task.to_dict()
    d["subtasks"] = [s.to_dict() for s in task.subtasks.order_by(TaskSubtask.position).all()]
    d["labels"] = [l.to_dict() for l in task.labels.all()]
    d["overdue"] = bool(task.deadline and task.deadline < datetime.utcnow() and task.status != 'done')
    activities = TaskActivity.query.filter_by(task_id=task_id).order_by(TaskActivity.created_at.desc()).limit(50).all()
    d["activity"] = [a.to_dict() for a in activities]
    return jsonify({"task": d})


@tasks_bp.route("/teams/<team_id>/tasks/<task_id>", methods=["PUT", "PATCH"])
@require_auth
def update_task(team_id, task_id):
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403

    task = Task.query.filter_by(id=task_id, team_id=team_id).first_or_404()
    data = request.get_json() or {}

    is_team_admin = _is_admin(team_id, g.current_user.id)
    is_assignee = task.assignee_id == g.current_user.id

    if not is_team_admin:
        if not is_assignee:
            return jsonify({"error": "Members can only update status for tasks assigned to them"}), 403
        restricted_keys = ["title", "description", "priority", "assigneeId", "assignee_id", "deadline", "channelId", "channel_id"]
        if any(k in data for k in restricted_keys):
            return jsonify({"error": "Only team admins can modify task details or reassign tasks"}), 403

    previous_status = task.status

    if "status" in data:
        new_status = _normalize_status(data["status"])
        if new_status not in VALID_STATUSES:
            return jsonify({"error": "Invalid status", "allowed": VALID_STATUSES}), 400
        if task.status != new_status:
            task.status = new_status
            _log_activity(task, g.current_user.id, team_id, "status_changed", previous_status, new_status)
            if new_status == "done":
                task.completed_at = datetime.utcnow()
            elif new_status in ["backlog", "to_do", "in_progress", "in_review"]:
                task.completed_at = None

    if is_team_admin:
        old_assignee = task.assignee_id
        if "title" in data and data["title"] is not None:
            task.title = str(data["title"])[:500]
        if "description" in data:
            task.description = data["description"] or ""
        if "priority" in data:
            p = str(data["priority"]).lower()
            if p in ["low", "medium", "high", "critical"]:
                task.priority = p
        if "assigneeId" in data or "assignee_id" in data:
            new_assignee_id = data.get("assigneeId") or data.get("assignee_id")
            if new_assignee_id and not _is_valid_assignee(team_id, new_assignee_id):
                return jsonify({"error": "Assignee must be a member of this team"}), 400
            task.assignee_id = new_assignee_id
            if new_assignee_id and new_assignee_id != old_assignee and new_assignee_id != g.current_user.id:
                try:
                    _create_notification(user_id=new_assignee_id, team_id=team_id,
                                        title="📋 Task assigned to you",
                                        message=f"{g.current_user.display_name} assigned: {task.title}",
                                        notif_type="task", entity_id=task.id,
                                        extra_data={"taskId": task.id})
                except Exception:
                    pass
        if "channelId" in data or "channel_id" in data:
            new_chan_id = data.get("channelId") or data.get("channel_id")
            if new_chan_id:
                if not ChatRoom.query.filter_by(id=new_chan_id, team_id=team_id).first():
                    return jsonify({"error": "Channel not found in this team"}), 400
            task.channel_id = new_chan_id
        if "deadline" in data:
            if data["deadline"] is None or data["deadline"] == "":
                task.deadline = None
            else:
                try:
                    dl = data["deadline"]
                    if isinstance(dl, str):
                        try:
                            task.deadline = datetime.fromisoformat(dl.replace("Z", "+00:00").replace("+00:00", ""))
                        except Exception:
                            pass
                except Exception:
                    pass
        if "deadlineText" in data or "deadline_text" in data:
            task.deadline_text = data.get("deadlineText") or data.get("deadline_text")

    if "isBlocked" in data or "is_blocked" in data or "blocked" in data:
        blocked = data.get("isBlocked", data.get("is_blocked", data.get("blocked")))
        if blocked and not task.is_blocked:
            task.is_blocked = True
            task.blocked_at = datetime.utcnow()
            task.blocked_reason = data.get("blockedReason") or data.get("blocked_reason") or ""
            _log_activity(task, g.current_user.id, team_id, "blocked", details={"reason": task.blocked_reason})
        elif not blocked and task.is_blocked:
            task.is_blocked = False
            task.blocked_reason = data.get("blockedReason") or data.get("blocked_reason") or ""
            _log_activity(task, g.current_user.id, team_id, "unblocked")

    db.session.add(ActivityLog(
        team_id=team_id, user_id=g.current_user.id,
        action="task_updated", entity_type="task", entity_id=task.id,
        description=f"{g.current_user.display_name} updated: {task.title}",
    ))
    db.session.commit()

    task_dict = task.to_dict()
    task_dict["subtasks"] = [s.to_dict() for s in task.subtasks.order_by(TaskSubtask.position).all()]
    task_dict["labels"] = [l.to_dict() for l in task.labels.all()]
    _emit_socket("task_updated", task_dict, f"team_{team_id}")
    return jsonify({"task": task_dict})


@tasks_bp.route("/teams/<team_id>/tasks/<task_id>/activity", methods=["GET"])
@require_auth
def get_task_activity(team_id, task_id):
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403
    Task.query.filter_by(id=task_id, team_id=team_id).first_or_404()
    activities = TaskActivity.query.filter_by(task_id=task_id).order_by(TaskActivity.created_at.desc()).all()
    return jsonify({"activity": [a.to_dict() for a in activities]})


@tasks_bp.route("/teams/<team_id>/tasks/<task_id>/block", methods=["PUT", "POST"])
@require_auth
def update_blocker(team_id, task_id):
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403
    task = Task.query.filter_by(id=task_id, team_id=team_id).first_or_404()
    data = request.get_json() or {}
    blocked = data.get("blocked", data.get("isBlocked", True))
    if blocked:
        task.is_blocked = True
        task.blocked_at = datetime.utcnow()
        task.blocked_reason = data.get("reason") or data.get("blockedReason") or ""
        _log_activity(task, g.current_user.id, team_id, "blocked", details={"reason": task.blocked_reason})
    else:
        task.is_blocked = False
        task.blocked_reason = data.get("reason") or data.get("blockedReason") or ""
        _log_activity(task, g.current_user.id, team_id, "unblocked")
    db.session.commit()
    task_dict = task.to_dict()
    _emit_socket("task_updated", task_dict, f"team_{team_id}")
    return jsonify({"task": task_dict})


@tasks_bp.route("/teams/<team_id>/tasks/<task_id>/status", methods=["PUT", "POST"])
@require_auth
def change_task_status(team_id, task_id):
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403
    task = Task.query.filter_by(id=task_id, team_id=team_id).first_or_404()
    data = request.get_json() or {}
    new_status = _normalize_status(data.get("status"))
    if new_status not in VALID_STATUSES:
        return jsonify({"error": "Invalid status", "allowed": VALID_STATUSES}), 400
    is_team_admin = _is_admin(team_id, g.current_user.id)
    is_assignee = task.assignee_id == g.current_user.id
    if not is_team_admin and not is_assignee:
        return jsonify({"error": "Not authorized to change status"}), 403
    previous_status = task.status
    if previous_status != new_status:
        task.status = new_status
        _log_activity(task, g.current_user.id, team_id, "status_changed", previous_status, new_status)
        if new_status == "done":
            task.completed_at = datetime.utcnow()
        else:
            task.completed_at = None
        db.session.add(ActivityLog(
            team_id=team_id, user_id=g.current_user.id,
            action="task_status_changed", entity_type="task", entity_id=task.id,
            description=f"{g.current_user.display_name} moved {task.title} from {previous_status} to {new_status}",
        ))
    db.session.commit()
    task_dict = task.to_dict()
    _emit_socket("task_updated", task_dict, f"team_{team_id}")
    return jsonify({"task": task_dict})


@tasks_bp.route("/teams/<team_id>/tasks/from-message", methods=["POST"])
@require_auth
def create_task_from_message(team_id):
    if not _is_admin(team_id, g.current_user.id):
        return jsonify({"error": "Only team admins can create and assign tasks"}), 403
    data = request.get_json() or {}
    message_id = data.get("messageId") or data.get("message_id")
    if not message_id:
        return jsonify({"error": "messageId is required"}), 400
    msg = ChatMessage.query.filter_by(id=message_id).first()
    if not msg:
        return jsonify({"error": "Message not found"}), 404
    room = ChatRoom.query.filter_by(id=msg.room_id, team_id=team_id).first()
    if not room:
        return jsonify({"error": "Message not in this team"}), 403
    # Avoid duplicates
    existing = Task.query.filter_by(team_id=team_id, source_message_id=message_id).first()
    if existing:
        return jsonify({"task": existing.to_dict(), "existing": True}), 200

    assignee_id = data.get("assigneeId") or data.get("assignee_id")
    if assignee_id and not _is_valid_assignee(team_id, assignee_id):
        return jsonify({"error": "Assignee must be a member of this team"}), 400

    title = (data.get("title") or msg.content or "Task from message")[:500]
    description = data.get("description") or msg.content or ""
    priority = (data.get("priority") or "medium").lower()
    if priority not in ["low", "medium", "high", "critical"]:
        priority = "medium"

    deadline = None
    if data.get("deadline"):
        try:
            dl = data["deadline"]
            if isinstance(dl, str) and dl:
                deadline = datetime.fromisoformat(dl.replace("Z", "+00:00").replace("+00:00", ""))
        except Exception:
            pass

    task = Task(
        team_id=team_id,
        creator_id=g.current_user.id,
        assignee_id=assignee_id,
        channel_id=msg.room_id,
        title=title,
        description=description,
        status=_normalize_status(data.get("status", "backlog")),
        priority=priority,
        deadline=deadline,
        source="message",
        source_message_id=message_id,
    )
    db.session.add(task)
    db.session.flush()
    _log_activity(task, g.current_user.id, team_id, "task_created_from_message", details={"messageId": message_id})
    db.session.add(ActivityLog(team_id=team_id, user_id=g.current_user.id, action="task_created", entity_type="task", entity_id=task.id,
                               description=f"{g.current_user.display_name} created task from message"))
    if task.assignee_id and task.assignee_id != g.current_user.id:
        try:
            _create_notification(user_id=task.assignee_id, team_id=team_id, title="📋 New task assigned to you",
                                message=f"{g.current_user.display_name} assigned: {task.title}", notif_type="task", entity_id=task.id,
                                extra_data={"taskId": task.id})
        except Exception:
            pass
    db.session.commit()
    task_dict = task.to_dict()
    task_dict["subtasks"] = []
    task_dict["labels"] = []
    _emit_socket("new_task", task_dict, f"team_{team_id}")
    return jsonify({"task": task_dict}), 201


@tasks_bp.route("/teams/<team_id>/pins", methods=["GET"])
@require_auth
def get_pins(team_id):
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403
    pins = TaskPin.query.filter_by(user_id=g.current_user.id).all()
    pin_task_ids = [p.task_id for p in pins]
    return jsonify({"pins": pin_task_ids})


@tasks_bp.route("/teams/<team_id>/pins/<task_id>", methods=["POST", "PUT"])
@require_auth
def add_pin(team_id, task_id):
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403
    task = Task.query.filter_by(id=task_id, team_id=team_id).first_or_404()
    existing = TaskPin.query.filter_by(user_id=g.current_user.id, task_id=task_id).first()
    if not existing:
        pin = TaskPin(user_id=g.current_user.id, task_id=task_id)
        db.session.add(pin)
        db.session.commit()
    return jsonify({"pinned": True})


@tasks_bp.route("/teams/<team_id>/pins/<task_id>", methods=["DELETE"])
@require_auth
def remove_pin(team_id, task_id):
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403
    TaskPin.query.filter_by(user_id=g.current_user.id, task_id=task_id).delete()
    db.session.commit()
    return jsonify({"pinned": False})


@tasks_bp.route("/teams/<team_id>/focus", methods=["GET"])
@require_auth
def get_focus_dashboard(team_id):
    if not _check_member(team_id, g.current_user.id):
        return jsonify({"error": "Access denied"}), 403
    user_id = g.current_user.id
    now = datetime.utcnow()
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    week_end = now + timedelta(days=7)

    # Get all team tasks
    team_tasks = Task.query.filter_by(team_id=team_id).all()
    my_tasks = [t for t in team_tasks if t.assignee_id == user_id]

    # Pinned tasks
    pins = TaskPin.query.filter_by(user_id=user_id).all()
    pinned_ids = set(p.task_id for p in pins)
    pinned_tasks = []
    for t in my_tasks:
        if t.id in pinned_ids:
            d = t.to_dict()
            d["pinReason"] = "You pinned this task."
            pinned_tasks.append(d)

    # Today's priorities (up to 3)
    priorities = []
    # Score tasks
    candidates = []
    for t in my_tasks:
        score = 0
        reasons = []
        if t.deadline and t.deadline < now and t.status != 'done':
            score += 100
            days = (now - t.deadline).days
            reasons.append(f"Overdue by {days} day{'s' if days != 1 else ''}.")
        elif t.deadline and t.deadline <= today_end and t.status != 'done':
            score += 80
            reasons.append("Due today.")
        elif t.deadline and t.deadline <= week_end and t.status != 'done':
            score += 40
            reasons.append("Due soon.")
        if t.priority == 'critical':
            score += 60
            reasons.append("Critical priority.")
        elif t.priority == 'high':
            score += 40
            reasons.append("High priority.")
        if t.status == 'in_progress':
            score += 20
            reasons.append("Already in progress.")
        if t.is_blocked:
            score -= 10  # blocked - needs resolution
            reasons.append("Blocked - needs resolution.")
        if t.id in pinned_ids:
            score += 50
            reasons.append("You pinned this task.")
        if score > 0:
            candidates.append((score, t, reasons))
    candidates.sort(reverse=True, key=lambda x: x[0])
    for score, t, reasons in candidates[:3]:
        d = t.to_dict()
        d["priorityReason"] = reasons[0] if reasons else "Recommended for you."
        d["priorityReasons"] = reasons
        priorities.append(d)

    # Group my tasks
    my_tasks_grouped = {
        "to_do": [],
        "in_progress": [],
        "in_review": [],
        "done": []
    }
    for t in my_tasks:
        d = t.to_dict()
        d["overdue"] = bool(t.deadline and t.deadline < now and t.status != 'done')
        status_key = t.status if t.status in my_tasks_grouped else ('to_do' if t.status == 'pending' else t.status)
        if status_key in my_tasks_grouped:
            my_tasks_grouped[status_key].append(d)
        elif status_key == 'backlog':
            my_tasks_grouped["to_do"].append(d)
        elif status_key == 'done':
            my_tasks_grouped["done"].append(d)

    # Due soon
    due_today = []
    due_week = []
    overdue = []
    for t in my_tasks:
        if t.status == 'done':
            continue
        if t.deadline:
            if t.deadline < now:
                d = t.to_dict()
                days = (now - t.deadline).days
                d["overdueDays"] = days
                overdue.append(d)
            elif t.deadline <= today_end:
                d = t.to_dict()
                due_today.append(d)
            elif t.deadline <= week_end:
                d = t.to_dict()
                due_week.append(d)
    due_soon = {"today": due_today, "week": due_week, "overdue": overdue}

    # Blocked tasks
    blocked = []
    for t in my_tasks:
        if t.is_blocked:
            d = t.to_dict()
            blocked.append(d)

    # Recent team activity (tasks)
    recent = []
    for t in sorted(team_tasks, key=lambda x: x.updated_at, reverse=True)[:10]:
        recent.append({
            "type": "task",
            "task": t.to_dict(),
            "updatedAt": t.updated_at.isoformat() if t.updated_at else None
        })

    return jsonify({
        "priorities": priorities,
        "pinned": pinned_tasks,
        "myTasks": my_tasks_grouped,
        "dueSoon": due_soon,
        "blocked": blocked,
        "recentActivity": recent
    })
