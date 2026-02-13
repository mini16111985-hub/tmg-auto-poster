import os
import json
import time
import datetime as dt
import base64
import requests

GRAPH = "https://graph.facebook.com/v24.0"

# --- OpenAI helpers (Images + Text) ---

def openai_headers():
    api_key = os.environ["OPENAI_API_KEY"].strip()
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

def openai_generate_image_base64(prompt: str, size: str = "1024x1024") -> bytes:
    # GPT image models use /v1/images/generations and return b64_json by default
    url = "https://api.openai.com/v1/images/generations"
    payload = {
        "model": "gpt-image-1",
        "prompt": prompt,
        "size": size,
    }

    r = requests.post(url, headers=openai_headers(), json=payload, timeout=180)
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI Images API error {r.status_code}: {r.text}")

    data = r.json()
    b64 = data["data"][0]["b64_json"]
    return base64.b64decode(b64)

def openai_generate_caption(prompt: str, hashtags: str = "") -> str:
    # Short IG-ready caption in Croatian
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
    r = requests.post(url, headers=openai_headers(), json=payload, timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

# --- Image hosting for IG (needs public URL) ---

def upload_to_imgur(image_bytes: bytes) -> str:
    """
    Uploads image to Imgur anonymously and returns a public direct image URL.
    NOTE: This relies on Imgur's anonymous upload endpoint which may rate-limit.
    If you hit limits, we’ll switch to GitHub Releases or another host.
    """
    r = requests.post(
        "https://api.imgur.com/3/image",
        headers={"Authorization": "Client-ID 546c25a59c58ad7"},  # public demo client-id
        data={"image": base64.b64encode(image_bytes)},
        timeout=120,
    )
    r.raise_for_status()
    link = r.json()["data"]["link"]
    return link

# --- IG publish helpers ---

def ig_create_container(ig_user_id: str, access_token: str, image_url: str, caption: str) -> str:
    url = f"{GRAPH}/{ig_user_id}/media"
    r = requests.post(url, data={
        "image_url": image_url,
        "caption": caption,
        "access_token": access_token,
    }, timeout=60)
    r.raise_for_status()
    return r.json()["id"]

def ig_publish(ig_user_id: str, access_token: str, creation_id: str) -> str:
    url = f"{GRAPH}/{ig_user_id}/media_publish"
    r = requests.post(url, data={
        "creation_id": creation_id,
        "access_token": access_token,
    }, timeout=60)
    r.raise_for_status()
    return r.json()["id"]

def ig_wait_container_ready(creation_id: str, access_token: str, max_wait_sec: int = 180) -> None:
    url = f"{GRAPH}/{creation_id}"
    start = time.time()
    while True:
        r = requests.get(url, params={"fields": "status_code", "access_token": access_token}, timeout=60)
        r.raise_for_status()
        status = r.json().get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"IG container status: {status}")
        if time.time() - start > max_wait_sec:
            raise TimeoutError("IG container not ready in time")
        time.sleep(5)

# --- Content selection (random daily) ---

def pick_prompt(items: list[dict]) -> dict:
    # Shuffle kroz sve pa ponovi:
    # napravimo fiksno izmiješan redoslijed i svaki dan uzmemo sljedeći element

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

    # 2) Host image publicly (IG requires public URL)
    image_url = upload_to_imgur(img_bytes)
    print("🖼️ Image URL:", image_url)

    # 3) Generate caption
    caption = openai_generate_caption(prompt, hashtags)
    print("📝 Caption:", caption)

    # 4) Publish to IG
    creation_id = ig_create_container(ig_user_id, access_token, image_url, caption)
    ig_wait_container_ready(creation_id, access_token)
    media_id = ig_publish(ig_user_id, access_token, creation_id)

    print("✅ Published IG media id:", media_id)

if __name__ == "__main__":
    main()
