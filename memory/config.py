"""Tunable constants for the long-term memory layer.

Kept in one place so the future memory *service* can expose them as config.
Distances are cosine (sqlite-vec `vec_distance_cosine`): 0 = identical, 2 = opposite.
"""

# --- Reconciliation (write-time) ---
# Two facts whose embeddings are within this cosine distance AND share
# subject+predicate are treated as the same fact (literal-object drift merge).
NEAR_DUP_DISTANCE = 0.12

# --- Retrieval (per-turn, read-time) ---
RETRIEVAL_K = 5
RETRIEVAL_DISTANCE = 0.55          # max distance for a fact to be injected on a turn

# --- Profile / Block 1 (identity render) ---
PROFILE_CONFIDENCE_FLOOR = 0.5     # live facts at/above this confidence enter Block 1

# --- Synthesis / Block 2 (themes rollup) ---
SYNTHESIS_THRESHOLD = 12           # salient conversations closed before a Block-2 rebuild
SYNTHESIS_WINDOW = 50              # most-recent summaries fed to the rollup

# --- Confidence model: f(trust_tier, #distinct source convs, recency) ---
TIER_BASE = {"asserted": 0.9, "derived": 0.5, "untrusted": 0.2}
REINFORCE_STEP = 0.08              # per extra distinct source conversation
REINFORCE_CAP = 0.3
RECENCY_HALFLIFE_DAYS = 365.0
RECENCY_MAX_PENALTY = 0.15

VALID_TIERS = ("asserted", "derived", "untrusted")
