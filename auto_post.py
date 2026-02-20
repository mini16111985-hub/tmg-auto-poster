import os
import json
import time
import datetime as dt
import base64
import requests
import random

GRAPH = "https://graph.facebook.com/v24.0"
SESSION = requests.Session()

# ----------------------------
# Helpers
# ----------------------------

def raise_for_status_with_body(resp: requests.Response, label: str) -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    raise RuntimeError(f"{label} HTTP {resp.status_code}: {body}")

def retry(fn, tries=4, base_sleep=2, label="request"):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            if i == tries - 1:
                raise RuntimeError(f"{label} failed after {tries} tries: {last}") from last
            time.sleep(base_sleep * (2 ** i))

# ----------------------------
# OpenAI (image + caption)
# ----------------------------

def openai_headers():
    api_key = os.environ["OPENAI_API_KEY"].strip()
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

def openai_generate_image_base64(prompt: str, size: str = "1024x1024") -> bytes:
    url = "https://api.openai.com/v1/images/generations"
    payload = {"model": "gpt-image-1", "prompt": prompt, "size": size}

    r = SESSION.post(url, headers=openai_headers(), json=payload, timeout=120)
    raise_for_status_with_body(r, "OpenAI images")
    data = r.json()
    b64 = data["data"][0]["b64_json"]
    return base64.b64decode(b64)

def openai_generate_caption(prompt: str, hashtags: str = "") -> str:
    url = "https://api.openai.com/v1/chat/completions"
    user_msg = (
        "Napiši Instagram opis na hrvatskom (maks 2-3 rečenice), "
        "bez previše emojija (0-2 max). "
        "Tema auta: " + prompt + "\n"
        "Na kraj dodaj ove hashtagove: " + (hashtags or "#oldtimer #classiccar #timemachinegarage")
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
    r = SESSION.post(url, headers=openai_headers(), json=payload, timeout=120)
    raise_for_status_with_body(r, "OpenAI caption")
    return r.json()["choices"][0]["message"]["content"].strip()

# ----------------------------
# Image hosting (Imgur)
# ----------------------------

def upload_to_imgur(image_bytes: bytes) -> str:
    r = SESSION.post(
        "https://api.imgur.com/3/image",
        headers={"Authorization": "Client-ID 546c25a59c58ad7"},
        data={"image": base64.b64encode(image_bytes)},
        timeout=120,
    )
    raise_for_status_with_body(r, "Imgur upload")
    return r.json()["data"]["link"]

# ----------------------------
# Meta discovery: user token -> page token -> ig id
# ----------------------------

def meta_whoami(user_token: str) -> dict:
    r = SESSION.get(f"{GRAPH}/me", params={"fields": "id,name", "access_token": user_token}, timeout=60)
    raise_for_status_with_body(r, "Meta /me")
    return r.json()

def get_page_token(user_token: str, fb_page_id: str) -> str:
    def _do():
        r = SESSION.get(
            f"{GRAPH}/me/accounts",
            params={"fields": "id,name,access_token", "access_token": user_token},
            timeout=60,
        )
        raise_for_status_with_body(r, "Meta /me/accounts")
        data = r.json().get("data", [])
        for p in data:
            if str(p.get("id")) == str(fb_page_id):
                return p["access_token"]
        ids = [p.get("id") for p in data]
        raise RuntimeError(f"FB_PAGE_ID {fb_page_id} nije pronađen u /me/accounts. Dostupni: {ids}")
    return retry(_do, label="Meta get page token")

def get_ig_user_id_from_page(page_token: str, fb_page_id: str) -> str:
    def _do():
        r = SESSION.get(
            f"{GRAPH}/{fb_page_id}",
            params={"fields": "instagram_business_account", "access_token": page_token},
            timeout=60,
        )
        raise_for_status_with_body(r, "Meta Page instagram_business_account")
        ig_obj = r.json().get("instagram_business_account")
        if not ig_obj or not ig_obj.get("id"):
            raise RuntimeError("Nema instagram_business_account na Page-u (provjeri povezivanje IG Business ↔ FB Page).")
        return ig_obj["id"]
    return retry(_do, label="Meta get IG user id")

# ----------------------------
# IG publish
# ----------------------------

def ig_create_container(ig_user_id: str, user_token: str, image_url: str, caption: str) -> str:
    r = SESSION.post(
        f"{GRAPH}/{ig_user_id}/media",
        data={"image_url": image_url, "caption": caption, "access_token": user_token},
        timeout=60,
    )
    raise_for_status_with_body(r, "IG create container (/media)")
    return r.json()["id"]

def ig_wait_container_ready(creation_id: str, page_token: str, max_wait_sec: int = 300) -> None:
    start = time.time()
    while True:
        r = SESSION.get(
            f"{GRAPH}/{creation_id}",
            params={"fields": "status_code", "access_token": access_token},
            timeout=60,
        )
        raise_for_status_with_body(r, "IG container status")
        j = r.json()
        status = j.get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"IG container status: {status} / {j}")
        if time.time() - start > max_wait_sec:
            raise TimeoutError(f"IG container not ready in {max_wait_sec}s. Last: {j}")
        time.sleep(5)

