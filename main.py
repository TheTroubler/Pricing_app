from __future__ import annotations
import asyncio, json, math, re, time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = Path(__file__).parent
STATIC = BASE / "static"
app = FastAPI(title="Моя корзина", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=STATIC), name="static")

class BasketItem(BaseModel):
    id: str
    name: str
    qty: float = 1
    unit: str = "pack"
    query: str | None = None

class Profile(BaseModel):
    address: str = ""
    no_pork: bool = True
    gluten_ingredients: bool = True
    allow_traces: bool = True

class SearchRequest(BaseModel):
    items: list[BasketItem]
    profile: Profile = Field(default_factory=Profile)
    stores: list[str] = Field(default_factory=lambda:["vkusvill","lenta","ozon","perekrestok","auchan"])

STORE_LABELS = {"vkusvill":"ВкусВилл","lenta":"Лента","ozon":"Ozon Fresh","perekrestok":"Перекрёсток","auchan":"Ашан"}

PORK_WORDS = ["свинин", "свиной", "свиная", "свиное", "шпик", "бекон"]
GLUTEN_WORDS = ["пшениц", "пшенич", "рожь", "ржан", "ячмен", "ячменн", "солод", "спельт", "полб", "сухар", "паниров"]
TRACE_WORDS = ["может содержать", "следы пшени", "следы глютена", "следы злаков"]

def money(v: Any) -> float | None:
    if v is None: return None
    if isinstance(v, (int,float)): return float(v)
    s = str(v).replace("\u00a0", " ").replace("₽", "").replace("руб", "").strip()
    m = re.search(r"(\d[\d\s]*(?:[,.]\d{1,2})?)", s)
    if not m: return None
    try: return float(m.group(1).replace(" ","").replace(",","."))
    except: return None

def size_from_title(title: str):
    s=title.lower().replace(",", ".")
    patterns=[
        (r"(\d+(?:\.\d+)?)\s*(?:л|литр(?:а|ов)?)\b","l",1),
        (r"(\d+(?:\.\d+)?)\s*(?:мл)\b","l",0.001),
        (r"(\d+(?:\.\d+)?)\s*(?:кг)\b","kg",1),
        (r"(\d+(?:\.\d+)?)\s*(?:г|гр)\b","kg",0.001),
        (r"(\d+)\s*(?:шт|штук)\b","pcs",1),
    ]
    for p,u,mul in patterns:
        m=re.search(p,s)
        if m:
            return float(m.group(1))*mul,u
    return 1.0,"pack"

def suitability(item: BasketItem, title: str, composition: str|None):
    text=(title+" "+(composition or "")).lower()
    reasons=[]
    if item.id=="eggs" and not re.search(r"(?:\bс0\b|\bc0\b)", text): reasons.append("не С0")
    if item.id=="milk" and not re.search(r"3[,.]?2\s*%", text): reasons.append("не 3,2%")
    if item.id=="kefir" and not re.search(r"(?:\b1\s*%|1[,.]0\s*%)", text): reasons.append("не 1%")
    return reasons

def diet_status(profile: Profile, title: str, composition: str|None, item_id: str):
    text=(title+" "+(composition or "")).lower()
    if profile.no_pork and any(w in text for w in PORK_WORDS): return "blocked","свинина"
    if profile.gluten_ingredients:
        trace = any(w in text for w in TRACE_WORDS)
        # Do not count a trace warning as an ingredient when traces are allowed.
        scrub = text
        if trace and profile.allow_traces:
            for w in TRACE_WORDS: scrub=scrub.replace(w,"")
        if any(w in scrub for w in GLUTEN_WORDS): return "blocked","глютен-содержащий ингредиент"
        if trace and not profile.allow_traces: return "blocked","следы глютена"
        if trace: return "ok_traces","следы допустимы"
    # For meat snacks, unknown composition is not automatically accepted.
    if item_id=="jerky" and not composition:
        return "verify","нужна проверка состава"
    return "ok","подходит"

