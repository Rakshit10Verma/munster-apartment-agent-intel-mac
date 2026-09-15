import os, requests
from dataclasses import dataclass
from pydantic import BaseModel
from .utils import extract_json
from .config_loader import env_bool, cloud_order

@dataclass
class ProviderResult:
    provider: str
    model: str
    data: BaseModel

class ProviderError(RuntimeError): pass

def validate(cls, text):
    return cls.model_validate(extract_json(text))

def call_llamacpp(system, prompt, cls):
    base = os.getenv("LLAMA_CPP_BASE_URL","http://127.0.0.1:8080/v1").rstrip("/")
    label = os.getenv("LOCAL_MODEL_LABEL","gemma-3-4b-it-Q4_K_M")
    try:
        r = requests.post(
            base + "/chat/completions",
            json={
                "model":"local",
                "messages":[{"role":"system","content":system},{"role":"user","content":prompt}],
                "temperature":0.2,
                "max_tokens":3000
            },
            timeout=300,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        return ProviderResult("llama.cpp", label, validate(cls,text))
    except Exception as e:
        raise ProviderError(f"local llama.cpp failed: {e}")

def call_anthropic(system,prompt,cls):
    key=os.getenv("ANTHROPIC_API_KEY","").strip()
    if not key: raise ProviderError("Anthropic key not configured")
    model=os.getenv("ANTHROPIC_MODEL","claude-sonnet-5")
    try:
        from anthropic import Anthropic
        c=Anthropic(api_key=key)
        m=c.messages.create(model=model,max_tokens=3000,temperature=.2,system=system+" Return only valid JSON.",
                            messages=[{"role":"user","content":prompt}])
        text="".join(b.text for b in m.content if getattr(b,"type",None)=="text")
        return ProviderResult("anthropic",model,validate(cls,text))
    except Exception as e: raise ProviderError(f"Anthropic failed: {e}")

def call_openai(system,prompt,cls):
    key=os.getenv("OPENAI_API_KEY","").strip()
    if not key: raise ProviderError("OpenAI key not configured")
    model=os.getenv("OPENAI_MODEL","gpt-5.6-luna")
    try:
        from openai import OpenAI
        c=OpenAI(api_key=key)
        res=c.responses.create(model=model,instructions=system+" Return only valid JSON.",input=prompt)
        return ProviderResult("openai",model,validate(cls,res.output_text))
    except Exception as e: raise ProviderError(f"OpenAI failed: {e}")

def call_gemini(system,prompt,cls):
    key=os.getenv("GEMINI_API_KEY","").strip()
    if not key: raise ProviderError("Gemini key not configured")
    model=os.getenv("GEMINI_MODEL","gemini-3.8-flash")
    try:
        from google import genai
        from google.genai import types
        c=genai.Client(api_key=key)
        res=c.models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(system_instruction=system,response_mime_type="application/json",temperature=.2)
        )
        return ProviderResult("gemini",model,validate(cls,res.text))
    except Exception as e: raise ProviderError(f"Gemini failed: {e}")

CALLERS={"anthropic":call_anthropic,"openai":call_openai,"gemini":call_gemini}

def cloud_chain(system,prompt,cls,notes):
    for name in cloud_order():
        try: return CALLERS[name](system,prompt,cls)
        except Exception as e: notes.append(str(e))
    raise ProviderError("All cloud providers failed: "+" | ".join(notes))

def route(system,prompt,cls):
    notes=[]
    threshold=float(os.getenv("LOCAL_CONFIDENCE_THRESHOLD","0.84"))
    if env_bool("LOCAL_FIRST",True):
        try:
            local=call_llamacpp(system,prompt,cls)
            conf=float(getattr(local.data,"confidence",0))
            needs=bool(getattr(local.data,"needs_cloud",False))
            if conf>=threshold and not needs:
                return local,notes
            notes.append(f"Local escalated: confidence={conf:.2f}, needs_cloud={needs}")
        except Exception as e:
            notes.append(str(e))
    return cloud_chain(system,prompt,cls,notes),notes
