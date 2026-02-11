import os
import json
import time
import datetime as dt
import requests

GRAPH = "https://graph.facebook.com/v24.0"

def pick_post(posts: list[dict]) -> dict:
    day_of_year = int(dt.datetime.utcnow().strftime("%j"))
    return posts[day_of_year % len(posts)]

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

def main():
    ig_user_id = os.environ["IG_USER_ID"].strip()
    access_token = os.environ["IG_ACCESS_TOKEN"].strip()

    with open("posts.json", "r", encoding="utf-8") as f:
        posts = json.load(f)

    post = pick_post(posts)
    image_url = post["image_url"]
    caption = post.get("caption", "")

    creation_id = ig_create_container(ig_user_id, access_token, image_url, caption)
    ig_wait_container_ready(creation_id, access_token)
    media_id = ig_publish(ig_user_id, access_token, creation_id)

    print("Published IG media id:", media_id)

if __name__ == "__main__":
    main()
