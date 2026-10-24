"""
AI Team Brain — Auth Routes
Fix 2: Added delete team endpoint for team creator.
"""

import os
import random
import string
import secrets
import hashlib
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, g
from flask_jwt_extended import create_access_token, create_refresh_token, jwt_required, get_jwt_identity
from loguru import logger
from models.database import (
    db, User, Team, TeamMember, ActivityLog, ChatRoom, Task,
    KnowledgeItem, Notification, ChatMessage, DailySummary,
    WorkspaceInvitation, ChannelMember
)
from utils.auth_middleware import require_auth, verify_firebase_token, firebase_status

auth_bp = Blueprint("auth", __name__)


def _generate_invite_code(length=8):
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


from services.firestore_service import (
    save_team_to_firestore,
    delete_team_from_firestore,
    save_membership_to_firestore,
    remove_membership_from_firestore,
    sync_user_teams,
)


# ── Firebase token exchange ────────────────────────────────────

@auth_bp.route("/firebase-login", methods=["POST"])
def firebase_login():
    data         = request.get_json(silent=True) or {}
    id_token     = data.get("idToken", "")
    norm_email   = ""
    display_name = ""
    firebase_uid = None
    avatar_url   = ""
    email_verified = False

    if not isinstance(id_token, str) or not id_token.strip():
        return jsonify({"error": "A Firebase ID token is required.", "code": "missing_id_token"}), 400

    # Try real Firebase verification first
    try:
        decoded        = verify_firebase_token(id_token)
        firebase_uid   = decoded["uid"]
        norm_email     = (decoded.get("email") or "").strip().lower()
        display_name   = decoded.get("name") or norm_email.split("@")[0]
        avatar_url     = decoded.get("picture", "")
        email_verified = bool(decoded.get("email_verified", False))
        logger.info(f"Firebase auth OK: {norm_email} (verified={email_verified})")
    except Exception:
        logger.warning("Rejected unverifiable Firebase ID token")
        status = firebase_status()
        if not status["ready"]:
            return jsonify({
                "error": "Server authentication is temporarily unavailable. Please contact the admin.",
                "code": "firebase_not_configured",
                "diagnostic": status["reason"],
                "missingConfiguration": status["missing_env_vars"],
            }), 503
        return jsonify({
            "error": "Invalid or expired authentication token. Please sign in again.",
            "code": "invalid_token",
        }), 401

    if not norm_email:
        return jsonify({"error": "Email is required"}), 400

    # Resilient user lookup:
    # 1. Try finding by firebase_uid
    # 2. If not found, lookup by case-insensitive email to link pre-existing accounts
    user = User.query.filter_by(firebase_uid=firebase_uid).first()
    if not user and norm_email:
        user = User.query.filter(db.func.lower(User.email) == norm_email).first()

    if not user:
        user = User(
            firebase_uid=firebase_uid,
            email=norm_email,
            display_name=display_name or norm_email.split("@")[0],
            avatar_url=avatar_url,
            email_verified=email_verified,
        )
        db.session.add(user)
        logger.info(f"New user registered: {norm_email} (uid={firebase_uid})")
    else:
        # Link / update verified Firebase UID and credentials
        if user.firebase_uid != firebase_uid:
            logger.info(f"Linking existing account {user.email} (old uid={user.firebase_uid}) to new Firebase UID: {firebase_uid}")
            user.firebase_uid = firebase_uid

        user.email = norm_email
        user.last_seen = datetime.utcnow()
        if display_name:
            user.display_name = display_name
        user.email_verified = email_verified
        if avatar_url:
            user.avatar_url = avatar_url

    db.session.commit()

    # Reconcile team memberships persistently with Firestore cloud storage
    try:
        sync_user_teams(user)
    except Exception as sync_err:
        logger.warning(f"Firestore team sync warning on login: {sync_err}")

    # Gather user's verified teams
    memberships = TeamMember.query.filter_by(user_id=user.id).all()
    teams = []
    for m in memberships:
        if m.team:
            data = m.team.to_dict()
            data["membershipStatus"] = m.status or "active"
            teams.append(data)

    last_team_id = user.last_active_team_id
    if last_team_id:
        is_active_member = any(t["id"] == last_team_id and t.get("membershipStatus") == "active" for t in teams)
        if not is_active_member:
            last_team_id = teams[0]["id"] if teams else None
            user.last_active_team_id = last_team_id
            db.session.commit()

    access_token  = create_access_token(identity=user.id)
    refresh_token = create_refresh_token(identity=user.id)

    return jsonify({
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "user": user.to_dict(),
        "teams": teams,
        "lastActiveTeamId": last_team_id,
    })


