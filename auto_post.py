import os
import json
import time
import datetime as dt
import base64
import requests
import random

GRAPH = "https://graph.facebook.com/v24.0"

# -----------------------------
# HTTP session + helpers
# -----------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "tmg-auto-poster/1.0"})
    return s

SESSION = make_session()

def _raise_for_status_with_body(resp: requests.Response, label: str) -> None:
    if resp.status_code < 400:
        return
    body = resp.text
    try:
        body = json.dumps(resp.json(), ensure_ascii=False)
    except Exception:
        pass
    raise RuntimeError(f"{label} HTTP {resp.status_code}: {body}")

def retry(fn, tries=5, base_sleep=2.0, label="call"):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            # exponential backoff
            time.sleep(base_sleep * (2 ** i))
    raise RuntimeError(f"{label} failed after {tries} tries: {last}")

# -----------------------------
# OpenAI helpers
# -----------------------------
def openai_headers():
    api_key = os.environ["OPENAI_API_KEY"].strip()
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

def openai_generate_image_base64(prompt: str, size: str = "1024x1024") -> bytes:
    url = "https://api.openai.com/v1/images/generations"
    payload = {"model": "gpt-image-1", "prompt": prompt, "size": size}

    def _do():
        r = SESSION.post(url, headers=openai_headers(), json=payload, timeout=180)
        if r.status_code >= 400:
            raise RuntimeError(f"OpenAI Images API error {r.status_code}: {r.text}")
        data = r.json()
        b64 = data["data"][0]["b64_json"]
        return base64.b64decode(b64)

    return retry(_do, tries=5, base_sleep=2, label="OpenAI image")

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

    def _do():
        r = SESSION.post(url, headers=openai_headers(), json=payload, timeout=90)
        _raise_for_status_with_body(r, "OpenAI caption")
        return r.json()["choices"][0]["message"]["content"].strip()

    return retry(_do, tries=4, base_sleep=2, label="OpenAI caption")

# -----------------------------
# Image hosting (Imgur)
# -----------------------------
def upload_to_imgur(image_bytes: bytes) -> str:
    def _do():
        r = SESSION.post(
            "https://api.imgur.com/3/image",
            headers={"Authorization": "Client-ID 546c25a59c58ad7"},
            data={"image": base64.b64encode(image_bytes)},
            timeout=120,
        )
        _raise_for_status_with_body(r, "Imgur upload")
        return r.json()["data"]["link"]

    return retry(_do, tries=5, base_sleep=2, label="Imgur upload")

# -----------------------------
# Meta token/ID discovery
#   - derive Page access_token + IG business ID from long-lived USER token
# -----------------------------
def get_page_token_and_ig_id(user_token: str, fb_page_id: str) -> tuple[str, str]:
    """
    1) /me/accounts (USER token) -> nađi baš taj PAGE i uzmi page access_token
    2) /{page-id}?fields=instagram_business_account (PAGE token) -> dobij IG business id
    """
    # 1) Get page token from /me/accounts (THIS is where "accounts" exists)
    url = f"{GRAPH}/me/accounts"
    r = SESSION.get(url, params={
        "fields": "id,name,access_token",
        "access_token": user_token
    }, timeout=60)
    _raise_for_status_with_body(r, "Meta /me/accounts")

    data = r.json().get("data", [])
    page = next((p for p in data if str(p.get("id")) == str(fb_page_id)), None)
    if not page:
        ids = [p.get("id") for p in data]
        raise RuntimeError(f"Ne nalazim FB Page ID {fb_page_id} u /me/accounts. Dostupni page IDs: {ids}")

    page_token = page["access_token"]

    # 2) Get instagram_business_account from the Page node (THIS is valid on Page)
    url2 = f"{GRAPH}/{fb_page_id}"
    r2 = SESSION.get(url2, params={
        "fields": "instagram_business_account",
        "access_token": page_token
    }, timeout=60)
    _raise_for_status_with_body(r2, "Meta Page instagram_business_account")

    ig_obj = r2.json().get("instagram_business_account")
    if not ig_obj or not ig_obj.get("id"):
        raise RuntimeError(f"Page {fb_page_id} nema instagram_business_account. Provjeri da je IG Business povezan na tu stranicu.")

    ig_user_id = ig_obj["id"]
    return page_token, ig_user_id

