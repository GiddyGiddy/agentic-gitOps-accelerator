import httpx


def notify_gateway(url: str, secret: str, message: dict) -> None:
    if not url.startswith("https://") or not secret:
        raise ValueError("HTTPS Teams gateway URL and shared secret are required")
    response = httpx.post(
        url, json=message, headers={"X-Gateway-Secret": secret}, timeout=15, follow_redirects=False
    )
    response.raise_for_status()
