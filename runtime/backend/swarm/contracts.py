"""Strict output contracts shared by provider finalization and durable publication."""
import json


def structured(content):
    """Accept one explicit JSON decision, optionally surrounded by commentary.

    Old saved completions can contain prose/fences even when prompted for JSON.
    Never infer a decision from prose, pick between conflicting objects, accept
    duplicate keys, or salvage a nested decision from a malformed root object.
    """
    text = content.strip()
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Agent JSON contains duplicate fields")
            result[key] = value
        return result
    decoder = json.JSONDecoder(object_pairs_hook=unique_keys)
    try:
        value = decoder.decode(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise ValueError("Agent must return one explicit JSON object") from None
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            raise ValueError("Agent returned malformed JSON; decision was not accepted") from None
        if "{" in text[end:] or (text.startswith("{") and text[end:].strip()):
            raise ValueError("Agent response contains ambiguous extra JSON or trailing data")
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object from the agent")
    return value


def validate_plan(content):
    plan = structured(content).get("items")
    if not isinstance(plan, list) or not 1 <= len(plan) <= 24:
        raise ValueError("Planner must produce 1–24 bounded assignments")
    ids = set()
    for part in plan:
        if not isinstance(part, dict) or not all(isinstance(part.get(k), str) and part[k].strip() for k in ("id", "title", "prompt")):
            raise ValueError("Each assignment needs an id, title, and concrete prompt")
        if part["id"] in ids:
            raise ValueError("Planner returned duplicate assignment ids")
        ids.add(part["id"])
        part.setdefault("dependencies", [])
        if not isinstance(part["dependencies"], list) or any(not isinstance(d, str) for d in part["dependencies"]):
            raise ValueError("Assignment dependencies must be a list of ids")
    completed = set()
    while len(completed) < len(ids):
        ready = {p["id"] for p in plan if p["id"] not in completed and set(p["dependencies"]) <= completed}
        if not ready:
            raise ValueError("Planner returned a dependency cycle or missing assignment")
        completed |= ready
    return plan


def validate_verification(content):
    value = structured(content)
    if not isinstance(value.get("accepted"), bool):
        raise ValueError("Verifier must explicitly accept or reject the deliverable")
    if not isinstance(value.get("result"), str):
        raise ValueError("Verifier must include a result string")
    if not isinstance(value.get("issues"), list) or any(not isinstance(x, str) for x in value["issues"]):
        raise ValueError("Verifier must include an issues list")
    if value["accepted"] and (not value["result"].strip() or value["issues"]):
        raise ValueError("Accepted verification requires a substantive result and no unresolved issues")
    if not value["accepted"] and not value["issues"]:
        raise ValueError("Rejected verification must explain the missing requirements")
    return value
