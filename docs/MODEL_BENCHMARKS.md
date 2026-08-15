# Guardian Model Benchmarks

Speed measurements for every local model in `config/models.yaml` served through Guardian (`/v1/chat/completions`, streaming). Each model was loaded on demand via Guardian's model-switch path, warmed up once, then measured over N runs. Generation speed (`gen_tps`) excludes the time-to-first-token; `load_switch_s` is the first-run wall-clock including model load + KV cache init.

- **Date:** 2026-08-15T13:42:15.877516+00:00
- **Endpoint:** `http://192.168.1.G:11434/v1`
- **Prompt tokens (approx):** varies per model tokenizer
- **Max tokens per run:** 256
- **Runs per model:** 3 (median reported)
- **Prompt:** `Explain in three sentences how a lighthouse keeper would log tidal patterns, lan...`

## Results

Sorted by generation speed (fastest first). Failed and pending models are listed at the bottom.

| Model | KV type | ngl | load+switch (s) | TTFT (s) | gen tok/s | prompt eval tok/s | status |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| `llama3.2-3b` | f16 | 99 | 10.906 | 0.127 | 126.27 | 520.1 | ✅ |
| `Qwen3.6-35B-A3B-HauhauCS-Aggressive` | turbo4 | 99 | 43.914 | 0 | 85.0 | 0 | ✅ |
| `Qwen3.6-35B-A3B-HauhauCS-Aggressive-Q8KV` | q8_0 | 99 | 12.282 | 0 | 82.92 | 0 | ✅ |
| `Qwen3.6-35B-A3B-HauhauCS-Aggressive-Turbo4` | turbo4 | 99 | 13.275 | 0 | 81.48 | 0 | ✅ |
| `unsloth-gemma-4-26B-A4B-it-qat-UD-Q4_K_XL-Q8KV` | turbo4 | 99 | 35.923 | 0 | 76.09 | 0 | ✅ |
| `gemma-4-E4B-it-uncensored` | f16 | 99 | 17.262 | 0 | 70.49 | 0 | ✅ |
| `qwen3.8-27b` | turbo4 | 99 | 23.957 | 9.067 | 63.68 | 9.2 | ✅ |
| `Huihui-gemma-4-26B-A4B-it-abliterated-Q8KV` | turbo4 | 99 | 22.831 | 0 | 54.11 | 0 | ✅ |
| `granite-4.1-8b` | turbo4 | 99 | 27.133 | 0.145 | 30.97 | 269.6 | ✅ |
| `Phi-4-reasoning-plus` | f16 | 99 | 70.421 | 33.001 | 23.33 | 8.2 | ✅ |
| `qwen3.8-27b-instruct` | turbo4 | 99 | 15.445 | 0.434 | 17.06 | 99.1 | ✅ |
| `Qwen3.6-35B-A3B-HauhauCS-Aggressive-DFlash-Turbo4` | turbo4 | 99 | 20.581 | 0 | 15.77 | 0 | ✅ |
| `Huihui-gemma-4-26B-A4B-it-abliterated` | turbo4 | 99 | 43.331 | 0 | 5.46 | 0 | ✅ |

## Per-model detail

### `Huihui-gemma-4-26B-A4B-it-abliterated`

- path: `/home/flip/models/gemma-4-26B-A4B.unsloth-imatrix-UD-Q4_K_XL.gguf`
- mmproj: `/home/flip/models/gemma-4-26b-a4b-mmproj-BF16.gguf`
- ngl: 99, tensor_split: `0.45,0.55`, kv_type: `turbo4`
- extra_args: `--flash-attn on --repeat-penalty 1 --temp 1.0 --top-p 0.95 --top-k 64`
- load+switch (first run): **43.331 s**
- median TTFT: **0 s**
- median gen speed: **5.46 tok/s**
- median prompt eval: **0 tok/s**
- all runs gen tok/s: 5.46, 4.58, 6.29

### `Huihui-gemma-4-26B-A4B-it-abliterated-Q8KV`

