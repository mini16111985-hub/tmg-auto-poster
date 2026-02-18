import os
import json
import time
import datetime as dt
import base64
import requests
import random

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


GRAPH = "https://graph.facebook.com/v24.0"


# -----------------------
# HTTP session + retries
# -----------------------
def make_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = make_session()


def _raise_for_status_with_body(resp: requests.Response, label: str) -> None:
    """
    Requests raise_for_status() is often too terse. This prints body for Meta/OpenAI/Imgur.
    """
    if resp.status_code < 400:
        return
    body = resp.text
    try:
        body = json.dumps(resp.json(), ensure_ascii=False)
    except Exception:
        pass
    raise RuntimeError(f"{label} HTTP {resp.status_code}: {body}")


# -----------------------
# OpenAI helpers
# -----------------------
def openai_headers():
    api_key = os.environ["OPENAI_API_KEY"].strip()
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def openai_generate_image_base64(prompt: str, size: str = "1024x1024") -> bytes:
    url = "https://api.openai.com/v1/images/generations"
    payload = {"model": "gpt-image-1", "prompt": prompt, "size": size}

    resp = SESSION.post(url, headers=openai_headers(), json=payload, timeout=180)
    _raise_for_status_with_body(resp, "OpenAI Images API")

    data = resp.json()
    b64 = data["data"][0]["b64_json"]
    return base64.b64decode(b64)


def openai_generate_caption(prompt: str, hashtags: str = "") -> str:
    url = "https://api.openai.com/v1/chat/completions"
    user_msg = (
        "Napiši Instagram opis na hrvatskom (maks 2-3 rečenice), "
        "bez previše emojija (0-2 max). "
        f"Tema auta: {prompt}\n"
        "Na kraj dodaj ove hashtagove: "
        + (hashtags or "#oldtimer #classiccar #timemachinegarage")
    )
    payload = {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "system", "content": "Ti si copywriter za Instagram stranicu o oldtimerima."},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.8,
        "max_tokens": 220,
    }

    resp = SESSION.post(url, headers=openai_headers(), json=payload, timeout=90)
    _raise_for_status_with_body(resp, "OpenAI Chat API")
    return resp.json()["choices"][0]["message"]["content"].strip()


# -----------------------
# Image hosting (Imgur)
# -----------------------
def upload_to_imgur(image_bytes: bytes) -> str:
    """
    Upload image to Imgur anonymously and return public URL.
    """
    b64_str = base64.b64encode(image_bytes).decode("utf-8")

    resp = SESSION.post(
        "https://api.imgur.com/3/image",
        headers={"Authorization": "Client-ID 546c25a59c58ad7"},
        data={"image": b64_str, "type": "base64"},
        timeout=120,
    )
    _raise_for_status_with_body(resp, "Imgur upload")

    link = resp.json()["data"]["link"]
    return link


# -----------------------
# Instagram publish helpers
# -----------------------
def ig_create_container(ig_user_id: str, access_token: str, image_url: str, caption: str) -> str:
    url = f"{GRAPH}/{ig_user_id}/media"
    resp = SESSION.post(
        url,
        data={
            "image_url": image_url,
            "caption": caption,
            "access_token": access_token,
        },
        timeout=60,
    )
    _raise_for_status_with_body(resp, "IG create container (/media)")
    return resp.json()["id"]


def ig_get_container_status(creation_id: str, access_token: str) -> dict:
    url = f"{GRAPH}/{creation_id}"
    resp = SESSION.get(
        url,
        params={"fields": "status_code,status,error_message", "access_token": access_token},
        timeout=60,
    )
    _raise_for_status_with_body(resp, "IG container status")
    return resp.json()


def ig_wait_container_ready(creation_id: str, access_token: str, max_wait_sec: int = 300) -> None:
    start = time.time()
    while True:
        info = ig_get_container_status(creation_id, access_token)
        status_code = info.get("status_code")
        status = info.get("status")
        err = info.get("error_message")

        print(f"⏳ IG container status_code={status_code} status={status} err={err}")

        if status_code == "FINISHED":
            return
        if status_code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"IG container failed: {json.dumps(info, ensure_ascii=False)}")
        if time.time() - start > max_wait_sec:
            raise TimeoutError(f"IG container not ready in {max_wait_sec}s: {json.dumps(info, ensure_ascii=False)}")
        time.sleep(5)


def ig_publish(ig_user_id: str, access_token: str, creation_id: str) -> str:
    url = f"{GRAPH}/{ig_user_id}/media_publish"
    resp = SESSION.post(
        url,
        data={"creation_id": creation_id, "access_token": access_token},
        timeout=60,
    )
    _raise_for_status_with_body(resp, "IG publish (/media_publish)")
    return resp.json()["id"]


# -----------------------
# Facebook Page publish helpers
# -----------------------
def fb_publish_photo(page_id: str, page_access_token: str, image_url: str, caption: str) -> str:
    url = f"{GRAPH}/{page_id}/photos"
    resp = SESSION.post(
        url,
        data={
            "url": image_url,
            "caption": caption,
            "published": "true",
            "access_token": page_access_token,
        },
        timeout=60,
    )
    _raise_for_status_with_body(resp, "FB publish photo (/photos)")
    return resp.json().get("post_id") or resp.json().get("id", "")


# -----------------------
# Content selection
# -----------------------
def pick_prompt(items: list[dict]) -> dict:
    seed = "TMG_SHUFFLE_V1"
    rnd = random.Random(seed)
    order = list(range(len(items)))
    rnd.shuffle(order)

    day_index = dt.datetime.utcnow().date().toordinal()
    pick = order[day_index % len(order)]
    return items[pick]


def main():
    ig_user_id = os.environ["IG_USER_ID"].strip()
    access_token = os.environ["IG_ACCESS_TOKEN"].strip()
    fb_page_id = os.environ["FB_PAGE_ID"].strip()
    fb_page_token = os.environ["FB_PAGE_ACCESS_TOKEN"].strip()

    with open("prompts.json", "r", encoding="utf-8") as f:
        items = json.load(f)
    if not items:
        raise RuntimeError("prompts.json je prazan")

    item = pick_prompt(items)
    prompt = item["prompt"]
    hashtags = item.get("hashtags", "")

    print("🧠 Prompt:", prompt)

    # 1) Generate image
    img_bytes = openai_generate_image_base64(prompt)

    # 2) Host image publicly
    image_url = upload_to_imgur(img_bytes)
    print("🖼️ Image URL:", image_url)

    # 3) Caption
    caption = openai_generate_caption(prompt, hashtags)
    print("📝 Caption:", caption)

    # 4) IG
    creation_id = ig_create_container(ig_user_id, access_token, image_url, caption)
    print("📦 IG creation_id:", creation_id)

    ig_wait_container_ready(creation_id, access_token)

    media_id = ig_publish(ig_user_id, access_token, creation_id)
    print("✅ Published IG media id:", media_id)

    # 5) FB
    fb_post_id = fb_publish_photo(fb_page_id, fb_page_token, image_url, caption)
    print("✅ Published FB post id:", fb_post_id)


if __name__ == "__main__":
    main()