# ── Token refresh ───────────────────────────────────────────────
# Access tokens are short-lived (see app.py JWT_ACCESS_TOKEN_EXPIRES). This
# endpoint lets the frontend silently exchange a still-valid refresh token
# for a new access token instead of forcing the user to log in again every
# time the access token expires. Requires a REFRESH token specifically
# (jwt_required(refresh=True)) — an access token cannot be used here, and
# a refresh token cannot be used on any other endpoint (require_auth calls
# verify_jwt_in_request() with its default refresh=False).

@auth_bp.route("/refresh", methods=["POST"])
@jwt_required(refresh=True)
def refresh():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if not user or not user.is_active:
        return jsonify({"error": "User not found or inactive"}), 401
    new_access_token = create_access_token(identity=user_id)
    return jsonify({"accessToken": new_access_token})


# ── Profile ───────────────────────────────────────────────────

@auth_bp.route("/me", methods=["GET"])
@require_auth
def get_profile():
    return jsonify({"user": g.current_user.to_dict()})


@auth_bp.route("/me", methods=["PUT"])
@require_auth
def update_profile():
    data = request.get_json() or {}
    user = g.current_user
    if "displayName" in data:
        user.display_name = data["displayName"]
    if "avatarUrl" in data:
        user.avatar_url = data["avatarUrl"]
    db.session.commit()
    return jsonify({"user": user.to_dict()})


# ── Teams ─────────────────────────────────────────────────────

@auth_bp.route("/teams", methods=["GET"])
@require_auth
def get_my_teams():
    # Sync persistent memberships from Firestore cloud storage
    try:
        sync_user_teams(g.current_user)
    except Exception as e:
        logger.warning(f"Firestore sync in get_my_teams: {e}")

    memberships = TeamMember.query.filter_by(user_id=g.current_user.id).all()
    teams = []
    for m in memberships:
        if m.team:
            data = m.team.to_dict()
            data["membershipStatus"] = m.status or "active"
            teams.append(data)

    last_team_id = g.current_user.last_active_team_id
    if last_team_id:
        is_active_member = any(t["id"] == last_team_id and t.get("membershipStatus") == "active" for t in teams)
        if not is_active_member:
            last_team_id = teams[0]["id"] if teams else None
            g.current_user.last_active_team_id = last_team_id
            db.session.commit()
    elif teams:
        # Default to first active team if last_team_id was null
        active_candidates = [t for t in teams if t.get("membershipStatus") == "active"]
        if active_candidates:
            last_team_id = active_candidates[0]["id"]
            g.current_user.last_active_team_id = last_team_id
            db.session.commit()

    return jsonify({
        "teams": teams,
        "lastActiveTeamId": last_team_id
    })


@auth_bp.route("/teams/select", methods=["POST"])
@require_auth
def select_team():
    data = request.get_json() or {}
    team_id = (data.get("teamId") or "").strip()
    if not team_id:
        return jsonify({"error": "Team ID required"}), 400

    membership = TeamMember.query.filter_by(team_id=team_id, user_id=g.current_user.id).first()
    if not membership or (membership.status or "active") != "active":
        return jsonify({"error": "You are not an active member of this team"}), 403

    g.current_user.last_active_team_id = team_id
    db.session.commit()
    data = membership.team.to_dict(include_members=True)
    data["membershipStatus"] = "active"
    return jsonify({"team": data, "lastActiveTeamId": team_id})


