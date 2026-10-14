"""Lance l'interface web et l'API : python -m api  (http://127.0.0.1:8000)."""
import os

import uvicorn

# 127.0.0.1 par défaut (poste local) ; Docker définit DATASENTINEL_HOST=0.0.0.0.
uvicorn.run("api.main:app", host=os.getenv("DATASENTINEL_HOST", "127.0.0.1"), port=int(os.getenv("DATASENTINEL_PORT", "8000")))
