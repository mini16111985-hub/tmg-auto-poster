import os
import io
import json
import time
import datetime as dt
import base64
import requests
import random
from PIL import Image

GRAPH = "https://graph.facebook.com/v24.0"
SESSION = requests.Session()


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


def openai_headers():
    api_key = os.environ["OPENAI_API_KEY"].strip()
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def openai_generate_image_base64(prompt: str, size: str = "1024x1024") -> bytes:
    url = "https://api.openai.com/v1/images/generations"
    payload = {
        "model": "gpt-image-1",
        "prompt": prompt,
        "size": size,
    }

    r = SESSION.post(url, headers=openai_headers(), json=payload, timeout=120)
    raise_for_status_with_body(r, "OpenAI images")
    data = r.json()
    b64 = data["data"][0]["b64_json"]
    return base64.b64decode(b64)


def png_to_jpeg_bytes(image_bytes: bytes, quality: int = 95) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def openai_generate_caption(prompt: str, hashtags: str = "") -> str:
    url = "https://api.openai.com/v1/chat/completions"
    user_msg = (
        "Write an Instagram caption in English (max 2-3 sentences), "
        "with a premium classic-car tone, natural and engaging, using 0-2 emojis max. "
        "Car theme: " + prompt + "\n"
        "Add these hashtags at the end: " + (hashtags or "#classiccar #vintagecar #timemachinegarage")
    )

    payload = {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "system", "content": "You are a copywriter for a classic car Instagram page."},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.8,
        "max_tokens": 220,
    }

    r = SESSION.post(url, headers=openai_headers(), json=payload, timeout=120)
    raise_for_status_with_body(r, "OpenAI caption")
    return r.json()["choices"][0]["message"]["content"].strip()


def upload_to_facebook_hosting(image_bytes: bytes, page_token: str) -> str:
    files = {
        "source": ("image.jpg", image_bytes, "image/jpeg")
    }

    r = SESSION.post(
        f"{GRAPH}/me/photos",
        files=files,
        data={
            "published": "false",
            "access_token": page_token
        },
        timeout=120,
    )

    raise_for_status_with_body(r, "FB upload (hosting)")

    photo_id = r.json()["id"]

    # dohvati URL slike
    r2 = SESSION.get(
        f"{GRAPH}/{photo_id}",
        params={
            "fields": "images",
            "access_token": page_token
        },
        timeout=60,
    )

    raise_for_status_with_body(r2, "FB get image URL")

    images = r2.json().get("images", [])

    if not images:
        raise RuntimeError("No image URL returned from Facebook")

    return images[0]["source"]


def meta_whoami(user_token: str) -> dict:
    r = SESSION.get(
        f"{GRAPH}/me",
        params={"fields": "id,name", "access_token": user_token},
        timeout=60,
    )
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
            raise RuntimeError("Nema instagram_business_account na Page-u.")

        return ig_obj["id"]

    return retry(_do, label="Meta get IG user id")


def ig_create_container(ig_user_id: str, access_token: str, image_url: str, caption: str) -> str:
    r = SESSION.post(
        f"{GRAPH}/{ig_user_id}/media",
        data={
            "image_url": image_url,
            "caption": caption,
            "access_token": access_token,
        },
        timeout=60,
    )
    raise_for_status_with_body(r, "IG create container (/media)")
    data = r.json()
    print("📦 IG container response:", data)
    return data["id"]


def ig_wait_container_ready(creation_id: str, access_token: str, max_wait_sec: int = 300) -> None:
    start = time.time()

    while True:
        r = SESSION.get(
            f"{GRAPH}/{creation_id}",
            params={"fields": "status_code", "access_token": access_token},
            timeout=60,
        )
        raise_for_status_with_body(r, "IG container status")
        status = r.json().get("status_code")

        if status == "FINISHED":
            return

        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"IG container status: {status}")

        if time.time() - start > max_wait_sec:
            raise TimeoutError("IG container not ready in time")

        time.sleep(5)