@auth_bp.route("/teams", methods=["POST"])
@require_auth
def create_team():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Team name required"}), 400

    # Email-verification gate: an unverified email/password account may NOT
    # create a team. Verified accounts (Google, dev-mode) pass through.
    if not g.current_user.email_verified:
        return jsonify({"error": "Please verify your email before creating a team."}), 403

    team = Team(
        name=name,
        description=data.get("description", ""),
        invite_code=_generate_invite_code(),
        created_by=g.current_user.id,
        require_approval=bool(data.get("requireApproval", False)),
    )
    db.session.add(team)
    db.session.flush()

    member = TeamMember(team_id=team.id, user_id=g.current_user.id, role="owner", status="active")
    db.session.add(member)
    db.session.add(ChatRoom(
        team_id=team.id, name="general",
        description="General discussion", room_type="general",
        created_by=g.current_user.id,
    ))
    db.session.add(ActivityLog(
        team_id=team.id, user_id=g.current_user.id,
        action="team_created", entity_type="team", entity_id=team.id,
        description=f"{g.current_user.display_name} created the team",
    ))
    g.current_user.last_active_team_id = team.id
    db.session.commit()

    # Persist team and owner membership in Firestore
    try:
        save_team_to_firestore(team)
        save_membership_to_firestore(team, g.current_user, role="owner", status="active")
    except Exception as e:
        logger.warning(f"Firestore persistence warning on create_team: {e}")

    logger.info(f"Team '{name}' created by {g.current_user.email} (require_approval={team.require_approval})")
    data = team.to_dict(include_members=True)
    data["membershipStatus"] = "active"
    return jsonify({"team": data, "lastActiveTeamId": team.id}), 201


@auth_bp.route("/teams/join", methods=["POST"])
@require_auth
def join_team():
    data = request.get_json() or {}
    code = data.get("inviteCode", "").strip().upper()
    team = Team.query.filter_by(invite_code=code).first()
    if not team:
        return jsonify({"error": "Invalid invite code"}), 404

    existing = TeamMember.query.filter_by(team_id=team.id, user_id=g.current_user.id).first()
    if existing:
        if (existing.status or "active") == "pending":
            return jsonify({"error": "Join request already sent — waiting for the server admin to approve"}), 409
        return jsonify({"error": "Already a member"}), 409

    # Sub-groups (require_approval) need the owner's OK before the member is
    # admitted; the main team joins directly.
    status = "pending" if team.require_approval else "active"
    new_member = TeamMember(team_id=team.id, user_id=g.current_user.id, role="member", status=status)
    db.session.add(new_member)
    db.session.add(ActivityLog(
        team_id=team.id, user_id=g.current_user.id,
        action="join_requested" if status == "pending" else "member_joined",
        entity_type="team", entity_id=team.id,
        description=f"{g.current_user.display_name} requested to join the team"
                     if status == "pending"
                     else f"{g.current_user.display_name} joined the team",
    ))
    if status == "active":
        g.current_user.last_active_team_id = team.id
    db.session.commit()

    # Persist membership in Firestore
    try:
        save_membership_to_firestore(team, g.current_user, role="member", status=status)
    except Exception as e:
        logger.warning(f"Firestore persistence warning on join_team: {e}")

    data = team.to_dict(include_members=True)
    data["membershipStatus"] = status
    return jsonify({"team": data, "lastActiveTeamId": g.current_user.last_active_team_id})


# ── Join requests (sub-group admin approval) ──────────────────

@auth_bp.route("/teams/<team_id>/requests", methods=["GET"])
@require_auth
def get_join_requests(team_id):
    """List pending join requests — only the team owner can manage these."""
    if not TeamMember.query.filter_by(
        team_id=team_id, user_id=g.current_user.id, role="owner"
    ).first():
        return jsonify({"error": "Only the server owner can manage join requests"}), 403
    pending = TeamMember.query.filter_by(team_id=team_id, status="pending").all()
    return jsonify({"requests": [m.to_dict() for m in pending]})


