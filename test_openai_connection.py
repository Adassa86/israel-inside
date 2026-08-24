import os
import httpx
from openai import OpenAI

api_key = os.environ.get("OPENAI_API_KEY")
if not api_key:
    raise SystemExit("OPENAI_API_KEY absente dans cette fenêtre.")

http_client = httpx.Client(trust_env=False, timeout=30.0)
client = OpenAI(api_key=api_key, http_client=http_client)

try:
    models = client.models.list()
    print("Connexion Python -> OpenAI : OK")
    print("Premier modèle :", models.data[0].id if models.data else "aucun")
finally:
    client.close()