- path: `/home/flip/models/gemma-4-26B-A4B.unsloth-imatrix-UD-Q4_K_XL.gguf`
- mmproj: `/home/flip/models/gemma-4-26b-a4b-mmproj-BF16.gguf`
- ngl: 99, tensor_split: `0.36,0.64`, kv_type: `turbo4`
- extra_args: `--flash-attn on --repeat-penalty 1 --temp 1.0 --top-p 0.95 --top-k 64`
- load+switch (first run): **22.831 s**
- median TTFT: **0 s**
- median gen speed: **54.11 tok/s**
- median prompt eval: **0 tok/s**
- all runs gen tok/s: 55.99, 51.23, 54.11

### `Phi-4-reasoning-plus`

- path: `/home/flip/models/Phi-4-reasoning-plus-Q8_0.gguf`
- mmproj: none
- ngl: 99, tensor_split: `0.45,0.55`, kv_type: `f16`
- extra_args: `—`
- load+switch (first run): **70.421 s**
- median TTFT: **33.001 s**
- median gen speed: **23.33 tok/s**
- median prompt eval: **8.2 tok/s**
- all runs gen tok/s: 23.28, 23.33, 23.37

### `Qwen3.6-35B-A3B-HauhauCS-Aggressive`

- path: `/home/flip/models/Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf`
- mmproj: `/home/flip/models/Qwen3.6-35B-A3B-mmproj-BF16.gguf`
- ngl: 99, tensor_split: `0.38,0.62`, kv_type: `turbo4`
- extra_args: `--reasoning on --reasoning-format deepseek --reasoning-budget -1 --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 0.0 --repeat-penalty 1.0`
- load+switch (first run): **43.914 s**
- median TTFT: **0 s**
- median gen speed: **85.0 tok/s**
- median prompt eval: **0 tok/s**
- all runs gen tok/s: 86.39, 81.92, 85.0

### `Qwen3.6-35B-A3B-HauhauCS-Aggressive-DFlash-Turbo4`

- path: `/home/flip/models/Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf`
- mmproj: `/home/flip/models/Qwen3.6-35B-A3B-mmproj-BF16.gguf`
- ngl: 99, tensor_split: `0.38,0.62`, kv_type: `turbo4`
- extra_args: `--flash-attn on --parallel 1 --batch-size 256 --ubatch-size 128 --reasoning on --reasoning-format deepseek --reasoning-budget -1 --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 0.0 --repeat-penalty 1.0`
- load+switch (first run): **20.581 s**
- median TTFT: **0 s**
- median gen speed: **15.77 tok/s**
- median prompt eval: **0 tok/s**
- all runs gen tok/s: 15.77, 16.13, 13.51

### `Qwen3.6-35B-A3B-HauhauCS-Aggressive-Q8KV`

- path: `/home/flip/models/Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf`
- mmproj: `/home/flip/models/Qwen3.6-35B-A3B-mmproj-BF16.gguf`
- ngl: 99, tensor_split: `0.36,0.64`, kv_type: `q8_0`
- extra_args: `--reasoning on --reasoning-format deepseek --reasoning-budget -1 --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 0.0 --repeat-penalty 1.0`
- load+switch (first run): **12.282 s**
- median TTFT: **0 s**
- median gen speed: **82.92 tok/s**
- median prompt eval: **0 tok/s**
- all runs gen tok/s: 87.16, 77.47, 82.92

### `Qwen3.6-35B-A3B-HauhauCS-Aggressive-Turbo4`

- path: `/home/flip/models/Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf`
- mmproj: `/home/flip/models/Qwen3.6-35B-A3B-mmproj-BF16.gguf`
- ngl: 99, tensor_split: `0.38,0.62`, kv_type: `turbo4`
- extra_args: `--flash-attn on --batch-size 1024 --ubatch-size 256 --rope-scaling yarn --rope-scale 1.5 --yarn-orig-ctx 262144 --reasoning on --reasoning-format deepseek --reasoning-budget -1 --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 0.0 --repeat-penalty 1.0`
- load+switch (first run): **13.275 s**
- median TTFT: **0 s**
- median gen speed: **81.48 tok/s**
- median prompt eval: **0 tok/s**
- all runs gen tok/s: 81.48, 79.24, 83.44

