# Unsloth GGUF Candidates (19B - 35B)

This list contains models from the `unsloth` Hugging Face collection that meet the criteria:
- **Parameter Count**: 19B - 35B
- **File Size**: < 20 GB
- **Quantization**: Q4 - F16 (where fits)

| Model Name | Parameters | Best Quality Quant < 20GB | Size (approx) | HF Link | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Qwen3-Coder-30B-A3B-Instruct** | 30B | `Q4_K_M` | 18.6 GB | [Link](https://hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF) | Strong coding model, MoE architecture. |
| **Gemma-3-27B-IT** | 27B | `Q5_K_M` | 19.3 GB | [Link](https://hf.co/unsloth/gemma-3-27b-it-GGUF) | Google's latest open model (Gemma 3). |
| **DeepSeek-R1-Distill-Qwen-32B** | 32B | `Q4_K_M` | 19.9 GB | [Link](https://hf.co/unsloth/DeepSeek-R1-Distill-Qwen-32B-GGUF) | Distilled reasoning model, very popular. |
| **Qwen3-32B** | 32B | `Q4_K_M` | 19.8 GB | [Link](https://hf.co/unsloth/Qwen3-32B-GGUF) | Base Qwen3 model (General purpose). |
| **Devstral-Small-2-24B-Instruct** | 24B | `Q6_K` | 19.3 GB | [Link](https://hf.co/unsloth/Devstral-Small-2-24B-Instruct-2512-GGUF) | **High Precision Fit!** Mistral Small 3 architecture. |
| **Qwen3-VL-30B-A3B-Instruct** | 30B | `Q4_K_M` | 18.6 GB | [Link](https://hf.co/unsloth/Qwen3-VL-30B-A3B-Instruct-GGUF) | Vision-Language model variant. |
| **Gpt-Oss-20B** | 20B | `Q6_K` / `Q8_0` | ~16-20 GB | [Link](https://hf.co/unsloth/gpt-oss-20b-GGUF) | (Estimated) - F16 was >40GB, but Q6/Q8 should fit easily. |

## Recommendation
- **For Coding**: `Qwen3-Coder-30B` (Q4_K_M)
- **For General Intelligence**: `DeepSeek-R1-Distill-Qwen-32B` (Q4_K_M) or `Gemma-3-27B` (Q5_K_M allows higher precision).
- **For High Precision/Accuracy**: `Devstral-Small-2-24B` (Q6_K fits!)
