import os

from .application import MangaApplication

for key, value in {
    "QWEN_N_CTX": "2048",
    "QWEN_N_BATCH": "128",
    "QWEN_N_UBATCH": "64",
    "QWEN_N_GPU_LAYERS": "-1",
    "QWEN_FLASH_ATTN": "on",
    "QWEN_OFFLOAD_KQV": "on",
    "QWEN_OP_OFFLOAD": "on",
    "QWEN_TYPE_K": "q4_0",
    "QWEN_TYPE_V": "q4_0",
    "QWEN_N_THREADS": "4",
    "QWEN_N_THREADS_BATCH": "4",
    "QWEN_VERBOSE": "false",
}.items():
    os.environ.setdefault(key, value)

raise SystemExit(MangaApplication().run())
