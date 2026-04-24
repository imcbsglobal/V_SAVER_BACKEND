"""
offer_app/fcm_notifications.py
──────────────────────────────────────────────────────────────────────────────
Firebase Cloud Messaging (FCM V1) helper.

Used ONLY for CommonNotification sends:
  • With image  → FCM with full image payload (Android + APNs)
  • Without image → FCM plain notification (still uses FCM so we go through
                    one consistent code-path; Expo tokens are NOT touched here)

OfferMaster push, send_push_notification view, and scheduler jobs for offers
continue to use Expo push (push_notifications.py) — completely unchanged.
"""

import logging
import os

logger = logging.getLogger(__name__)
_firebase_initialised = False


def _init_firebase():
    """Initialise the Firebase Admin SDK once per process."""
    global _firebase_initialised
    if _firebase_initialised:
        return True
    try:
        import firebase_admin
        from firebase_admin import credentials

        if firebase_admin._apps:
            _firebase_initialised = True
            return True

        from django.conf import settings

        json_path = getattr(
            settings,
            'FIREBASE_SERVICE_ACCOUNT_JSON',
            os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON', ''),
        )
        if not json_path or not os.path.exists(json_path):
            logger.error(
                "[FCM] Service-account JSON not found. "
                "Set FIREBASE_SERVICE_ACCOUNT_JSON in settings.py or as an env var."
            )
            return False

        cred = credentials.Certificate(json_path)
        firebase_admin.initialize_app(cred)
        _firebase_initialised = True
        logger.info("[FCM] Firebase Admin SDK initialised.")
        return True

    except ImportError:
        logger.error("[FCM] firebase-admin not installed. Run: pip install firebase-admin")
        return False
    except Exception as exc:
        logger.exception("[FCM] Initialisation failed: %s", exc)
        return False


def send_fcm_notification_with_image(fcm_tokens, title, body, image_url=None):
    """
    Send a notification to a list of FCM device tokens.

    Parameters
    ----------
    fcm_tokens : list[str]   FCM registration tokens (from ExpoPushToken.fcm_token)
    title      : str         Notification title
    body       : str         Notification body
    image_url  : str | None  Optional full URL of the image to show in the notification

    Returns
    -------
    (sent_count: int, dead_tokens: list[str])
        dead_tokens — tokens that are no longer valid and should be removed from the DB.
    """
    if not fcm_tokens:
        return 0, []

    if not _init_firebase():
        return 0, []

    try:
        from firebase_admin import messaging
    except ImportError:
        logger.error("[FCM] firebase-admin not installed.")
        return 0, []

    sent_count  = 0
    dead_tokens = []

    for token in fcm_tokens:
        try:
            if image_url:
                # ── With image ──────────────────────────────────────────────
                notification = messaging.Notification(
                    title=title,
                    body=body,
                    image=image_url,
                )
                android_config = messaging.AndroidConfig(
                    notification=messaging.AndroidNotification(
                        image=image_url,
                        channel_id='default',
                    ),
                )
                apns_config = messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(mutable_content=True),
                    ),
                    fcm_options=messaging.APNSFCMOptions(image=image_url),
                )
            else:
                # ── Without image ───────────────────────────────────────────
                notification = messaging.Notification(
                    title=title,
                    body=body,
                )
                android_config = messaging.AndroidConfig(
                    notification=messaging.AndroidNotification(channel_id='default'),
                )
                apns_config = messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(mutable_content=True),
                    ),
                )

            message = messaging.Message(
                notification=notification,
                android=android_config,
                apns=apns_config,
                token=token,
            )
            messaging.send(message)
            sent_count += 1

        except Exception as exc:
            err_str = str(exc)
            if (
                'registration-token-not-registered' in err_str
                or 'invalid-registration-token' in err_str
            ):
                dead_tokens.append(token)
                logger.warning("[FCM] Dead token queued for removal: %.30s…", token)
            else:
                logger.error("[FCM] Send failed for token %.30s…: %s", token, exc)

    logger.info(
        "[FCM] Sent=%d  Dead=%d  Image=%s",
        sent_count,
        len(dead_tokens),
        "yes" if image_url else "no",
    )
    return sent_count, dead_tokens