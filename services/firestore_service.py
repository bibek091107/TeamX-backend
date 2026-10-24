"""
AI Team Brain — Firestore Persistence Service
=============================================
Persistently stores and retrieves Teams and authenticated User-to-Team
memberships in Google Cloud Firestore (Firebase Project: teamcollab-9b857).
Maintains bidirectional consistency between Firestore and the relational
database (SQLite/PostgreSQL) so team memberships survive restarts, logouts,
and server migrations.
"""

import os
from datetime import datetime
from loguru import logger

_db_firestore = None
_firestore_attempted = False


def get_firestore_client():
    """Returns the Firestore client if Firebase Admin is available, else None."""
    global _db_firestore, _firestore_attempted
    if _db_firestore is not None:
        return _db_firestore
    if _firestore_attempted:
        return None

    _firestore_attempted = True
    try:
        import firebase_admin
        from firebase_admin import firestore

        if not firebase_admin._apps:
            from utils.auth_middleware import init_firebase
            init_firebase()

        if firebase_admin._apps:
            _db_firestore = firestore.client()
            logger.info("✅ Firestore client successfully connected to persistent cloud storage")
            return _db_firestore
        else:
            logger.warning("⚠️  Firebase Admin not initialized; Firestore persistence running in local fallback mode")
            return None
    except Exception as e:
        logger.warning(f"⚠️  Firestore client unavailable: {e}. Running in local fallback mode.")
        return None


def save_team_to_firestore(team):
    """
    Saves or updates a Team record in Firestore under 'teams/{team_id}'.
    """
    fs = get_firestore_client()
    if not fs or not team:
        return False

    try:
        team_data = {
            "id": team.id,
            "name": team.name,
            "description": team.description or "",
            "inviteCode": team.invite_code,
            "requireApproval": bool(team.require_approval),
            "createdBy": team.created_by,
            "createdAt": team.created_at.isoformat() if team.created_at else datetime.utcnow().isoformat(),
            "updatedAt": datetime.utcnow().isoformat(),
        }

        # Include creator email if available
        if hasattr(team, "creator") and team.creator:
            team_data["creatorEmail"] = (team.creator.email or "").strip().lower()

        fs.collection("teams").document(team.id).set(team_data, merge=True)
        logger.info(f"Persistent team '{team.name}' ({team.id}) synced to Firestore")
        return True
    except Exception as e:
        logger.warning(f"Failed to save team {team.id} to Firestore: {e}")
        return False


def delete_team_from_firestore(team_id):
    """
    Deletes team document and related membership records from Firestore.
    """
    fs = get_firestore_client()
    if not fs or not team_id:
        return False

    try:
        fs.collection("teams").document(team_id).delete()

        # Delete all team_members docs for this team
        members_query = fs.collection("team_members").where("teamId", "==", team_id).stream()
        for doc in members_query:
            doc.reference.delete()

        logger.info(f"Team {team_id} and memberships removed from Firestore")
        return True
    except Exception as e:
        logger.warning(f"Failed to delete team {team_id} from Firestore: {e}")
        return False


def save_membership_to_firestore(team, user, role="member", status="active"):
    """
    Saves or updates a User-to-Team membership in Firestore under 'team_members/{team_id}_{user_id}'.
    Stores verified Firebase UID and normalized email for resilient lookup.
    """
    fs = get_firestore_client()
    if not fs or not team or not user:
        return False

    try:
        norm_email = (user.email or "").strip().lower()
        doc_id = f"{team.id}_{user.id}"
        membership_data = {
            "id": doc_id,
            "teamId": team.id,
            "teamName": team.name,
            "teamInviteCode": team.invite_code,
            "userId": user.id,
            "firebaseUid": user.firebase_uid,
            "email": norm_email,
            "displayName": user.display_name or norm_email.split("@")[0],
            "role": role or "member",
            "status": status or "active",
            "joinedAt": datetime.utcnow().isoformat(),
            "updatedAt": datetime.utcnow().isoformat(),
        }

        fs.collection("team_members").document(doc_id).set(membership_data, merge=True)

        # Also register in team's member_uids array for easy security rule evaluation
        try:
            from firebase_admin import firestore
            fs.collection("teams").document(team.id).update({
                "memberUids": firestore.ArrayUnion([user.firebase_uid])
            })
        except Exception:
            pass

        logger.info(f"Persistent membership for {norm_email} in team '{team.name}' synced to Firestore")
        return True
    except Exception as e:
        logger.warning(f"Failed to save membership to Firestore: {e}")
        return False