def ig_publish(ig_user_id: str, page_token: str, creation_id: str) -> str:
    r = SESSION.post(
        f"{GRAPH}/{ig_user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": user_token},
        timeout=60,
    )
    raise_for_status_with_body(r, "IG publish (/media_publish)")
    return r.json()["id"]

# ----------------------------
# FB publish photo
# ----------------------------

def fb_publish_photo(fb_page_id: str, page_token: str, image_url: str, caption: str) -> str:
    r = SESSION.post(
        f"{GRAPH}/{fb_page_id}/photos",
        data={"url": image_url, "caption": caption, "published": "true", "access_token": page_token},
        timeout=60,
    )
    raise_for_status_with_body(r, "FB publish photo")
    return r.json().get("post_id") or r.json().get("id", "")

# ----------------------------
# Content selection
# ----------------------------

def pick_prompt(items: list[dict]) -> dict:
    seed = "TMG_SHUFFLE_V1"
    rnd = random.Random(seed)
    order = list(range(len(items)))
    rnd.shuffle(order)
    day_index = dt.datetime.utcnow().date().toordinal()
    pick = order[day_index % len(order)]
    return items[pick]

def main():
    user_token = os.environ["META_USER_ACCESS_TOKEN"].strip()
    fb_page_id = os.environ["FB_PAGE_ID"].strip()

    who = meta_whoami(user_token)
    print("🔎 TOKEN /me:", who)

    page_token = get_page_token(user_token, fb_page_id)
    print("✅ Got PAGE token (masked):", page_token[:20] + "..." + page_token[-10:])

    ig_user_id = get_ig_user_id_from_page(page_token, fb_page_id)
    print("✅ IG user id:", ig_user_id)

    with open("prompts.json", "r", encoding="utf-8") as f:
        items = json.load(f)
    if not items:
        raise RuntimeError("prompts.json je prazan")

    item = pick_prompt(items)
    prompt = item["prompt"]
    hashtags = item.get("hashtags", "")

    print("🧠 Prompt:", prompt)

    img_bytes = openai_generate_image_base64(prompt)
    image_url = upload_to_imgur(img_bytes)
    print("🖼️ Image URL:", image_url)

    caption = openai_generate_caption(prompt, hashtags)
    print("📝 Caption:", caption)

    creation_id = ig_create_container(ig_user_id, user_token, image_url, caption)
    ig_wait_container_ready(creation_id, page_token)
    media_id = ig_publish(ig_user_id, page_token, creation_id)
    print("✅ Published IG media id:", media_id)

    fb_post_id = fb_publish_photo(fb_page_id, page_token, image_url, caption)
    print("✅ Published FB post id:", fb_post_id)

if __name__ == "__main__":
    main()
