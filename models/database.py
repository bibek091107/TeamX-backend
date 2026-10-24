"""
AI Team Brain - Database Models
================================
Complete SQLAlchemy ORM models with relationships.
"""

from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import uuid

db = SQLAlchemy()


def generate_uuid():
    return str(uuid.uuid4())


# ─────────────────────────────────────────────
# USER & TEAM MODELS
# ─────────────────────────────────────────────

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    firebase_uid = db.Column(db.String(128), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    display_name = db.Column(db.String(100))
    avatar_url = db.Column(db.String(500))
    role = db.Column(db.String(20), default="member")
    is_active = db.Column(db.Boolean, default=True)
    email_verified = db.Column(db.Boolean, default=False)
    last_active_team_id = db.Column(db.String(36), db.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    team_memberships = db.relationship("TeamMember", back_populates="user", lazy="dynamic")
    tasks_assigned = db.relationship("Task", foreign_keys="Task.assignee_id", back_populates="assignee", lazy="dynamic")
    tasks_created = db.relationship("Task", foreign_keys="Task.creator_id", back_populates="creator", lazy="dynamic")
    messages = db.relationship("ChatMessage", back_populates="user", lazy="dynamic")
    activities = db.relationship("ActivityLog", back_populates="user", lazy="dynamic")
    device_tokens = db.relationship("DeviceToken", back_populates="user", lazy="dynamic", cascade="all, delete-orphan")
    task_pins = db.relationship("TaskPin", back_populates="user", lazy="dynamic", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "displayName": self.display_name,
            "avatarUrl": self.avatar_url,
            "role": self.role,
            "isActive": self.is_active,
            "emailVerified": self.email_verified,
            "lastActiveTeamId": self.last_active_team_id,
            "lastSeen": self.last_seen.isoformat() if self.last_seen else None,
            "createdAt": self.created_at.isoformat()
        }


class Team(db.Model):
    __tablename__ = "teams"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    avatar_url = db.Column(db.String(500))
    invite_code = db.Column(db.String(8), unique=True)
    # When True, joining this team via invite code creates a PENDING
    # membership that the team owner must approve. Sub-groups created from
    # the chat "+" button opt in; the main team created on onboarding does not.
    require_approval = db.Column(db.Boolean, default=False)
    created_by = db.Column(db.String(36), db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    members = db.relationship("TeamMember", back_populates="team", lazy="dynamic")
    tasks = db.relationship("Task", back_populates="team", lazy="dynamic")
    chat_rooms = db.relationship("ChatRoom", back_populates="team", lazy="dynamic")
    knowledge_items = db.relationship("KnowledgeItem", back_populates="team", lazy="dynamic")
    invitations = db.relationship("WorkspaceInvitation", back_populates="team", lazy="dynamic", cascade="all, delete-orphan")

    def to_dict(self, include_members=False):
        data = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "avatarUrl": self.avatar_url,
            "inviteCode": self.invite_code,
            "requireApproval": bool(self.require_approval),
            "createdBy": self.created_by,
            "memberCount": self.members.count(),
            "createdAt": self.created_at.isoformat()
        }
        if include_members:
            data["members"] = [m.to_dict() for m in self.members.all()]
        return data


class TeamMember(db.Model):
    __tablename__ = "team_members"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=False, index=True)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    role = db.Column(db.String(20), default="member")
    # "active" = full member, "pending" = join request awaiting owner approval.
    status = db.Column(db.String(20), default="active")
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    team = db.relationship("Team", back_populates="members")
    user = db.relationship("User", back_populates="team_memberships")

    def to_dict(self):
        return {
            "id": self.id,
            "teamId": self.team_id,
            "userId": self.user_id,
            "user": self.user.to_dict() if self.user else None,
            "role": self.role,
            "status": self.status,
            "joinedAt": self.joined_at.isoformat()
        }


# ─────────────────────────────────────────────
# TASK MODELS
# ─────────────────────────────────────────────

class Task(db.Model):
    __tablename__ = "tasks"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=False, index=True)
    creator_id = db.Column(db.String(36), db.ForeignKey("users.id"), index=True)
    assignee_id = db.Column(db.String(36), db.ForeignKey("users.id"), index=True)
    channel_id = db.Column(db.String(36), db.ForeignKey("chat_rooms.id"), index=True)
    title = db.Column(db.String(500), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(20), default="backlog")
    priority = db.Column(db.String(10), default="medium")
    deadline = db.Column(db.DateTime)
    deadline_text = db.Column(db.String(100))
    source = db.Column(db.String(50), default="manual")
    source_id = db.Column(db.String(36))
    source_message_id = db.Column(db.String(36))
    ai_confidence = db.Column(db.Float, default=1.0)
    follow_up_suggestions = db.Column(db.Text)
    completed_at = db.Column(db.DateTime)
    is_blocked = db.Column(db.Boolean, default=False)
    blocked_reason = db.Column(db.Text)
    blocked_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    team = db.relationship("Team", back_populates="tasks")
    assignee = db.relationship("User", foreign_keys=[assignee_id], back_populates="tasks_assigned")
    creator = db.relationship("User", foreign_keys=[creator_id], back_populates="tasks_created")
    channel = db.relationship("ChatRoom", back_populates="tasks")
    activity = db.relationship("TaskActivity", back_populates="task", lazy="dynamic", cascade="all, delete-orphan")
    subtasks = db.relationship("TaskSubtask", back_populates="task", lazy="dynamic", cascade="all, delete-orphan")
    labels = db.relationship("TaskLabel", back_populates="task", lazy="dynamic", cascade="all, delete-orphan")
    pins = db.relationship("TaskPin", back_populates="task", lazy="dynamic", cascade="all, delete-orphan")

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "teamId": self.team_id,
            "channelId": self.channel_id,
            "channel": {"id": self.channel.id, "name": self.channel.name} if self.channel else None,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "deadlineText": self.deadline_text,
            "assignee": self.assignee.to_dict() if self.assignee else None,
            "assigneeId": self.assignee_id,
            "creator": self.creator.to_dict() if self.creator else None,
            "source": self.source,
            "sourceId": self.source_id,
            "sourceMessageId": self.source_message_id,
            "aiConfidence": self.ai_confidence,
            "followUpSuggestions": json.loads(self.follow_up_suggestions) if self.follow_up_suggestions else [],
            "completedAt": self.completed_at.isoformat() if self.completed_at else None,
            "isBlocked": bool(self.is_blocked),
            "blockedReason": self.blocked_reason,
            "blockedAt": self.blocked_at.isoformat() if self.blocked_at else None,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat()
        }


