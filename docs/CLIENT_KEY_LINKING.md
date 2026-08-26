# Client-key linken aan alle cloud-providers (guardian/* routes zichtbaar maken)

Wanneer een client-API-key (bv. een nieuwe pi-key of een andere tool) de
`guardian/{provider}/{model}` routes niet te zien krijgt in `/v1/models`,
ligt dat aan de `links`-map in `config/cloud_keys.json` — niet aan de
dedup-warning in `providers.py`.

## Root cause

De per-key routes `guardian/{provider}/{model}` worden uitsluitend opgebouwd
in `app/gateway/model_discovery.py::list_models` via
`CloudCredentialStore.get_linked_models_for_key(key_fp)` (zie
`app/proxy/cloud_keys.py`). Die functie leest alleen vingerafdrukken die onder
de top-level `"links"`-key staan. Staat een key-fingerprint er niet in, dan
krijgt die client **nul** `guardian/*` routes geadverteerd (wel de globale
modellen zoals `z-ai/glm-5.2`, `openrouter/z-ai/glm-5.2`, etc.).

De bekende warning
`⚠️ Model 'z-ai/glm-5.2' is registered on both 'openrouter' and 'nvidia'; keeping the first ('openrouter')`
uit `app/proxy/providers.py:185` beïnvloedt alleen de **globale** modellijst
(één entry per model); de per-key `guardian/{provider}/{model}` routes zijn
onafhankelijk en worden per credential uit `cloud_keys.json` gebouwd.

## Procedure

1. Bepaal de key-vingerafdruk (zelfde algoritme als Guardian-auth):
   ```bash
   python3 -c "import hashlib; print(hashlib.sha256(b'<TOKEN>').hexdigest()[:12])"
   ```
2. Backup:
   ```bash
   cd /home/flip/guardian-llmprovider-gateway
   cp config/cloud_keys.json config/cloud_keys.json.bak.$(date +%s)
   ```
3. Voeg onder `"links"` een entry toe (credential-ID's staan in dezelfde file
   onder `"credentials"`; pas aan als er nieuwe credentials zijn):
   ```json
   "<fingerprint>": {
     "nvidia": "cred_1bdc257b",
     "openrouter": "cred_4edcf709",
     "poolside": "cred_1ded4a0b",
     "openai": "cred_0cb0d01e",
     "google": "cred_b620ca88"
   }
   ```
4. Herstart Guardian (de credential store leest alleen bij startup):
   ```bash
   sudo systemctl restart guardian-llmprovider-gateway.service
   ```
5. Verifieer dat de routes zichtbaar zijn:
   ```bash
   curl -s -H "Authorization: Bearer <TOKEN>" http://192.168.1.35:11434/v1/models \
     | python3 -c "import sys,json; d=json.load(sys.stdin); ids=[m['id'] for m in d.get('data',[])]; print('total:',len(ids)); print('\n'.join(sorted(i for i in ids if i.startswith('guardian/'))))"
   ```
   Verwacht: ~211 modellen, ~99 `guardian/*` routes, inclusief
   `guardian/nvidia/z-ai/glm-5.2`, `guardian/nvidia/minimaxai/minimax-m3`,
   `guardian/openrouter/z-ai/glm-5.2`, etc.
6. Pi cached modellen lokaal in `~/.pi/agent/models.json`: herstart pi (of
   forceer een model-refresh) zodat de nieuwe routes in de model-picker
   verschijnen.

## Per-provider credential-ID's (snapshot 2026-08-15)

| provider    | credential_id   |
|------------|-----------------|
| nvidia     | cred_1bdc257b   |
| openrouter | cred_4edcf709   |
| poolside   | cred_1ded4a0b  |
| openai     | cred_0cb0d01e  |
| google     | cred_b620ca88   |

Controleren: `python3 -c "import json; d=json.load(open('config/cloud_keys.json')); print({k:v['provider'] for k,v in d['credentials'].items()})"`.