def remove_membership_from_firestore(team_id, user_id, firebase_uid=None):
    """
    Removes a user's membership from Firestore.
    """
    fs = get_firestore_client()
    if not fs or not team_id:
        return False

    try:
        doc_id = f"{team_id}_{user_id}"
        fs.collection("team_members").document(doc_id).delete()

        if firebase_uid:
            try:
                from firebase_admin import firestore
                fs.collection("teams").document(team_id).update({
                    "memberUids": firestore.ArrayRemove([firebase_uid])
                })
            except Exception:
                pass

        logger.info(f"Membership for user {user_id} in team {team_id} removed from Firestore")
        return True
    except Exception as e:
        logger.warning(f"Failed to remove membership from Firestore: {e}")
        return False


def get_user_memberships_from_firestore(firebase_uid=None, email=None):
    """
    Queries Firestore for all memberships matching firebase_uid OR normalized email.
    Returns a list of dicts.
    """
    fs = get_firestore_client()
    if not fs:
        return []

    results = {}
    norm_email = (email or "").strip().lower()

    try:
        # Query by firebaseUid
        if firebase_uid:
            q_uid = fs.collection("team_members").where("firebaseUid", "==", firebase_uid).stream()
            for doc in q_uid:
                data = doc.to_dict()
                results[data.get("teamId")] = data

        # Query by email
        if norm_email:
            q_email = fs.collection("team_members").where("email", "==", norm_email).stream()
            for doc in q_email:
                data = doc.to_dict()
                tid = data.get("teamId")
                if tid and tid not in results:
                    results[tid] = data

        return list(results.values())
    except Exception as e:
        logger.warning(f"Error querying Firestore memberships: {e}")
        return []


def sync_user_teams(user):
    """
    Reconciles team memberships between the relational DB and Firestore for a user.
    Ensures that:
    1. If a user previously joined teams recorded in Firestore, but the local DB was
       wiped/recreated, those teams and memberships are restored into the local DB.
    2. If the user has active teams in the local DB not yet in Firestore, they are
       pushed to Firestore.
    Returns the list of active team models.
    """
    from models.database import db, Team, TeamMember, ChatRoom
    norm_email = (user.email or "").strip().lower()

    # Step 1: Check existing memberships in local DB
    local_memberships = TeamMember.query.filter_by(user_id=user.id).all()
    local_team_ids = {m.team_id: m for m in local_memberships if m.team}

    # Step 2: Push any local teams/memberships to Firestore if Firestore is accessible
    for tid, m in local_team_ids.items():
        save_team_to_firestore(m.team)
        save_membership_to_firestore(m.team, user, role=m.role, status=m.status)

    # Step 3: Fetch memberships from Firestore to restore any teams missing locally
    fs_memberships = get_user_memberships_from_firestore(firebase_uid=user.firebase_uid, email=norm_email)

    for fs_m in fs_memberships:
        team_id = fs_m.get("teamId")
        if not team_id:
            continue

        role = fs_m.get("role", "member")
        status = fs_m.get("status", "active")

        # If team doesn't exist in local DB, restore it
        existing_team = Team.query.get(team_id)
        if not existing_team:
            fs_team_doc = None
            fs = get_firestore_client()
            if fs:
                try:
                    doc = fs.collection("teams").document(team_id).get()
                    if doc.exists:
                        fs_team_doc = doc.to_dict()
                except Exception:
                    pass

            team_name = (fs_team_doc or {}).get("name") or fs_m.get("teamName") or "Team Workspace"
            team_desc = (fs_team_doc or {}).get("description") or ""
            invite_code = (fs_team_doc or {}).get("inviteCode") or fs_m.get("teamInviteCode") or "ABC12345"
            require_approval = bool((fs_team_doc or {}).get("requireApproval", False))

            existing_team = Team(
                id=team_id,
                name=team_name,
                description=team_desc,
                invite_code=invite_code,
                created_by=user.id if role == "owner" else None,
                require_approval=require_approval,
            )
            db.session.add(existing_team)
            db.session.flush()

            # Ensure default #general channel exists
            general_room = ChatRoom.query.filter_by(team_id=team_id, name="general").first()
            if not general_room:
                db.session.add(ChatRoom(
                    team_id=team_id,
                    name="general",
                    description="General discussion",
                    room_type="general",
                    created_by=user.id,
                ))

        # Ensure TeamMember exists in local DB
        membership = TeamMember.query.filter_by(team_id=team_id, user_id=user.id).first()
        if not membership:
            membership = TeamMember(
                team_id=team_id,
                user_id=user.id,
                role=role,
                status=status,
            )
            db.session.add(membership)
        else:
            membership.status = status
            membership.role = role

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning(f"Error committing synced teams for user {user.id}: {e}")
