import requests

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
BATCH_SIZE = 100


def send_expo_push_notification(tokens: list, title: str, body: str, data: dict = None):
    if not tokens:
        return [], []

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    }

    all_responses = []
    dead_tokens = []

    # Extract imageUrl from data dict if present (passed by views.py as extra_data)
    payload_data = dict(data) if data else {}
    image_url = payload_data.pop("imageUrl", None)

    # Split tokens into batches of 100
    for i in range(0, len(tokens), BATCH_SIZE):
        batch = tokens[i:i + BATCH_SIZE]

        messages = []
        for token in batch:
            message = {
                "to": token,
                "title": title,
                "body": body,
                "data": payload_data,
                "sound": "default",
            }

            # ✅ Correct structure so image shows in notification drawer on Android & iOS
            if image_url:
                message["android"] = {
                    "imageUrl": image_url,
                }
                message["apns"] = {
                    "payload": {
                        "aps": {
                            "mutable-content": 1,
                        }
                    },
                    "fcm_options": {
                        "image": image_url,
                    },
                }

            messages.append(message)

        try:
            response = requests.post(EXPO_PUSH_URL, json=messages, headers=headers, timeout=10)
            result = response.json()
            all_responses.append(result)

            # Detect dead/invalid tokens
            for idx, ticket in enumerate(result.get("data", [])):
                if ticket.get("status") == "error":
                    if ticket.get("details", {}).get("error") == "DeviceNotRegistered":
                        dead_tokens.append(batch[idx])

        except Exception as e:
            print(f"[PushNotification] Batch {i // BATCH_SIZE + 1} failed: {e}")

    return all_responses, dead_tokens