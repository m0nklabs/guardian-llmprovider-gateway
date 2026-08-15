# Guardian Model Benchmarks

Speed measurements for every local model in `config/models.yaml` served through Guardian (`/v1/chat/completions`, streaming). Each model was loaded on demand via Guardian's model-switch path, warmed up once, then measured over N runs. Generation speed (`gen_tps`) excludes the time-to-first-token; `load_switch_s` is the first-run wall-clock including model load + KV cache init.

- **Date:** 2026-08-15T17:23:51.360183+00:00
- **Endpoint:** `http://192.168.1.G:11434/v1`
- **Prompt tokens (approx):** varies per model tokenizer
- **Max tokens per run:** 256
- **Runs per model:** 3 (median reported)
- **Prompt:** `Explain in three sentences how a lighthouse keeper would log tidal patterns, lan...`

## Results

Sorted by generation speed (fastest first). Failed and pending models are listed at the bottom.

| Model | KV type | ngl | load+switch (s) | TTFT (s) | gen tok/s | prompt eval tok/s | status |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| `qwen3.5-9b-coder` | turbo4 | 99 | 14.651 | 0 | 29.87 | 0 | ✅ |
| `qwen3.5-9b-reasoner` | turbo4 | 99 | 13.795 | 0.304 | 27.7 | 141.6 | ✅ |
| `qwen3.5-9b-instruct` | turbo4 | 99 | 13.936 | 0.301 | 27.5 | 142.9 | ✅ |
| `qwen3.5-9b` | turbo4 | 99 | 12.751 | 0 | 26.81 | 0 | ✅ |

## Per-model detail

### `qwen3.5-9b`

- path: `/home/flip/models/qwen3.5-9b/Qwen3.5-9B-UD-Q8_K_XL.gguf`
- mmproj: `/home/flip/models/qwen3.5-9b/mmproj-F16.gguf`
- ngl: 99, tensor_split: `0.45,0.55`, kv_type: `turbo4`
- extra_args: `--jinja --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 1.5 --repeat-penalty 1.0`
- load+switch (first run): **12.751 s**
- median TTFT: **0 s**
- median gen speed: **26.81 tok/s**
- median prompt eval: **0 tok/s**
- all runs gen tok/s: 26.81, 26.61, 26.94

### `qwen3.5-9b-coder`

- path: `/home/flip/models/qwen3.5-9b/Qwen3.5-9B-UD-Q8_K_XL.gguf`
- mmproj: `/home/flip/models/qwen3.5-9b/mmproj-F16.gguf`
- ngl: 99, tensor_split: `0.45,0.55`, kv_type: `turbo4`
- extra_args: `--jinja --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 0.0 --repeat-penalty 1.0`
- load+switch (first run): **14.651 s**
- median TTFT: **0 s**
- median gen speed: **29.87 tok/s**
- median prompt eval: **0 tok/s**
- all runs gen tok/s: 29.87, 29.74, 30.21

### `qwen3.5-9b-instruct`

- path: `/home/flip/models/qwen3.5-9b/Qwen3.5-9B-UD-Q8_K_XL.gguf`
- mmproj: `/home/flip/models/qwen3.5-9b/mmproj-F16.gguf`
- ngl: 99, tensor_split: `0.45,0.55`, kv_type: `turbo4`
- extra_args: `--jinja --reasoning off --temp 0.7 --top-p 0.8 --top-k 20 --min-p 0.0 --presence-penalty 1.5 --repeat-penalty 1.0`
- load+switch (first run): **13.936 s**
- median TTFT: **0.301 s**
- median gen speed: **27.5 tok/s**
- median prompt eval: **142.9 tok/s**
- all runs gen tok/s: 27.47, 27.5, 27.65

### `qwen3.5-9b-reasoner`

- path: `/home/flip/models/qwen3.5-9b/Qwen3.5-9B-UD-Q8_K_XL.gguf`
- mmproj: `/home/flip/models/qwen3.5-9b/mmproj-F16.gguf`
- ngl: 99, tensor_split: `0.45,0.55`, kv_type: `turbo4`
- extra_args: `--jinja --reasoning off --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 1.5 --repeat-penalty 1.0`
- load+switch (first run): **13.795 s**
- median TTFT: **0.304 s**
- median gen speed: **27.7 tok/s**
- median prompt eval: **141.6 tok/s**
- all runs gen tok/s: 25.03, 27.72, 27.7