### `gemma-4-E4B-it-uncensored`

- path: `/home/flip/models/gemma-4-E4B-it-uncensored-Q4_K_M.gguf`
- mmproj: none
- ngl: 99, tensor_split: `0.32,0.68`, kv_type: `f16`
- extra_args: `--flash-attn on --parallel 1 --batch-size 256 --ubatch-size 128 --repeat-penalty 1 --repeat-last-n 128 --dry-multiplier 1.0 --dry-base 1.75 --dry-penalty-last-n 256 --temp 1.0 --top-p 0.95 --top-k 64`
- load+switch (first run): **17.262 s**
- median TTFT: **0 s**
- median gen speed: **70.49 tok/s**
- median prompt eval: **0 tok/s**
- all runs gen tok/s: 70.49, 70.61, 65.39

### `granite-4.1-8b`

- path: `/home/flip/models/granite-4.1-8b-UD-Q8_K_XL.gguf`
- mmproj: none
- ngl: 99, tensor_split: `—`, kv_type: `turbo4`
- extra_args: `--jinja`
- load+switch (first run): **27.133 s**
- median TTFT: **0.145 s**
- median gen speed: **30.97 tok/s**
- median prompt eval: **269.6 tok/s**
- all runs gen tok/s: 30.97, 30.99, 30.61

### `llama3.2-3b`

- path: `/home/flip/models/llama3.2-3b.gguf`
- mmproj: none
- ngl: 99, tensor_split: `—`, kv_type: `f16`
- extra_args: `—`
- load+switch (first run): **10.906 s**
- median TTFT: **0.127 s**
- median gen speed: **126.27 tok/s**
- median prompt eval: **520.1 tok/s**
- all runs gen tok/s: 125.48, 126.27, 128.67

### `qwen3.8-27b`

- path: `/home/flip/models/qwen3.8-27b/Qwen3.8-27B-UD-Q4_K_XL.gguf`
- mmproj: `/home/flip/models/qwen3.8-27b/mmproj-F16.gguf`
- ngl: 99, tensor_split: `0.45,0.55`, kv_type: `turbo4`
- extra_args: `--jinja --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 0.0 --repeat-penalty 1.0`
- load+switch (first run): **23.957 s**
- median TTFT: **9.067 s**
- median gen speed: **63.68 tok/s**
- median prompt eval: **9.2 tok/s**
- all runs gen tok/s: 63.9, 63.4, 63.68

### `qwen3.8-27b-instruct`

- path: `/home/flip/models/qwen3.8-27b/Qwen3.8-27B-UD-Q4_K_XL.gguf`
- mmproj: `/home/flip/models/qwen3.8-27b/mmproj-F16.gguf`
- ngl: 99, tensor_split: `0.45,0.55`, kv_type: `turbo4`
- extra_args: `--jinja --reasoning off --temp 0.7 --top-p 0.8 --top-k 20 --min-p 0.0 --presence-penalty 1.5 --repeat-penalty 1.0`
- load+switch (first run): **15.445 s**
- median TTFT: **0.434 s**
- median gen speed: **17.06 tok/s**
- median prompt eval: **99.1 tok/s**
- all runs gen tok/s: 16.95, 17.06, 17.4

### `unsloth-gemma-4-26B-A4B-it-qat-UD-Q4_K_XL-Q8KV`

- path: `/home/flip/models/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf`
- mmproj: `/home/flip/models/gemma-4-26b-a4b-mmproj-BF16.gguf`
- ngl: 99, tensor_split: `0.36,0.64`, kv_type: `turbo4`
- extra_args: `--flash-attn on --repeat-penalty 1 --temp 1.0 --top-p 0.95 --top-k 64 --main-gpu 1 -sm layer`
- load+switch (first run): **35.923 s**
- median TTFT: **0 s**
- median gen speed: **76.09 tok/s**
- median prompt eval: **0 tok/s**
- all runs gen tok/s: 74.16, 76.09, 77.27
