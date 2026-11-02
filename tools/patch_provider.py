# -*- coding: utf-8 -*-
import json
import os

path = os.path.join(os.environ["APPDATA"], "dsh-pet-standalone", "config.json")
with open(path, encoding="utf-8") as fh:
    data = json.load(fh)
data["companion_image_provider"] = "openai"
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
print("provider ->", data["companion_image_provider"])
print("base ->", data["companion_image_base_url"])
print("model ->", data["companion_image_model"])
