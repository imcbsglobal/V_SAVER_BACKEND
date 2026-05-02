"""
offer_app/apns_notifications.py
────────────────────────────────
Direct APNs HTTP/2 sender for iOS rich (image) notifications.
Used ONLY when a CommonNotification has an image — for iOS devices.
Android image notifications continue to use FCM (fcm_notifications.py) unchanged.

Prerequisites:
  pip install httpx[http2] PyJWT cryptography
"""

import time
import logging
import jwt
import httpx

logger = logging.getLogger(__name__)

# ── Apple credentials ─────────────────────────────────────────────────────────
APNS_KEY_ID    = "3CAH63A266"
APNS_TEAM_ID   = "BP2GB4UWSP"
APNS_BUNDLE_ID = "com.anazilfa.vcaremart"
APNS_ENDPOINT  = "https://api.push.apple.com"        # production
# APNS_ENDPOINT = "https://api.sandbox.push.apple.com"  # uncomment for dev/TestFlight

APNS_PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQgOWv4BPkX+EPYKIIl
Y4eq3piAi+Gm+gSyKnSqCGypRLqgCgYIKoZIzj0DAQehRANCAATVXVOpkpTF+Yyl
D5DiXOTR4uiUWq8nTR8szugJOYtCSOhPW7j9aaJW9t7c6HY4Z4VwOma9j0cDYp1H
aAEZiIEz
-----END PRIVATE KEY-----"""
# ─────────────────────────────────────────────────────────────────────────────

_jwt_cache = {"token": None, "expires_at": 0}


def _get_jwt() -> str:
    """Return a cached APNs JWT, refreshing if within 5 min of expiry."""
    now = int(time.time())
    if _jwt_cache["token"] and _jwt_cache["expires_at"] - now > 300:
        return _jwt_cache["token"]

    payload = {
        "iss": APNS_TEAM_ID,
        "iat": now,
    }
    token = jwt.encode(
        payload,
        APNS_PRIVATE_KEY,
        algorithm="ES256",
        headers={"kid": APNS_KEY_ID},
    )
    _jwt_cache["token"]      = token
    _jwt_cache["expires_at"] = now + 3600
    return token


def send_apns_notification(device_tokens: list, title: str, body: str, image_url: str = None) -> tuple:
    """
    Send a notification directly to APNs for iOS devices.

    Parameters
    ----------
    device_tokens : list[str]   Raw APNs device tokens (hex string, 64 chars)
    title         : str
    body          : str
    image_url     : str | None  Image URL — triggers mutable-content for NSE

    Returns
    -------
    (sent_count: int, dead_tokens: list[str])
    """
    if not device_tokens:
        return 0, []

    sent_count  = 0
    dead_tokens = []

    apns_payload = {
        "aps": {
            "alert": {
                "title": title,
                "body":  body,
            },
            "sound": "default",
            "badge": 1,
        }
    }

    if image_url:
        apns_payload["aps"]["mutable-content"] = 1
        apns_payload["body"] = {"imageUrl": image_url}  # ✅ NSE reads body.imageUrl

    try:
        with httpx.Client(http2=True, base_url=APNS_ENDPOINT, timeout=10) as client:
            jwt_token = _get_jwt()

            for token in device_tokens:
                url = f"/3/device/{token}"
                headers = {
                    "authorization":  f"bearer {jwt_token}",
                    "apns-push-type": "alert",
                    "apns-topic":     APNS_BUNDLE_ID,
                    "apns-priority":  "10",
                    "content-type":   "application/json",
                }

                try:
                    resp = client.post(url, json=apns_payload, headers=headers)

                    if resp.status_code == 200:
                        sent_count += 1

                    elif resp.status_code == 410:
                        # Token no longer valid
                        dead_tokens.append(token)
                        logger.warning("[APNs] Dead token: %.20s…", token)

                    else:
                        err = resp.json() if resp.content else {}
                        logger.error(
                            "[APNs] HTTP %s for token %.20s… — %s",
                            resp.status_code, token, err
                        )

                except Exception as exc:
                    logger.error("[APNs] Request failed for token %.20s…: %s", token, exc)

    except Exception as exc:
        logger.exception("[APNs] Client setup error: %s", exc)

    logger.info(
        "[APNs] Sent=%d  Dead=%d  Image=%s",
        sent_count, len(dead_tokens), "yes" if image_url else "no"
    )
    return sent_count, dead_tokens