def pick_prompt(data: dict) -> dict:
    cars = data["cars"]
    years = data["years"]
    scenes = data["scenes"]

    # deterministički shuffle
    seed = "TMG_PROMPT_ENGINE_V1"
    rnd = random.Random(seed)

    combos = []

    for car in cars:
        for year in years:
            for scene in scenes:
                combos.append({
                    "prompt": f"{year} {car}, ultra realistic photo, {scene}, cinematic lighting, sharp focus, clean background, no people, no text, no watermark",
                    "hashtags": "#classiccar #vintagecar #timemachinegarage"
                })

    rnd.shuffle(combos)

    start_date = dt.date(2026, 3, 1)
    today = dt.datetime.utcnow().date()

    days = (today - start_date).days
    index = days % len(combos)

    return combos[index]
