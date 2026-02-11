import os
import json
import time
import base64
import hashlib
import datetime as dt
import requests
from openai import OpenAI

GRAPH = "https://graph.facebook.com/v24.0"


def pick_prompt(prompts: list[dict]) -> dict:
    # "Random ali deterministički" po danu (da ne ponavlja isti dan)
    day = dt.datetime.utcnow().strftime("%Y-%m-%d")
    h = hashlib.sha256(day.encode("utf-8")).hexdigest()
    idx = int(h[:8], 16) % len(prompts)
    return prompts[idx]


def catbox_upload(filepath: str) -> str:
    # Public URL potreban za IG image_url
    url = "https://catbox.moe/user/api.php"
    with open(filepath, "rb") as f:
        r = requests.post(
            url,
            data={"reqtype": "fileupload"},
            files={"fileToUpload": f},
            timeout=120,
        )
    r.raise_for_status()
    return r.text.strip()


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


def generate_caption(client: OpenAI, prompt: str, hashtags: str) -> str:
    # Kratak opis na HR + par emojija + hashtagovi
    input_text = (
        "Napiši Instagram caption na hrvatskom za objavu AI generirane fotke oldtimera.\n"
        "Stil: kratak, punchy, auto-entuzijast, 1-2 rečenice + 1 red hashtagova.\n"
        "Ne spominji da je slika AI.\n\n"
        f"Auto prompt: {prompt}\n"
        f"Hashtagovi: {hashtags}\n"
    )
    resp = client.responses.create(
        model="gpt-4.1-mini",
        input=input_text
    )
    text = resp.output_text.strip()
    # osiguraj da hashtagovi postoje na kraju
    if hashtags and hashtags not in text:
        text = text.rstrip() + "\n\n" + hashtags
    return text


def generate_image(client: OpenAI, prompt: str, out_path: str) -> None:
    img = client.images.generate(
        model="gpt-image-1",
        prompt=prompt,
        size="1024x1024",
    )
    b64 = img.data[0].b64_json
    data = base64.b64decode(b64)
    with open(out_path, "wb") as f:
        f.write(data)


def main():
    ig_user_id = os.environ["IG_USER_ID"].strip()
    ig_access_token = os.environ["IG_ACCESS_TOKEN"].strip()
    openai_key = os.environ["OPENAI_API_KEY"].strip()

    with open("prompts.json", "r", encoding="utf-8") as f:
        prompts = json.load(f)
    if not prompts:
        raise RuntimeError("prompts.json je prazan")

    chosen = pick_prompt(prompts)
    prompt = chosen["prompt"]
    hashtags = chosen.get("hashtags", "")

    client = OpenAI(api_key=openai_key)

    # 1) generate image
    out_path = "generated.png"
    print("Generating image...")
    generate_image(client, prompt, out_path)

    # 2) upload to get public URL
    print("Uploading image...")
    public_url = catbox_upload(out_path)
    print("Public URL:", public_url)

    # 3) generate caption
    print("Generating caption...")
    caption = generate_caption(client, prompt, hashtags)
    print("Caption:", caption)

    # 4) IG publish
    print("Creating IG container...")
    creation_id = ig_create_container(ig_user_id, ig_access_token, public_url, caption)
    ig_wait_container_ready(creation_id, ig_access_token)
    media_id = ig_publish(ig_user_id, ig_access_token, creation_id)
    print("✅ Published IG media id:", media_id)


if __name__ == "__main__":
    main()