@auth_bp.route("/teams/<team_id>/requests/<membership_id>/approve", methods=["POST"])
@require_auth
def approve_join_request(team_id, membership_id):
    if not TeamMember.query.filter_by(
        team_id=team_id, user_id=g.current_user.id, role="owner"
    ).first():
        return jsonify({"error": "Only the server owner can approve join requests"}), 403
    membership = TeamMember.query.filter_by(
        id=membership_id, team_id=team_id, status="pending"
    ).first()
    if not membership:
        return jsonify({"error": "Join request not found"}), 404
    membership.status = "active"
    membership.joined_at = datetime.utcnow()
    db.session.add(ActivityLog(
        team_id=team_id, user_id=membership.user_id,
        action="member_approved", entity_type="team", entity_id=team_id,
        description=f"{membership.user.display_name if membership.user else 'Member'} was approved",
    ))
    db.session.commit()

    # Persist active membership in Firestore
    try:
        if membership.team and membership.user:
            save_membership_to_firestore(membership.team, membership.user, role=membership.role, status="active")
    except Exception as e:
        logger.warning(f"Firestore persistence warning on approve_join_request: {e}")

    logger.info(f"Approved join request {membership_id} for team {team_id}")
    return jsonify({"message": "Join request approved", "membership": membership.to_dict()})


@auth_bp.route("/teams/<team_id>/requests/<membership_id>/reject", methods=["POST"])
@require_auth
def reject_join_request(team_id, membership_id):
    if not TeamMember.query.filter_by(
        team_id=team_id, user_id=g.current_user.id, role="owner"
    ).first():
        return jsonify({"error": "Only the server owner can reject join requests"}), 403
    membership = TeamMember.query.filter_by(
        id=membership_id, team_id=team_id, status="pending"
    ).first()
    if not membership:
        return jsonify({"error": "Join request not found"}), 404

    target_user_id = membership.user_id
    target_firebase_uid = membership.user.firebase_uid if membership.user else None

    db.session.delete(membership)
    db.session.commit()

    # Remove membership from Firestore
    try:
        remove_membership_from_firestore(team_id, target_user_id, firebase_uid=target_firebase_uid)
    except Exception as e:
        logger.warning(f"Firestore remove warning on reject_join_request: {e}")

    logger.info(f"Rejected join request {membership_id} for team {team_id}")
    return jsonify({"message": "Join request rejected"})


@auth_bp.route("/teams/<team_id>", methods=["GET"])
@require_auth
def get_team(team_id):
    member = TeamMember.query.filter_by(team_id=team_id, user_id=g.current_user.id).first()
    if not member:
        return jsonify({"error": "Access denied"}), 403
    return jsonify({"team": member.team.to_dict(include_members=True)})


@auth_bp.route("/teams/<team_id>/members", methods=["GET"])
@require_auth
def get_team_members(team_id):
    if not TeamMember.query.filter_by(team_id=team_id, user_id=g.current_user.id).first():
        return jsonify({"error": "Access denied"}), 403
    members = TeamMember.query.filter_by(team_id=team_id).all()
    return jsonify({"members": [m.to_dict() for m in members]})


@auth_bp.route("/teams/<team_id>/invite-code", methods=["POST"])
@require_auth
def regenerate_invite(team_id):
    member = TeamMember.query.filter_by(
        team_id=team_id, user_id=g.current_user.id, role="owner"
    ).first()
    if not member:
        return jsonify({"error": "Only owner can regenerate invite code"}), 403
    team = Team.query.get(team_id)
    team.invite_code = _generate_invite_code()
    db.session.commit()

    # Update Firestore team with new invite code
    try:
        save_team_to_firestore(team)
    except Exception as e:
        logger.warning(f"Firestore update invite code warning: {e}")

    return jsonify({"inviteCode": team.invite_code})


# ── FIX 2: Delete team (owner only) ──────────────────────────