def ig_publish(ig_user_id: str, page_token: str, creation_id: str) -> str:
    url = f"{GRAPH}/{ig_user_id}/media_publish"

    r = SESSION.post(
        url,
        data={
            "creation_id": creation_id,
            "access_token": page_token,
        },
        timeout=60,
    )

    raise_for_status_with_body(r, "IG publish (/media_publish)")
    data = r.json()
    print("📤 IG publish response:", data)
    return data["id"]


def fb_publish_photo(fb_page_id: str, access_token: str, image_url: str, caption: str) -> str:
    r = SESSION.post(
        f"{GRAPH}/{fb_page_id}/photos",
        data={
            "url": image_url,
            "caption": caption,
            "published": "true",
            "access_token": access_token,
        },
        timeout=60,
    )
    raise_for_status_with_body(r, "FB publish photo")
    return r.json().get("post_id") or r.json().get("id", "")


def load_prompts():
    with open("prompts.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and "cars" in data and "years" in data and "scenes" in data:
        return data

    raise RuntimeError("prompts.json nije u očekivanom structured formatu (cars/years/scenes).")


def pick_prompt(data: dict) -> dict:
    cars = data["cars"]
    years = data["years"]
    scenes = data["scenes"]

    seed = "TMG_PROMPT_ENGINE_V1"
    rnd = random.Random(seed)

    combos = []

    hashtag_sets = [
        "#classiccar #vintagecar #timemachinegarage #classiccars #vintagecars",
        "#classiccar #classiccarsdaily #classicdriver #timemachinegarage #vintagecar",
        "#classiccar #carsofinstagram #vintagecars #timemachinegarage #carphotography",
        "#classiccar #automotivehistory #classicdrivers #timemachinegarage #vintageauto",
    ]

    for car in cars:
        for year in years:
            for scene in scenes:
                hashtags = rnd.choice(hashtag_sets)

                combos.append(
                    {
                        "prompt": f"{year} {car}, ultra realistic photo, {scene}, cinematic lighting, sharp focus, clean background, no people, no text, no watermark",
                        "hashtags": hashtags,
                    }
                )

    rnd.shuffle(combos)

    start_date = dt.date(2026, 3, 1)
    today = dt.datetime.utcnow().date()

    days = (today - start_date).days
    if days < 0:
        days = 0

    index = days % len(combos)

    return combos[index]


def main():
    user_token = os.environ["META_USER_ACCESS_TOKEN"].strip()
    fb_page_id = os.environ["FB_PAGE_ID"].strip()

    who = meta_whoami(user_token)
    print("🔎 TOKEN /me:", who)

    page_token = get_page_token(user_token, fb_page_id)
    print("✅ Got PAGE token (masked):", page_token[:20] + "..." + page_token[-10:])

    ig_user_id = get_ig_user_id_from_page(page_token, fb_page_id)
    print("✅ IG user id:", ig_user_id)

    prompts = load_prompts()
    item = pick_prompt(prompts)

    prompt = item["prompt"]
    hashtags = item["hashtags"]

    print("🧠 Prompt:", prompt)

    img_bytes = openai_generate_image_base64(prompt)
    image_url = upload_to_facebook_hosting(img_bytes, page_token)
    print("🖼️ Image URL:", image_url)

    caption = openai_generate_caption(prompt, hashtags)
    print("📝 Caption:", caption)

    creation_id = ig_create_container(ig_user_id, page_token, image_url, caption)
    ig_wait_container_ready(creation_id, page_token)
    media_id = ig_publish(ig_user_id, page_token, creation_id)
    print("✅ Published IG media id:", media_id)

    fb_post_id = fb_publish_photo(fb_page_id, page_token, image_url, caption)
    print("✅ Published FB post id:", fb_post_id)


if __name__ == "__main__":
    main()
