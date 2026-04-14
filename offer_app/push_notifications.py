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

    # Split tokens into batches of 100
    for i in range(0, len(tokens), BATCH_SIZE):
        batch = tokens[i:i + BATCH_SIZE]

        messages = [
            {
                "to": token,
                "title": title,
                "body": body,
                "data": data or {},
                "sound": "default",
            }
            for token in batch
        ]

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