@auth_bp.route("/teams/<team_id>", methods=["DELETE"])
@require_auth
def delete_team(team_id):
    """Delete a team and all its data. Only the team owner can do this."""
    member = TeamMember.query.filter_by(
        team_id=team_id, user_id=g.current_user.id, role="owner"
    ).first()
    if not member:
        return jsonify({"error": "Only the team owner can delete this team"}), 403

    team = Team.query.get(team_id)
    if not team:
        return jsonify({"error": "Team not found"}), 404

    try:
        # Delete all related data in correct order (foreign key constraints)
        Notification.query.filter_by(team_id=team_id).delete()
        DailySummary.query.filter_by(team_id=team_id).delete()
        ActivityLog.query.filter_by(team_id=team_id).delete()
        Task.query.filter_by(team_id=team_id).delete()
        KnowledgeItem.query.filter_by(team_id=team_id).delete()

        rooms = ChatRoom.query.filter_by(team_id=team_id).all()
        for room in rooms:
            ChatMessage.query.filter_by(room_id=room.id).delete()
        ChatRoom.query.filter_by(team_id=team_id).delete()

        TeamMember.query.filter_by(team_id=team_id).delete()
        db.session.delete(team)
        db.session.commit()

        # Delete from Firestore cloud persistence
        try:
            delete_team_from_firestore(team_id)
        except Exception as fs_err:
            logger.warning(f"Firestore delete warning: {fs_err}")

        logger.info(f"Team '{team.name}' deleted by {g.current_user.email}")
        return jsonify({"message": "Team deleted successfully"})

    except Exception as e:
        db.session.rollback()
        logger.error(f"Team deletion failed: {e}")
        return jsonify({"error": "Failed to delete team"}), 500


# ─────────────────────────────────────────────
# SECURE WORKSPACE & CHANNEL INVITATIONS
# ─────────────────────────────────────────────

def _is_team_admin(team_id, user_id):
    if not team_id or not user_id:
        return False
    u = User.query.get(user_id)
    if u and getattr(u, "role", None) == "admin":
        return True
    m = TeamMember.query.filter_by(team_id=team_id, user_id=user_id).first()
    return m is not None and m.role in ("owner", "admin") and (m.status or "active") == "active"


@auth_bp.route("/teams/<team_id>/invitations", methods=["POST"])
@require_auth
def create_invitation(team_id):
    """
    Admins create a secure single-use invitation for an email address.
    Optionally pre-assigns the user to a specific channel.
    """
    if not _is_team_admin(team_id, g.current_user.id):
        return jsonify({"error": "Only team admins can create invitations"}), 403

    team = Team.query.get_or_404(team_id)
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "Valid email address required"}), 400

    channel_id = data.get("channelId") or data.get("channel_id")
    if channel_id:
        channel = ChatRoom.query.filter_by(id=channel_id, team_id=team_id).first()
        if not channel:
            return jsonify({"error": "Specified channel not found in this team"}), 400

    role = data.get("role", "member")
    if role not in ("member", "admin"):
        role = "member"

    expires_in_days = int(data.get("expiresInDays", 7))
    expires_at = datetime.utcnow() + timedelta(days=expires_in_days)

    # Generate cryptographically secure token and store only the SHA-256 hash
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    # Expire previous pending invitations for this email + team
    WorkspaceInvitation.query.filter_by(
        team_id=team_id, email=email, status="pending"
    ).update({"status": "revoked"})

    invitation = WorkspaceInvitation(
        team_id=team_id,
        channel_id=channel_id,
        inviter_id=g.current_user.id,
        email=email,
        role=role,
        token_hash=token_hash,
        status="pending",
        expires_at=expires_at,
    )
    db.session.add(invitation)
    db.session.commit()

    inv_dict = invitation.to_dict()
    inv_dict["token"] = raw_token
    # Build web invite URL for direct browser access
    frontend_url = os.getenv("FRONTEND_URL", "https://teamx-brain.onrender.com")
    inv_dict["inviteUrl"] = f"{frontend_url}/invite/{raw_token}"

    logger.info(f"Created invitation for {email} to team '{team.name}' by {g.current_user.email}")
    return jsonify({"invitation": inv_dict, "token": raw_token, "inviteUrl": inv_dict["inviteUrl"]}), 201


@auth_bp.route("/invitations/<token>", methods=["GET"])
def get_invitation_details(token):
    """
    Public lookup for invitation details before acceptance.
    Validates token hash, status, and expiry.
    """
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    inv = WorkspaceInvitation.query.filter_by(token_hash=token_hash).first()

    if not inv:
        return jsonify({"error": "Invalid invitation link"}), 404

    if inv.status == "revoked":
        return jsonify({"error": "This invitation has been revoked"}), 410

    if inv.status == "accepted":
        return jsonify({"error": "This invitation has already been accepted"}), 410

    if inv.expires_at < datetime.utcnow():
        inv.status = "expired"
        db.session.commit()
        return jsonify({"error": "This invitation has expired"}), 410

    return jsonify({"invitation": inv.to_dict()})