# ─────────────────────────────────────────────
# CHAT MODELS
# ─────────────────────────────────────────────

class ChatRoom(db.Model):
    __tablename__ = "chat_rooms"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    room_type = db.Column(db.String(20), default="general")
    is_restricted = db.Column(db.Boolean, default=False)
    is_archived = db.Column(db.Boolean, default=False)
    created_by = db.Column(db.String(36), db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    team = db.relationship("Team", back_populates="chat_rooms")
    messages = db.relationship("ChatMessage", back_populates="room", lazy="dynamic", cascade="all, delete-orphan")
    members = db.relationship("ChannelMember", back_populates="room", lazy="dynamic", cascade="all, delete-orphan")
    tasks = db.relationship("Task", back_populates="channel", lazy="dynamic")

    def to_dict(self, user_id=None):
        data = {
            "id": self.id,
            "teamId": self.team_id,
            "name": self.name,
            "description": self.description,
            "roomType": self.room_type,
            "isRestricted": bool(self.is_restricted),
            "isArchived": bool(self.is_archived),
            "messageCount": self.messages.count(),
            "createdAt": self.created_at.isoformat()
        }
        if user_id:
            cm = ChannelMember.query.filter_by(room_id=self.id, user_id=user_id).first()
            data["membershipStatus"] = cm.status if cm else ("approved" if not self.is_restricted else "not_member")
            data["channelRole"] = cm.role if cm else "member"
        return data


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    room_id = db.Column(db.String(36), db.ForeignKey("chat_rooms.id"), nullable=False, index=True)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    content = db.Column(db.Text, nullable=False)
    message_type = db.Column(db.String(20), default="text")
    extra_data = db.Column(db.Text)  # renamed from 'metadata' (reserved word)
    reply_to_id = db.Column(db.String(36), db.ForeignKey("chat_messages.id"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    room = db.relationship("ChatRoom", back_populates="messages")
    user = db.relationship("User", back_populates="messages")
    reply_to = db.relationship("ChatMessage", remote_side=[id], uselist=False)
    reactions = db.relationship("MessageReaction", back_populates="message", lazy="dynamic", cascade="all, delete-orphan")
    attachments = db.relationship("FileAttachment", back_populates="message", lazy="dynamic", cascade="all, delete-orphan")

    def to_dict(self):
        import json
        from collections import Counter
        reaction_counts = Counter()
        for r in self.reactions.all():
            reaction_counts[r.emoji] += 1
        attachments_list = [a.to_dict() for a in self.attachments.all()]
        reply_preview = None
        if self.reply_to:
            reply_preview = {
                "id": self.reply_to.id,
                "content": (self.reply_to.content or "")[:120],
                "userId": self.reply_to.user_id,
                "userName": self.reply_to.user.display_name if self.reply_to.user else "User",
            }
        return {
            "id": self.id,
            "roomId": self.room_id,
            "userId": self.user_id,
            "user": self.user.to_dict() if self.user else None,
            "content": self.content,
            "messageType": self.message_type,
            "metadata": json.loads(self.extra_data) if self.extra_data else None,
            "replyTo": reply_preview,
            "reactions": dict(reaction_counts) if reaction_counts else {},
            "attachments": attachments_list if attachments_list else [],
            "createdAt": self.created_at.isoformat()
        }


# ─────────────────────────────────────────────
# KNOWLEDGE BASE MODELS
# ─────────────────────────────────────────────

class KnowledgeItem(db.Model):
    __tablename__ = "knowledge_items"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=False, index=True)
    uploaded_by = db.Column(db.String(36), db.ForeignKey("users.id"))
    title = db.Column(db.String(500), nullable=False)
    content = db.Column(db.Text)
    original_filename = db.Column(db.String(500))
    file_path = db.Column(db.String(1000))
    file_type = db.Column(db.String(20))
    file_size = db.Column(db.Integer)
    summary = db.Column(db.Text)
    key_points = db.Column(db.Text)
    vector_id = db.Column(db.String(100))
    processing_status = db.Column(db.String(20), default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    team = db.relationship("Team", back_populates="knowledge_items")

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "teamId": self.team_id,
            "title": self.title,
            "originalFilename": self.original_filename,
            "fileType": self.file_type,
            "fileSize": self.file_size,
            "summary": self.summary,
            "keyPoints": json.loads(self.key_points) if self.key_points else [],
            "processingStatus": self.processing_status,
            "uploadedBy": self.uploaded_by,
            "createdAt": self.created_at.isoformat()
        }


class TaskActivity(db.Model):
    __tablename__ = "task_activity"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    task_id = db.Column(db.String(36), db.ForeignKey("tasks.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), index=True)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), index=True)
    action = db.Column(db.String(50), nullable=False)
    previous_status = db.Column(db.String(20))
    new_status = db.Column(db.String(20))
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    task = db.relationship("Task", back_populates="activity")
    user = db.relationship("User")

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "taskId": self.task_id,
            "teamId": self.team_id,
            "userId": self.user_id,
            "user": self.user.to_dict() if self.user else None,
            "action": self.action,
            "previousStatus": self.previous_status,
            "newStatus": self.new_status,
            "details": json.loads(self.details) if self.details else None,
            "createdAt": self.created_at.isoformat()
        }


class TaskSubtask(db.Model):
    __tablename__ = "task_subtasks"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    task_id = db.Column(db.String(36), db.ForeignKey("tasks.id"), nullable=False, index=True)
    title = db.Column(db.String(500), nullable=False)
    is_completed = db.Column(db.Boolean, default=False)
    position = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    task = db.relationship("Task", back_populates="subtasks")

    def to_dict(self):
        return {
            "id": self.id,
            "taskId": self.task_id,
            "title": self.title,
            "isCompleted": bool(self.is_completed),
            "position": self.position,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat()
        }


class TaskLabel(db.Model):
    __tablename__ = "task_labels"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    task_id = db.Column(db.String(36), db.ForeignKey("tasks.id"), nullable=False, index=True)
    name = db.Column(db.String(50), nullable=False)
    color = db.Column(db.String(20), default="#6366f1")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    task = db.relationship("Task", back_populates="labels")

    def to_dict(self):
        return {
            "id": self.id,
            "taskId": self.task_id,
            "name": self.name,
            "color": self.color,
            "createdAt": self.created_at.isoformat()
        }


class TaskPin(db.Model):
    __tablename__ = "task_pins"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    task_id = db.Column(db.String(36), db.ForeignKey("tasks.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", back_populates="task_pins")
    task = db.relationship("Task", back_populates="pins")

    __table_args__ = (db.UniqueConstraint('user_id', 'task_id', name='unique_user_task_pin'),)

    def to_dict(self):
        return {
            "id": self.id,
            "userId": self.user_id,
            "taskId": self.task_id,
            "createdAt": self.created_at.isoformat()
        }

class ActivityLog(db.Model):
    __tablename__ = "activity_logs"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), index=True)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), index=True)
    action = db.Column(db.String(100), nullable=False)
    entity_type = db.Column(db.String(50))
    entity_id = db.Column(db.String(36))
    description = db.Column(db.Text)
    extra_data = db.Column(db.Text)  # renamed from 'metadata' (reserved word)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", back_populates="activities")

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "teamId": self.team_id,
            "userId": self.user_id,
            "user": self.user.to_dict() if self.user else None,
            "action": self.action,
            "entityType": self.entity_type,
            "entityId": self.entity_id,
            "description": self.description,
            "metadata": json.loads(self.extra_data) if self.extra_data else None,
            "createdAt": self.created_at.isoformat()
        }


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), index=True)
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text)
    notification_type = db.Column(db.String(50))
    entity_id = db.Column(db.String(36))
    extra_data = db.Column(db.Text)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "userId": self.user_id,
            "teamId": self.team_id,
            "title": self.title,
            "message": self.message,
            "type": self.notification_type,
            "entityId": self.entity_id,
            "metadata": json.loads(self.extra_data) if self.extra_data else None,
            "isRead": self.is_read,
            "createdAt": self.created_at.isoformat()
        }


