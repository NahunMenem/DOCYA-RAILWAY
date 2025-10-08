import os
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 Iniciando servidor DOCYA en puerto {port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port)
