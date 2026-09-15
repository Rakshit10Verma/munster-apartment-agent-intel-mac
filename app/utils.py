import json, re

def extract_json(text: str):
    text = text.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S|re.I).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        dec = json.JSONDecoder()
        for i,c in enumerate(text):
            if c == "{":
                try:
                    obj,_ = dec.raw_decode(text[i:])
                    return obj
                except Exception:
                    pass
    raise ValueError("No JSON object found in model response")
