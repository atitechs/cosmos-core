from sentence_transformers import SentenceTransformer
import os

class AIEmbedder:
    def __init__(self, model_name='BAAI/bge-m3'):
        # BAAI/bge-m3 คือ Multilingual Model ที่เก่งที่สุดตัวหนึ่งสำหรับภาษาไทยและอังกฤษ
        print(f"⏳ Loading Embedding Model ({model_name})... This may take a moment.")
        self.model = SentenceTransformer(model_name)
        print(f"✅ Embedding Model loaded successfully.")

    def encode(self, text):
        return self.model.encode(text).tolist()

_embedder = None

def get_embedder():
    global _embedder
    if _embedder is None:
        _embedder = AIEmbedder()
    return _embedder
