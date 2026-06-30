from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
url = os.getenv("SUPABASE_URL").rstrip("/")
key = os.getenv("SUPABASE_KEY")

print(f"URL: '{url}'")  # confere se tem barra ou path extra

sb = create_client(url, key)

# Lista as tabelas disponíveis
try:
    r = sb.table("vagas_extraidas").select("*").limit(1).execute()
    print("Tabela vagas_extraidas: OK →", r)
except Exception as e:
    print("ERRO vagas_extraidas:", e)

# Testa o nome alternativo mais comum
try:
    r2 = sb.table("vagas").select("*").limit(1).execute()
    print("Tabela 'vagas': OK →", r2.data[:1])
except Exception as e2:
    print("ERRO 'vagas':", e2)