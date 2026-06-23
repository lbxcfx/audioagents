# Dialogue Service

This module adds a configurable script-first dialogue layer in front of the existing LiveKit `ASR -> LLM -> TTS` path.

## Runtime Path

```text
LiveKit audio
  -> ASR
  -> ScriptFirstLLM
      -> dialogue-service /api/dialogue/turn
          -> flow / knowledge hit: return fixed text
          -> disabled / missed: handled=false
      -> upstream Qwen LLM when handled=false
  -> TTS
```

## Start The GUI And API

Default ops server:

```powershell
.\scripts\start-ops.ps1
```

The script starts FastAPI on `http://127.0.0.1:8090` by default.

For a direct Windows run:

```powershell
python -m uvicorn server.main:app --host 127.0.0.1 --port 8090
```

Open:

```text
http://127.0.0.1:8090/
```

## LiveKit Agent Settings

The agent now wraps the original Qwen LLM with `ScriptFirstLLM`.

Environment variables:

```text
QWEN_NLU_ENABLED=true
QWEN_DIALOGUE_URL=http://127.0.0.1:8090/api/dialogue/turn
QWEN_DIALOGUE_SCENE_ID=
QWEN_DIALOGUE_TIMEOUT=0.8
```

Set `QWEN_NLU_ENABLED=false` to keep the original chain:

```text
ASR -> LLM -> TTS
```

The GUI switch also controls the backend service. If the GUI switch is off, `/api/dialogue/turn` returns:

```json
{
  "handled": false,
  "route_type": "disabled",
  "reason": "nlu_disabled"
}
```

## Main APIs

```text
GET  /api/dialogue/config
PUT  /api/dialogue/config
GET  /api/dialogue/scenes
GET  /api/dialogue/scenes/{scene_id}
PUT  /api/dialogue/scenes/{scene_id}
POST /api/dialogue/scenes/{scene_id}/publish
POST /api/dialogue/scenes/{scene_id}/knowledge
PUT  /api/dialogue/knowledge/{item_id}
GET  /api/dialogue/unresolved
POST /api/dialogue/turn
```

## First Version Scope

- GUI: scene list, flow canvas, node inspector, knowledge base, label rules, unresolved learning, simulation, NLU switch.
- Backend: SQLite storage, flow validation, keyword knowledge hit, five-way intent routing, unresolved question collection.
- LiveKit: script-first LLM adapter with safe fallback to Qwen LLM.
