import os
import sys

# Permite `import main`, `import gemini_service` etc. a partir da raiz do projeto.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Os testes nunca devem tocar o gateway, o Supabase ou o Google de verdade.
os.environ.setdefault("ROTA_IA_API_KEY", "teste")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")
