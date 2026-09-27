import logging
import warnings

# Avertissements sans conséquence liés au Python 3.9 du Mac (fin de vie, LibreSSL).
# Ils disparaissent avec un Python récent (brew install python).
warnings.filterwarnings("ignore", category=FutureWarning, module=r"google\..*")
warnings.filterwarnings("ignore", message=r".*OpenSSL.*")

# Le SDK Gemini signale que la réponse contient aussi la « réflexion » du modèle
# (thought_signature) : normal, on ne lit que le JSON.
logging.getLogger("google_genai.types").setLevel(logging.ERROR)
