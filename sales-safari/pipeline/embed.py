"""§4 Local embeddings. Default bge-small (fast, ~130MB); swap to bge-large in config."""
import numpy as np

_MODEL = None
_MODEL_NAME = None


def _model(name: str):
    global _MODEL, _MODEL_NAME
    if _MODEL is None or _MODEL_NAME != name:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(name)
        _MODEL_NAME = name
    return _MODEL


def pain_text(p: dict) -> str:
    parts = [p.get("complaint"), p.get("workflow_pain"), p.get("wish"), p.get("verbatim_span")]
    return " ".join(x for x in parts if x)


def embed_run(store, run_id: str, model_name: str = "BAAI/bge-small-en-v1.5",
              progress=None, batch_size: int = 32) -> dict:
    pains = store.get_pains(run_id)
    if not pains:
        if progress:
            progress(0, 0)
        return {"embedded": 0}
    if progress:
        progress(0, len(pains))
    model = _model(model_name)
    done = 0
    for i in range(0, len(pains), batch_size):
        batch = pains[i:i + batch_size]
        texts = [pain_text(p) for p in batch]
        vecs = model.encode(texts, normalize_embeddings=True, batch_size=batch_size,
                            show_progress_bar=False)
        for p, v in zip(batch, vecs):
            store.save_embedding(run_id, p["id"], np.asarray(v, dtype="float32").tobytes())
        done += len(batch)
        if progress:
            progress(done, len(pains))
    return {"embedded": len(pains)}
