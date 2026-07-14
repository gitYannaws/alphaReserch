"""§5 Cluster pains into themes. Reports size + DISTINCT-AUTHOR count (one loud user != market).

Guard against grab-bag megaclusters:
- HDBSCAN runs in `leaf` mode, which yields finer, more homogeneous clusters than the
  default `eom` (which agglomerates into a few big blobs).
- Any surviving cluster whose members are too dispersed (low mean cosine cohesion on the
  ORIGINAL embeddings) is recursively bisected, so one cluster cannot pass off unrelated
  complaints as a single theme. Sub-groups below min_cluster_size fall back to noise.
"""
from collections import defaultdict
import numpy as np

_EPS = 1e-9


def _unit(X: np.ndarray) -> np.ndarray:
    return X / (np.linalg.norm(X, axis=1, keepdims=True) + _EPS)


def _cohesion(X: np.ndarray) -> float:
    """Mean cosine similarity of members to their unit centroid (~theme tightness)."""
    if len(X) <= 1:
        return 1.0
    Xn = _unit(X)
    c = Xn.mean(axis=0)
    c = c / (np.linalg.norm(c) + _EPS)
    return float((Xn @ c).mean())


def _reduce(X: np.ndarray) -> np.ndarray:
    """UMAP -> low-dim before HDBSCAN when enough points; else raw vectors."""
    if len(X) < 15:
        return X
    try:
        import umap
        n = min(10, len(X) - 2)
        return umap.UMAP(n_components=min(5, n), n_neighbors=min(15, len(X) - 1),
                         metric="cosine", random_state=42).fit_transform(X)
    except Exception:
        return X


def _split_incoherent(idx: np.ndarray, X: np.ndarray, min_cluster_size: int,
                      min_cohesion: float, depth: int = 0):
    """Recursively bisect a dispersed group until each part is cohesive or too small.

    Returns a list of index-arrays (into X). Cohesion + KMeans run on the ORIGINAL
    embeddings so a grab-bag cluster is broken by MEANING, not by the UMAP projection.
    """
    sub = X[idx]
    if (len(idx) < 2 * min_cluster_size or depth >= 4
            or _cohesion(sub) >= min_cohesion):
        return [idx]
    try:
        from sklearn.cluster import KMeans
        parts = KMeans(n_clusters=2, n_init=10, random_state=42).fit_predict(_unit(sub))
    except Exception:
        return [idx]
    out = []
    for p in (0, 1):
        child = idx[parts == p]
        if len(child):
            out.extend(_split_incoherent(child, X, min_cluster_size, min_cohesion, depth + 1))
    # a degenerate split (everything on one side) yields the original back -> stop churning
    return out if len(out) > 1 else [idx]


def cluster_run(store, run_id: str, min_cluster_size: int = 2,
                min_cohesion: float = 0.55,
                cluster_selection_method: str = "leaf",
                progress=None) -> dict:
    rows = store.get_embeddings(run_id)
    if len(rows) < min_cluster_size:
        store.clear_clusters(run_id)
        if progress:
            progress(1, 1)
        return {"clusters": 0, "noise": len(rows), "reason": "too few pains"}
    if progress:
        progress(0, 3)

    ids = np.array([r[0] for r in rows], dtype=object)
    authors = [r[2] for r in rows]
    X = np.vstack([np.frombuffer(r[1], dtype="float32") for r in rows])

    import hdbscan
    Xr = _reduce(X)
    if progress:
        progress(1, 3)
    labels = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size, metric="euclidean",
                             cluster_selection_method=cluster_selection_method).fit_predict(Xr)
    if progress:
        progress(2, 3)

    raw_groups = defaultdict(list)
    for i, lab in enumerate(labels):
        if lab != -1:
            raw_groups[lab].append(i)

    # cohesion guard: split any dispersed megacluster into coherent sub-themes
    final, split_count = [], 0
    for members in raw_groups.values():
        parts = _split_incoherent(np.array(members), X, min_cluster_size, min_cohesion)
        if len(parts) > 1:
            split_count += 1
        final.extend(p for p in parts if len(p) >= min_cluster_size)

    store.clear_clusters(run_id)
    for lab, part in enumerate(final):
        pids = [ids[i] for i in part]
        distinct_authors = len({authors[i] for i in part if authors[i]})
        label = store.representative_text(pids)[:120]
        store.save_cluster(f"{run_id}-{lab}", run_id, label, len(pids), distinct_authors, pids)

    noise = len(rows) - sum(len(p) for p in final)
    if progress:
        progress(3, 3)
    return {"clusters": len(final), "noise": int(noise), "split_megaclusters": split_count}
