import requests

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

def send_expo_push_notification(tokens: list, title: str, body: str, data: dict = None):
    if not tokens:
        return []

    messages = [
        {
            "to": token,
            "title": title,
            "body": body,
            "data": data or {},
            "sound": "default",
        }
        for token in tokens
    ]

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    }

    response = requests.post(EXPO_PUSH_URL, json=messages, headers=headers)
    return response.json()