class VkusvillMCP:
    url="https://mcp001.vkusvill.ru/mcp"
    def __init__(self): self.session_id=None; self.client=httpx.AsyncClient(timeout=25)
    async def init(self):
        h={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
        payload={"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"grocery-compare","version":"0.2"}}}
        r=await self.client.post(self.url,json=payload,headers=h); r.raise_for_status()
        self.session_id=r.headers.get("mcp-session-id")
        if not self.session_id: raise RuntimeError("VkusVill MCP: no session id")
        h["Mcp-Session-Id"]=self.session_id
        await self.client.post(self.url,json={"jsonrpc":"2.0","method":"notifications/initialized","params":{}},headers=h)
    async def call(self,name,args):
        if not self.session_id: await self.init()
        h={"Content-Type":"application/json","Accept":"application/json, text/event-stream","Mcp-Session-Id":self.session_id}
        p={"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":name,"arguments":args}}
        r=await self.client.post(self.url,json=p,headers=h); r.raise_for_status(); raw=r.json()
        if "error" in raw: raise RuntimeError(str(raw["error"]))
        res=raw.get("result",raw)
        content=res.get("content") if isinstance(res,dict) else None
        if isinstance(content,list) and content:
            t=content[0].get("text","")
            try: return json.loads(t)
            except: return {"text":t}
        return res
    async def search(self,q): return await self.call("vkusvill_products_search",{"q":q,"page":1,"sort":"price"})
    async def details(self,pid): return await self.call("vkusvill_product_details",{"id":int(pid)})

vv=VkusvillMCP()

def recursive_items(obj: Any):
    if isinstance(obj,dict):
        if isinstance(obj.get("items"),list): return obj["items"]
        for v in obj.values():
            x=recursive_items(v)
            if x: return x
    if isinstance(obj,list):
        for v in obj:
            x=recursive_items(v)
            if x: return x
    return []

def pick(d:dict,*keys):
    for k in keys:
        if k in d and d[k] not in (None,""): return d[k]
    return None

def nested_price(d):
    p=d.get("price")
    if isinstance(p,dict): return money(p.get("current") or p.get("value") or p.get("price"))
    return money(p or d.get("current_price") or d.get("price_current"))

async def vkusvill_search(item: BasketItem, profile: Profile):
    data=await vv.search(item.query or item.name)
    raw=recursive_items(data)
    out=[]
    for x in raw[:8]:
        if not isinstance(x,dict): continue
        title=str(pick(x,"name","title","product_name") or "")
        price=nested_price(x)
        pid=pick(x,"id","xml_id","product_id")
        if not title or price is None: continue
        composition=None
        if item.id=="jerky" and pid:
            try:
                det=await vv.details(pid)
                blob=json.dumps(det,ensure_ascii=False)
                # flexible composition extraction
                m=re.search(r'(?i)состав.{0,30}[\":\s]+([^\}\]]{3,600})',blob)
                composition=m.group(1)[:500] if m else None
            except: pass
        size,unit=size_from_title(title)
        status,reason=diet_status(profile,title,composition,item.id)
        mismatch=suitability(item,title,composition)
        if mismatch: continue
        out.append({"store":"vkusvill","storeLabel":STORE_LABELS["vkusvill"],"title":title,"price":price,"size":size,"unit":unit,"url":str(pick(x,"url","link") or "https://vkusvill.ru/search/?q="+quote(item.query or item.name)),"composition":composition,"diet":status,"dietReason":reason,"availability":"catalog","addressLevel":"catalog"})
    return out

SEARCH_URLS={
 "lenta":"https://lenta.com/search/?search={q}",
 "perekrestok":"https://www.perekrestok.ru/cat/search?search={q}",
 "auchan":"https://www.auchan.ru/search/?query={q}",
 "ozon":"https://www.ozon.ru/search/?text={q}&miniapp=supermarket",
}

async def browser_search(store:str,item:BasketItem,profile:Profile):
    # Import lazily: if Chromium cannot start, one adapter fails without killing the app.
    from playwright.async_api import async_playwright
    url=SEARCH_URLS[store].format(q=quote(item.query or item.name))
    offers=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=["--no-sandbox","--disable-dev-shm-usage"])
        context=await browser.new_context(locale="ru-RU",timezone_id="Europe/Moscow",user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1")
        page=await context.new_page()
        try:
            await page.goto(url,wait_until="domcontentloaded",timeout=30000)
            await page.wait_for_timeout(3500)
            candidates=await page.evaluate('''() => {
              const rub=/\\b\\d[\\d\\s]*(?:[,.]\\d{1,2})?\\s*(?:₽|руб)/i;
              const out=[]; const seen=new Set();
              for(const a of [...document.querySelectorAll('a[href]')]){
                const href=a.href||''; if(!href || seen.has(href)) continue;
                let el=a; let txt='';
                for(let i=0;i<4 && el;i++,el=el.parentElement){ const t=(el.innerText||'').trim(); if(t.length>txt.length && t.length<1800) txt=t; }
                if(!rub.test(txt) || txt.length<12) continue;
                const lines=txt.split(/\\n+/).map(s=>s.trim()).filter(Boolean);
                let title=lines.find(x=>x.length>8 && !rub.test(x) && !/в корзину|достав|отзыв|скидк/i.test(x)) || (a.innerText||'').trim();
                const pm=txt.match(rub); if(!title || !pm) continue;
                seen.add(href); out.push({href,title,text:txt,priceText:pm[0]});
                if(out.length>=18) break;
              }
              return out;
            }''')
            for c in candidates:
                title=c.get("title","").strip(); price=money(c.get("priceText"))
                if not title or price is None: continue
                mismatch=suitability(item,title,None)
                if mismatch: continue
                size,unit=size_from_title(title+" "+c.get("text",""))
                status,reason=diet_status(profile,title,None,item.id)
                offers.append({"store":store,"storeLabel":STORE_LABELS[store],"title":title,"price":price,"size":size,"unit":unit,"url":c.get("href") or url,"composition":None,"diet":status,"dietReason":reason,"availability":"page","addressLevel":"moscow_region"})
        finally:
            await browser.close()
    # cheapest unique titles
    uniq={}
    for o in offers:
        k=o["title"].lower()
        if k not in uniq or o["price"]<uniq[k]["price"]: uniq[k]=o
    return sorted(uniq.values(),key=lambda x:x["price"])[:8]

async def one_store(store,item,profile):
    t0=time.time()
    try:
        data=await (vkusvill_search(item,profile) if store=="vkusvill" else browser_search(store,item,profile))
        return {"store":store,"ok":True,"elapsed":round(time.time()-t0,2),"offers":data,"error":None}
    except Exception as e:
        return {"store":store,"ok":False,"elapsed":round(time.time()-t0,2),"offers":[],"error":str(e)[:300]}

@app.get("/")
async def index(): return FileResponse(STATIC/"index.html")

@app.get("/api/health")
async def health(): return {"ok":True,"version":"0.2.0","stores":STORE_LABELS}

@app.post("/api/search")
async def search(req: SearchRequest):
    items=[]
    for item in req.items:
        tasks=[one_store(s,item,req.profile) for s in req.stores if s in STORE_LABELS]
        res=await asyncio.gather(*tasks)
        offers=[]; adapters=[]
        for r in res:
            adapters.append({k:r[k] for k in ("store","ok","elapsed","error")})
            offers.extend(r["offers"])
        items.append({"item":item.model_dump(),"offers":offers,"adapters":adapters})
    return {"generatedAt":time.time(),"address":req.profile.address,"items":items,"note":"Address-level availability is not yet guaranteed for every store. UI shows source level per offer."}