@auth_bp.route("/invitations/<token>/accept", methods=["POST"])
@require_auth
def accept_invitation(token):
    """
    Authenticated user accepts an invitation.
    Enforces email verification match between invitation and authenticated user.
    """
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    inv = WorkspaceInvitation.query.filter_by(token_hash=token_hash).first()

    if not inv:
        return jsonify({"error": "Invalid invitation link"}), 404

    if inv.status != "pending":
        return jsonify({"error": f"Invitation is no longer active (status: {inv.status})"}), 400

    if inv.expires_at < datetime.utcnow():
        inv.status = "expired"
        db.session.commit()
        return jsonify({"error": "This invitation has expired"}), 410

    # Security check: Recipient email must match authenticated user's email
    user_email = (g.current_user.email or "").strip().lower()
    inv_email = (inv.email or "").strip().lower()
    if user_email != inv_email:
        return jsonify({
            "error": f"This invitation was sent to {inv_email}. You are currently signed in as {user_email}. Please switch accounts to accept."
        }), 403

    # Add user to team
    existing_team_member = TeamMember.query.filter_by(
        team_id=inv.team_id, user_id=g.current_user.id
    ).first()

    if not existing_team_member:
        tm = TeamMember(
            team_id=inv.team_id,
            user_id=g.current_user.id,
            role=inv.role or "member",
            status="active",
        )
        db.session.add(tm)
    else:
        existing_team_member.status = "active"
        if inv.role == "admin":
            existing_team_member.role = "admin"

    # If linked to a channel, automatically add user as approved member
    if inv.channel_id:
        existing_chan_member = ChannelMember.query.filter_by(
            room_id=inv.channel_id, user_id=g.current_user.id
        ).first()

        if not existing_chan_member:
            cm = ChannelMember(
                room_id=inv.channel_id,
                user_id=g.current_user.id,
                role=inv.role or "member",
                status="approved",
                approved_at=datetime.utcnow(),
                approved_by=inv.inviter_id,
            )
            db.session.add(cm)
        else:
            existing_chan_member.status = "approved"

    # Mark invitation accepted
    inv.status = "accepted"
    inv.accepted_at = datetime.utcnow()
    inv.accepted_by = g.current_user.id

    db.session.add(ActivityLog(
        team_id=inv.team_id, user_id=g.current_user.id,
        action="member_joined", entity_type="invitation", entity_id=inv.id,
        description=f"{g.current_user.display_name} joined via invitation",
    ))

    db.session.commit()
    logger.info(f"User {g.current_user.email} accepted invitation to team {inv.team_id}")

    return jsonify({
        "success": True,
        "message": f"Successfully joined {inv.team.name if inv.team else 'team'}",
        "team": inv.team.to_dict(include_members=True) if inv.team else None,
        "channelId": inv.channel_id,
    })


@auth_bp.route("/teams/<team_id>/invitations", methods=["GET"])
@require_auth
def get_team_invitations(team_id):
    """Admins view all sent invitations for their team."""
    if not _is_team_admin(team_id, g.current_user.id):
        return jsonify({"error": "Only team admins can view invitations"}), 403

    invitations = WorkspaceInvitation.query.filter_by(team_id=team_id).order_by(
        WorkspaceInvitation.created_at.desc()
    ).all()
    return jsonify({"invitations": [i.to_dict() for i in invitations]})


@auth_bp.route("/teams/<team_id>/invitations/<invitation_id>/revoke", methods=["POST"])
@require_auth
def revoke_invitation(team_id, invitation_id):
    """Admin revokes an invitation before it is accepted."""
    if not _is_team_admin(team_id, g.current_user.id):
        return jsonify({"error": "Only team admins can revoke invitations"}), 403

    inv = WorkspaceInvitation.query.filter_by(id=invitation_id, team_id=team_id).first_or_404()
    inv.status = "revoked"
    db.session.commit()
    return jsonify({"success": True, "message": "Invitation revoked"})
