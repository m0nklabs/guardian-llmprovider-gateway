# Model & Terminology Guide

## Our Model Collection: What makes them special?

Below is an overview of the models in your registry and why they are unique.

| Model | Why special? |
| :--- | :--- |
| **Qwen3-VL-30B-A3B-Thinking** | **The All-Rounder.** Combines vision (looking at images) with "Thinking" (reasoning). Can "look and think" simultaneously. Very powerful. |
| **DeepSeek-R1-Distill-32B** | **The Thinker.** A "distilled" version of the gigantic DeepSeek-R1. Has the reasoning power of a giant in the size of a dwarf. Uncensored = no moralizing. |
| **GPT-OSS 20B** | **The Promise.** An open-source model attempting to mimic the "GPT-4 experience". Very compliant with instructions and general knowledge. |
| **Nemotron-3-Nano-30B** | **The Specialist.** Optimized by NVIDIA. Despite the "Nano" name, this is a serious 30B model that runs extremely efficiently on GPUs. |
| **Ministral-3-14B-Reasoning** | **The Smart Little One.** Mistral's latest attempt to bring "reasoning" to smaller models. Ideal for complex logic without massive memory usage. |
| **Phi-4 (Reasoning/Base)** | **Microsoft's Prodigy.** Known for bizarre performance for its size. Trained on "textbook quality" data. Excellent in math and logic. |
| **GLM-4.7-Flash** | **The Speed Demon.** Optimized for pure speed (tokens per second). Ideal for simple tasks where you want an instant answer. |
| **Gemma 3 (27B)** | **Google's Pride.** The open variant of Google's Gemini. Very strong in creative writing and multilingual capabilities. |
| **Step3-VL-10B** | **The Vision Expert.** A lighter model specifically good at analyzing images (Vision-Language). |
| **Nomic-Embed-v2-MoE** | **The Search Engine.** Not a chat model, but an "Embedding" model. Converts text into numbers so your database (RAG) can search within it. Uses MoE technique for efficiency. |

---

# LLM Glossary

Below is an overview of commonly used terms, abbreviations, and suffixes in the world of LLMs (Large Language Models), specifically focused on local inference with tools like `llama.cpp`.

| Term | Category | Explanation |
| :--- | :--- | :--- |
| **Precision & Quantization** | | |
| **FP16** | Precision | *Floating Point 16-bit* (Half Precision). The standard 'high quality' for models. Uses a lot of VRAM. |
| **BF16** | Precision | *Brain Float 16*. Similar to FP16, but more stable for training. For inference, practically identical to FP16. |
| **FP32** | Precision | *Full Precision* (32-bit). Enormously large, rarely used for inference, only for training. |
| **Q8_0** | Quantization | 8-bit compression. Almost identical quality to FP16, but consumes half the memory. Very high quality. |
| **Q6_K** | Quantization | 6-bit. A good intermediate step that is slightly smaller than Q8 but better than Q5/Q4. |
| **Q5_K_M** | Quantization | 5-bit Medium. Slightly higher detail level than the standard Q4, with a small speed penalty. |
| **Q4_K_M** | Quantization | 4-bit Medium. The **gold standard** for local use. Best balance between quality, speed, and VRAM usage. |
| **Q4_K_S** | Quantization | 4-bit Small. Slightly smaller and faster than Medium, but can sometimes be slightly less accurate. |
| **IQ4_NL** | Quantization | *Importance Matrix Quantization*. A newer, smarter way of compressing that achieves higher quality from 4-bits. |
| **Model Types & Suffixes** | | |
| **Base** | Type | The "raw" model. Can only complete text, doesn't understand instructions. Needs training for chat. |
| **Instruct** | Type | Trained to follow specific instructions (e.g., "Summarize this", "Write Python code"). |
| **Chat** | Type | Optimized for natural conversations and back-and-forth dialogue. |
| **Reasoning** | Feature | Models trained to "think" via *Chain-of-Thought* for an answer. Often slower, but much smarter in logic. |
| **Thinking** | Feature | Often synonymous with Reasoning. The model shows its "thoughts" (often in `<think>` tags) in the output. |
| **Flash** | Suffix | Designation for models optimized for pure speed and low latency (e.g., GLM-4-Flash). |
| **Turbo** | Suffix | Often the faster, slightly lighter variant of a flagship model. |
| **Distill** | Training | A smaller model trained by a larger "teacher" model. Attempts to cram the smarts of the big one into a small package. |
| **Uncensored** | Variant | A model where built-in refusals and safety filters have been removed. Answers everything. |
| **Abliterated** | Variant | A specific technique to rip out "refusal mechanisms" from a model without retraining it. |
| **Embedding** | Type | **The Translator for Computers.** Unlike chat models that generate text, embedding models convert text (sentences/documents) into a long list of numbers (vectors). These numbers represent the *meaning* of the text. This allows a database to verify if two pieces of text are semantically similar, even if they use different words. Crucial for RAG (retrieving relevant info for the AI). |
| **Architecture & Technique** | | |
| **GGUF** | Format | *GPT-Generated Unified Format*. The standard format for `llama.cpp`. Works on both CPU and GPU. |
| **SafeTensors** | Format | A safe file format for model weights (unlike the unsafe `.pickle`). Standard for HuggingFace. |
| **MoE** | Architecture | *Mixture of Experts*. The model consists of multiple "experts". Only a small part becomes active per word. Very efficient and smart. |
| **VL** | Architecture | *Vision-Language*. Multimodal model that can "see" and understand images in addition to text. |
| **Context** | Metric | The short-term memory of the model, measured in tokens. (e.g., 32k = approx. 24,000 words). |
| **RoPE** | Technique | *Rotary Positional Embeddings*. A mathematical trick that helps models understand where words are located in a long text. |
| **KV Cache** | Technique | *Key-Value Cache*. Memory reservation to remember earlier parts of the conversation. Grows as the conversation gets longer. |
