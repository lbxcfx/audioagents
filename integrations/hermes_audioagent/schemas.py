"""Tool schemas exposed to Hermes."""

_CUSTOMER = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "phone": {
            "type": "string",
            "description": "Customer phone number in E.164 or an 11-digit mainland China number.",
        },
        "name": {"type": "string", "description": "Customer name, when known."},
        "company": {"type": "string", "description": "Customer company, when known."},
        "profile": {
            "description": "Task-specific customer facts. May be text or a small JSON object.",
        },
        "external_id": {
            "type": "string",
            "description": "Optional caller-provided CRM identifier.",
        },
    },
    "required": ["phone"],
}

SUBMIT_OUTBOUND_TASK = {
    "name": "audioagent_submit_outbound_task",
    "description": (
        "Submit an outbound calling task only after the operator has reviewed the phone "
        "numbers, task objective, generated prompt, schedule, and concurrency and explicitly "
        "confirmed execution. The backend rejects confirmed=false."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "task_name": {"type": "string", "minLength": 1, "maxLength": 200},
            "prompt": {
                "type": "string",
                "minLength": 1,
                "maxLength": 24000,
                "description": (
                    "Complete, immutable prompt snapshot for this task. It may use "
                    "{{customer_name}}, {{customer_company}}, {{customer_phone}}, "
                    "{{customer_profile}}, {{session_id}}, and {{scene_id}} placeholders."
                ),
            },
            "customers": {
                "type": "array",
                "minItems": 1,
                "maxItems": 100,
                "items": _CUSTOMER,
            },
            "confirmed": {
                "type": "boolean",
                "description": "Must be true only after explicit operator confirmation.",
            },
            "task_id": {"type": "string", "maxLength": 120},
            "scene_id": {"type": "integer", "minimum": 1},
            "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 100},
            "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
            "scheduled_at": {
                "type": "string",
                "description": "Optional ISO-8601 scheduled time.",
            },
            "agent_name": {"type": "string", "maxLength": 200},
            "trunk_id": {"type": "string", "maxLength": 200},
            "source_number": {"type": "string", "maxLength": 16},
        },
        "required": ["task_name", "prompt", "customers", "confirmed"],
    },
}

GET_OUTBOUND_TASK = {
    "name": "audioagent_get_outbound_task",
    "description": "Get campaign progress and structured call results for an AudioAgent task.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "campaign_id": {"type": "string", "minLength": 1},
            "include_results": {"type": "boolean", "default": True},
            "include_transcript": {"type": "boolean", "default": False},
        },
        "required": ["campaign_id"],
    },
}

WAIT_OUTBOUND_TASK = {
    "name": "audioagent_wait_outbound_task",
    "description": (
        "Wait for a submitted task to finish and return structured results. Use this in a "
        "Hermes background session for long calls so the originating chat stays responsive."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "campaign_id": {"type": "string", "minLength": 1},
            "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 3600},
            "poll_seconds": {"type": "number", "minimum": 1, "maximum": 30},
            "include_transcript": {"type": "boolean", "default": False},
        },
        "required": ["campaign_id"],
    },
}

CANCEL_OUTBOUND_TASK = {
    "name": "audioagent_cancel_outbound_task",
    "description": "Cancel queued calls for an AudioAgent campaign after explicit operator request.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "campaign_id": {"type": "string", "minLength": 1},
            "confirmed": {"type": "boolean"},
        },
        "required": ["campaign_id", "confirmed"],
    },
}
