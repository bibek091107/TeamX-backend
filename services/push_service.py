"""
AI Team Brain — Push Notification Service (FCM & Fallback)
Dispatches Firebase Cloud Messaging (FCM) notifications to registered user devices.
"""

from loguru import logger
from models.database import db, DeviceToken


def send_push_notification(user_id: str, title: str, body: str, extra_data: dict = None):
    """
    Send push notification to all active device tokens for user_id.
    Gracefully handles absence of Firebase credentials.
    """
    if not user_id:
        return

    tokens = DeviceToken.query.filter_by(user_id=user_id).all()
    if not tokens:
        logger.debug(f"No FCM device tokens registered for user {user_id}")
        return

    try:
        import firebase_admin
        from firebase_admin import messaging

        if not firebase_admin._apps:
            logger.debug("Firebase Admin not initialized, skipping FCM push")
            return

        token_strings = [t.token for t in tokens if t.token]
        if not token_strings:
            return

        payload_data = {}
        if extra_data:
            for k, v in extra_data.items():
                payload_data[str(k)] = str(v) if v is not None else ""

        message = messaging.MulticastMessage(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=payload_data,
            tokens=token_strings,
        )

        response = messaging.send_multicast(message)
        logger.info(f"FCM sent to {len(token_strings)} devices for user {user_id}: {response.success_count} success, {response.failure_count} failure")

        # Cleanup invalid tokens if any failed
        if response.failure_count > 0:
            for idx, resp in enumerate(response.responses):
                if not resp.success and resp.exception:
                    exc_str = str(resp.exception).lower()
                    if "not registered" in exc_str or "invalid" in exc_str:
                        stale_token = token_strings[idx]
                        DeviceToken.query.filter_by(token=stale_token).delete()
                        db.session.commit()
                        logger.info(f"Removed stale device token: {stale_token[:15]}...")

    except Exception as e:
        logger.warning(f"Failed to dispatch FCM push notification for user {user_id}: {e}")
