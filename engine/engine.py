from fastapi import FastAPI
 

app = FastAPI(title="Forge Engine")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "engine"}
