"""Embedding retrieval Deliverer — dense semantic matching over the EM library.

Probe-validated: on knowledge-heavy tasks, dense retrieval ranks cards
better than lexical overlap does
(A_naive lex-top1 5/9 -> emb-top1 7/9; every variant improves under embedding).
Lexical (need_driven_retrieval) is the zero-dependency default; packages that
want dense select THIS deliverer, which loads an embedder by name.

Kept decoupled: the embedder is injected by NAME (an env/config string), so the
kernel has no hard dependency on any vendor SDK. Card vectors are computed once
and cached in-process. Same DeliveryAction contract as need_driven_retrieval,
so grounding/coverage are identical — only the ranking backend differs.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from loguru import logger

from recuris.events import Event
from recuris.spi import CoverWithDoc, TriggerContext


def _load_embedder(name: str):
    """Resolve an embedder by name. 'doubao_vision' -> the fork's Ark embedder.
    Import is lazy so a package NOT using embeddings never needs the SDK."""
    if name in ("doubao_vision", "doubao-embedding-vision-251215"):
        from tau2.knowledge.embedders.doubao_vision_embedder import DoubaoVisionEmbedder
        return DoubaoVisionEmbedder(max_workers=8)
    raise ValueError(f"unknown embedder '{name}'")


def _cosine_topk(qvec, mat, k):
    import numpy as np

    q = np.asarray(qvec, float)
    q = q / (np.linalg.norm(q) + 1e-9)
    sims = mat @ q
    order = np.argsort(-sims)[:k]
    return [(float(sims[i]), int(i)) for i in order]


class EmbeddingRetrieval:
    """Dense-retrieval variant of need_driven_retrieval (SPI ③).

    Query = the pending entry's description (on a knowledge task that IS the
    verbalized blind spot). Docs = each EM entry (id + head). Ranking = cosine.
    On-disk vector cache keyed by (embedder, card-set hash) so re-runs are free.
    """

    name = "embedding_retrieval"
    events = (Event.INTENT_RECORDED.value,)

    def __init__(self, embedder: str = "doubao_vision",
                 em_types: tuple = ("knowledge", "procedure"),
                 top_k: int = 2, min_sim: float = 0.30, head_chars: int = 300,
                 settle: bool = True, scope_by_tool: bool = False,
                 max_per_episode: int = 0):
        self.embedder_name = embedder
        self.em_types = tuple(em_types)
        self.top_k = int(top_k)
        self.min_sim = float(min_sim)
        self.head_chars = int(head_chars)
        self.settle = bool(settle)
        self.scope_by_tool = bool(scope_by_tool)
        self.max_per_episode = int(max_per_episode)
        if self.max_per_episode < 0:
            raise ValueError("max_per_episode must be >= 0")
        self._embedder = None
        self._mat = None          # cached card matrix (normalized)
        self._doc_ids: list = []
        self._doc_bodies: dict = {}
        self._doc_tools: dict = {}

    # ── lazy card-vector build with on-disk cache ────────────────────────
    def _ensure_index(self, em):
        if self._mat is not None:
            return
        import numpy as np

        docs = [e for e in em.entries if e.type in self.em_types]
        self._doc_ids = [d.id for d in docs]
        self._doc_bodies = {d.id: d.body for d in docs}
        self._doc_tools = {d.id: getattr(d, "trigger_tool", "") for d in docs}
        texts = [f"{d.id}\n{d.body[:self.head_chars]}" for d in docs]
        key = hashlib.sha1(
            (self.embedder_name + "|" + "|".join(sorted(self._doc_ids))).encode()
        ).hexdigest()[:16]
        cache = Path(os.getenv("RECURIS_EMBED_CACHE", ".embed_cache")) / f"{key}.npy"
        if cache.exists():
            self._mat = np.load(cache)
            logger.info(f"[skfw.embed] card vectors from cache {cache}")
            return
        self._embedder = self._embedder or _load_embedder(self.embedder_name)
        vecs = np.stack([np.asarray(v, float) for v in self._embedder.embed(texts)])
        vecs = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)
        self._mat = vecs
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.save(cache, vecs)
        logger.info(f"[skfw.embed] embedded {len(texts)} cards -> {cache}")

    def select(self, ctx: TriggerContext, wm_view, em, draft=None):
        pend = wm_view.pending()
        if not pend:
            return []
        self._ensure_index(em)
        self._embedder = self._embedder or _load_embedder(self.embedder_name)
        actions = []
        already = ctx.extra.get("delivered_docs") or set()
        scheduled = {
            did: sum(1 for delivered in already
                     if isinstance(delivered, tuple) and delivered and delivered[0] == did)
            for did in self._doc_ids
        }
        queries = [e.description for e in pend]
        qvecs = self._embedder.embed(queries)
        for entry, qv in zip(pend, qvecs):
            selected = 0
            rank_k = len(self._doc_ids) if self.scope_by_tool else self.top_k
            for sim, idx in _cosine_topk(qv, self._mat, rank_k):
                if sim < self.min_sim:
                    break
                did = self._doc_ids[idx]
                scope = self._doc_tools.get(did, "")
                if self.scope_by_tool and scope != "*" and scope != getattr(entry, "tool", ""):
                    continue
                if (self.max_per_episode and
                        scheduled.get(did, 0) >= self.max_per_episode):
                    continue
                if (did, entry.description) in already:
                    continue
                actions.append(CoverWithDoc(entry_id=entry.id, doc_id=did,
                                            content=self._doc_bodies[did],
                                            settle=self.settle))
                scheduled[did] = scheduled.get(did, 0) + 1
                selected += 1
                if selected >= self.top_k:
                    break
        return actions
