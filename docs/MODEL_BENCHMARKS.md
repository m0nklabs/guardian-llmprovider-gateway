# Guardian Model Benchmarks

Speed measurements for every local model in `config/models.yaml` served through Guardian (`/v1/chat/completions`, streaming). Each model was loaded on demand via Guardian's model-switch path, warmed up once, then measured over N runs. Generation speed (`gen_tps`) excludes the time-to-first-token; `load_switch_s` is the first-run wall-clock including model load + KV cache init.

- **Date:** 2026-08-15T15:59:33.875160+00:00
- **Endpoint:** `http://192.168.1.G:11434/v1`
- **Prompt tokens (approx):** varies per model tokenizer
- **Max tokens per run:** 256
- **Runs per model:** 3 (median reported)
- **Prompt:** `Explain in three sentences how a lighthouse keeper would log tidal patterns, lan...`

## Results

Sorted by generation speed (fastest first). Failed and pending models are listed at the bottom.

| Model | KV type | ngl | load+switch (s) | TTFT (s) | gen tok/s | prompt eval tok/s | status |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| `unsloth-gemma-4-26B-A4B-it-qat-UD-Q4_K_XL-Q8KV` | turbo4 | 99 | 37.255 | 0 | 74.43 | 0 | ✅ |
| `gemma-4-E4B-it-uncensored` | f16 | 99 | 19.245 | 0 | 69.72 | 0 | ✅ |
| `qwen3.8-27b-q8kv` | q8_0 | 99 | 16.886 | 9.336 | 64.78 | 8.9 | ✅ |
| `qwen3.8-27b` | turbo4 | 99 | 15.468 | 9.098 | 63.4 | 9.1 | ✅ |
| `Huihui-gemma-4-26B-A4B-it-abliterated` | turbo4 | 99 | 29.033 | 0 | 59.84 | 0 | ✅ |
| `Huihui-gemma-4-26B-A4B-it-abliterated-Q8KV` | turbo4 | 99 | 14.548 | 0 | 57.36 | 0 | ✅ |
| `qwen3.8-27b-instruct` | turbo4 | 99 | 13.894 | 0.541 | 17.64 | 79.5 | ✅ |

## Per-model detail

### `Huihui-gemma-4-26B-A4B-it-abliterated`

- path: `/home/flip/models/gemma-4-26B-A4B.unsloth-imatrix-UD-Q4_K_XL.gguf`
- mmproj: `/home/flip/models/gemma-4-26b-a4b-mmproj-BF16.gguf`
- ngl: 99, tensor_split: `0.45,0.55`, kv_type: `turbo4`
- extra_args: `--flash-attn on --repeat-penalty 1 --temp 1.0 --top-p 0.95 --top-k 64`
- load+switch (first run): **29.033 s**
- median TTFT: **0 s**
- median gen speed: **59.84 tok/s**
- median prompt eval: **0 tok/s**
- all runs gen tok/s: 59.84, 57.8, 60.37

### `Huihui-gemma-4-26B-A4B-it-abliterated-Q8KV`

- path: `/home/flip/models/gemma-4-26B-A4B.unsloth-imatrix-UD-Q4_K_XL.gguf`
- mmproj: `/home/flip/models/gemma-4-26b-a4b-mmproj-BF16.gguf`
- ngl: 99, tensor_split: `0.36,0.64`, kv_type: `turbo4`
- extra_args: `--flash-attn on --repeat-penalty 1 --temp 1.0 --top-p 0.95 --top-k 64`
- load+switch (first run): **14.548 s**
- median TTFT: **0 s**
- median gen speed: **57.36 tok/s**
- median prompt eval: **0 tok/s**
- all runs gen tok/s: 57.36, 55.59, 59.97

### `gemma-4-E4B-it-uncensored`

- path: `/home/flip/models/gemma-4-E4B-it-uncensored-Q4_K_M.gguf`
- mmproj: none
- ngl: 99, tensor_split: `0.32,0.68`, kv_type: `f16`
- extra_args: `--flash-attn on --parallel 1 --batch-size 256 --ubatch-size 128 --repeat-penalty 1 --repeat-last-n 128 --dry-multiplier 1.0 --dry-base 1.75 --dry-penalty-last-n 256 --temp 1.0 --top-p 0.95 --top-k 64`
- load+switch (first run): **19.245 s**
- median TTFT: **0 s**
- median gen speed: **69.72 tok/s**
- median prompt eval: **0 tok/s**
- all runs gen tok/s: 69.39, 70.59, 69.72

### `qwen3.8-27b`

- path: `/home/flip/models/qwen3.8-27b/Qwen3.8-27B-UD-Q4_K_XL.gguf`
- mmproj: `/home/flip/models/qwen3.8-27b/mmproj-F16.gguf`
- ngl: 99, tensor_split: `0.45,0.55`, kv_type: `turbo4`
- extra_args: `--jinja --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 0.0 --repeat-penalty 1.0`
- load+switch (first run): **15.468 s**
- median TTFT: **9.098 s**
- median gen speed: **63.4 tok/s**
- median prompt eval: **9.1 tok/s**
- all runs gen tok/s: 63.28, 63.71, 63.4

### `qwen3.8-27b-instruct`

- path: `/home/flip/models/qwen3.8-27b/Qwen3.8-27B-UD-Q4_K_XL.gguf`
- mmproj: `/home/flip/models/qwen3.8-27b/mmproj-F16.gguf`
- ngl: 99, tensor_split: `0.45,0.55`, kv_type: `turbo4`
- extra_args: `--jinja --reasoning off --temp 0.7 --top-p 0.8 --top-k 20 --min-p 0.0 --presence-penalty 1.5 --repeat-penalty 1.0`
- load+switch (first run): **13.894 s**
- median TTFT: **0.541 s**
- median gen speed: **17.64 tok/s**
- median prompt eval: **79.5 tok/s**
- all runs gen tok/s: 17.49, 17.64, 17.64

### `qwen3.8-27b-q8kv`

- path: `/home/flip/models/qwen3.8-27b/Qwen3.8-27B-UD-Q4_K_XL.gguf`
- mmproj: `/home/flip/models/qwen3.8-27b/mmproj-F16.gguf`
- ngl: 99, tensor_split: `0.40,0.60`, kv_type: `q8_0`
- extra_args: `--jinja --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 0.0 --repeat-penalty 1.0`
- load+switch (first run): **16.886 s**
- median TTFT: **9.336 s**
- median gen speed: **64.78 tok/s**
- median prompt eval: **8.9 tok/s**
- all runs gen tok/s: 64.78, 64.87, 57.19

### `unsloth-gemma-4-26B-A4B-it-qat-UD-Q4_K_XL-Q8KV`

- path: `/home/flip/models/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf`
- mmproj: `/home/flip/models/gemma-4-26b-a4b-mmproj-BF16.gguf`
- ngl: 99, tensor_split: `0.36,0.64`, kv_type: `turbo4`
- extra_args: `--flash-attn on --repeat-penalty 1 --temp 1.0 --top-p 0.95 --top-k 64 --main-gpu 1 -sm layer`
- load+switch (first run): **37.255 s**
- median TTFT: **0 s**
- median gen speed: **74.43 tok/s**
- median prompt eval: **0 tok/s**
- all runs gen tok/s: 76.74, 74.21, 74.43