return retry(_do, tries=4, base_sleep=2, label="Meta page+ig discovery")

# -----------------------------
# IG publish helpers
# -----------------------------
def ig_create_container(ig_user_id: str, access_token: str, image_url: str, caption: str) -> str:
    url = f"{GRAPH}/{ig_user_id}/media"

    def _do():
        r = SESSION.post(
            url,
            data={
                "image_url": image_url,
                "caption": caption,
                "access_token": access_token,
            },
            timeout=60,
        )
        _raise_for_status_with_body(r, "IG create container (/media)")
        return r.json()["id"]

    return retry(_do, tries=4, base_sleep=3, label="IG create container")

def ig_wait_container_ready(creation_id: str, access_token: str, max_wait_sec: int = 240) -> None:
    url = f"{GRAPH}/{creation_id}"
    start = time.time()

    while True:
        r = SESSION.get(url, params={"fields": "status_code,status,error_message", "access_token": access_token}, timeout=60)
        _raise_for_status_with_body(r, "IG container status")
        j = r.json()
        status = j.get("status_code")

        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"IG container status: {status} / {j.get('error_message')}")

        if time.time() - start > max_wait_sec:
            raise TimeoutError("IG container not ready in time")

        time.sleep(5)

def ig_publish(ig_user_id: str, access_token: str, creation_id: str) -> str:
    url = f"{GRAPH}/{ig_user_id}/media_publish"

    def _do():
        r = SESSION.post(
            url,
            data={"creation_id": creation_id, "access_token": access_token},
            timeout=60,
        )
        _raise_for_status_with_body(r, "IG publish (/media_publish)")
        return r.json()["id"]

    return retry(_do, tries=4, base_sleep=3, label="IG publish")

# -----------------------------
# FB Page publish helpers
# -----------------------------
def fb_publish_photo(page_id: str, page_access_token: str, image_url: str, caption: str) -> str:
    url = f"{GRAPH}/{page_id}/photos"

    def _do():
        r = SESSION.post(
            url,
            data={
                "url": image_url,
                "caption": caption,
                "published": "true",
                "access_token": page_access_token,
            },
            timeout=60,
        )
        _raise_for_status_with_body(r, "FB publish photo (/photos)")
        return r.json().get("post_id") or r.json().get("id", "")

    return retry(_do, tries=4, base_sleep=3, label="FB publish photo")

# -----------------------------
# Content selection
# -----------------------------
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

    with open("prompts.json", "r", encoding="utf-8") as f:
        items = json.load(f)
    if not items:
        raise RuntimeError("prompts.json je prazan")

    # Discover Page token + IG business ID from the USER token
    page_token, ig_user_id = get_page_token_and_ig_id(user_token, fb_page_id)
    print("✅ Resolved IG_USER_ID:", ig_user_id)
    print("✅ Resolved FB_PAGE_ID:", fb_page_id)

    item = pick_prompt(items)
    prompt = item["prompt"]
    hashtags = item.get("hashtags", "")

    print("🧠 Prompt:", prompt)

    # 1) Generate image
    img_bytes = openai_generate_image_base64(prompt)

    # 2) Host image
    image_url = upload_to_imgur(img_bytes)
    print("🖼️ Image URL:", image_url)

    # 3) Caption
    caption = openai_generate_caption(prompt, hashtags)
    print("📝 Caption:", caption)

    # 4) Publish to IG
    creation_id = ig_create_container(ig_user_id, user_token, image_url, caption)
    ig_wait_container_ready(creation_id, user_token)
    media_id = ig_publish(ig_user_id, user_token, creation_id)
    print("✅ Published IG media id:", media_id)

    # 5) Publish to FB Page
    fb_post_id = fb_publish_photo(fb_page_id, page_token, image_url, caption)
    print("✅ Published FB post id:", fb_post_id)

if __name__ == "__main__":
    main()