class DailySummary(db.Model):
    __tablename__ = "daily_summaries"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=False, index=True)
    summary_date = db.Column(db.Date, nullable=False)
    content = db.Column(db.Text)
    stats = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "teamId": self.team_id,
            "summaryDate": self.summary_date.isoformat(),
            "content": self.content,
            "stats": json.loads(self.stats) if self.stats else {},
            "createdAt": self.created_at.isoformat()
        }


# ─────────────────────────────────────────────
# CHANNEL MEMBERSHIP & APPROVAL MODELS
# ─────────────────────────────────────────────

class ChannelMember(db.Model):
    __tablename__ = "channel_members"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    room_id = db.Column(db.String(36), db.ForeignKey("chat_rooms.id"), nullable=False, index=True)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    role = db.Column(db.String(20), default="member")  # "admin" | "member"
    status = db.Column(db.String(20), default="approved")  # "pending" | "approved" | "rejected"
    requested_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_at = db.Column(db.DateTime)
    approved_by = db.Column(db.String(36), db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    room = db.relationship("ChatRoom", back_populates="members")
    user = db.relationship("User", foreign_keys=[user_id])
    approver = db.relationship("User", foreign_keys=[approved_by])

    def to_dict(self):
        return {
            "id": self.id,
            "roomId": self.room_id,
            "userId": self.user_id,
            "user": self.user.to_dict() if self.user else None,
            "role": self.role,
            "status": self.status,
            "requestedAt": self.requested_at.isoformat() if self.requested_at else None,
            "approvedAt": self.approved_at.isoformat() if self.approved_at else None,
            "approvedBy": self.approved_by,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat()
        }


# ─────────────────────────────────────────────
# INVITATION MODELS
# ─────────────────────────────────────────────

class WorkspaceInvitation(db.Model):
    __tablename__ = "workspace_invitations"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=False, index=True)
    channel_id = db.Column(db.String(36), db.ForeignKey("chat_rooms.id"), index=True)
    inviter_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    role = db.Column(db.String(20), default="member")
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    status = db.Column(db.String(20), default="pending")  # "pending" | "accepted" | "expired" | "revoked"
    expires_at = db.Column(db.DateTime, nullable=False)
    accepted_at = db.Column(db.DateTime)
    accepted_by = db.Column(db.String(36), db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    team = db.relationship("Team", back_populates="invitations")
    channel = db.relationship("ChatRoom")
    inviter = db.relationship("User", foreign_keys=[inviter_id])
    acceptor = db.relationship("User", foreign_keys=[accepted_by])

    def to_dict(self):
        return {
            "id": self.id,
            "teamId": self.team_id,
            "teamName": self.team.name if self.team else "Workspace",
            "team": {"id": self.team.id, "name": self.team.name} if self.team else None,
            "channelId": self.channel_id,
            "channelName": self.channel.name if self.channel else None,
            "channel": {"id": self.channel.id, "name": self.channel.name} if self.channel else None,
            "inviterId": self.inviter_id,
            "inviterName": self.inviter.display_name if self.inviter else "Admin",
            "inviter": {"id": self.inviter.id, "displayName": self.inviter.display_name, "email": self.inviter.email} if self.inviter else None,
            "email": self.email,
            "role": self.role,
            "status": self.status,
            "expiresAt": self.expires_at.isoformat() if self.expires_at else None,
            "acceptedAt": self.accepted_at.isoformat() if self.accepted_at else None,
            "createdAt": self.created_at.isoformat()
        }


# ─────────────────────────────────────────────
# MESSAGE REACTION MODELS
# ─────────────────────────────────────────────

class MessageReaction(db.Model):
    __tablename__ = "message_reactions"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    message_id = db.Column(db.String(36), db.ForeignKey("chat_messages.id"), nullable=False, index=True)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    emoji = db.Column(db.String(10), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    message = db.relationship("ChatMessage", back_populates="reactions")
    user = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "messageId": self.message_id,
            "userId": self.user_id,
            "emoji": self.emoji,
            "userName": self.user.display_name if self.user else None,
            "createdAt": self.created_at.isoformat()
        }


# ─────────────────────────────────────────────
# FILE ATTACHMENT MODELS
# ─────────────────────────────────────────────

class FileAttachment(db.Model):
    __tablename__ = "file_attachments"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    message_id = db.Column(db.String(36), db.ForeignKey("chat_messages.id"), index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=False, index=True)
    uploader_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False)
    filename = db.Column(db.String(500), nullable=False)
    original_filename = db.Column(db.String(500), nullable=False)
    file_path = db.Column(db.String(1000), nullable=False)
    file_type = db.Column(db.String(100))
    file_size = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    message = db.relationship("ChatMessage", back_populates="attachments")
    uploader = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "messageId": self.message_id,
            "teamId": self.team_id,
            "uploaderId": self.uploader_id,
            "uploaderName": self.uploader.display_name if self.uploader else None,
            "filename": self.filename,
            "originalFilename": self.original_filename,
            "filePath": self.file_path,
            "fileType": self.file_type,
            "fileSize": self.file_size,
            "createdAt": self.created_at.isoformat()
        }


# ─────────────────────────────────────────────
# DEVICE TOKEN MODELS (FCM PUSH)
# ─────────────────────────────────────────────

class DeviceToken(db.Model):
    __tablename__ = "device_tokens"

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    token = db.Column(db.String(500), unique=True, nullable=False, index=True)
    platform = db.Column(db.String(20), default="android")  # "android" | "web" | "ios"
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship("User", back_populates="device_tokens")

    def to_dict(self):
        return {
            "id": self.id,
            "userId": self.user_id,
            "token": self.token,
            "platform": self.platform,
            "createdAt": self.created_at.isoformat()
        }
