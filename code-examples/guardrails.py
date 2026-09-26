"""
guardrails.py
=============

Reference implementation of the Guardrails layer (Layer 5).

Two pipelines, each a list of independent checks that return a Verdict:

  Input pipeline  : schema validation -> prompt-injection classifier -> PII/secret
                    detection -> toxicity -> policy rules -> context sanitisation
  Output pipeline : response validation -> grounding (hallucination) check ->
                    PII/secret masking -> toxicity -> policy compliance

Design rules:
  * Checks are layered: cheap deterministic checks run first, model-based checks
    last, and the pipeline short-circuits on the first hard block.
  * Every check is individually observable (name, latency, verdict) so false
    positives can be tuned from production data.
  * "mask" verdicts transform the text and continue; "block" verdicts stop the
    request and return a safe response; "flag" verdicts continue but are logged
    and can trigger HITL review.
  * Model-based checks (Llama Guard, an LLM judge) are called through the LLM
    gateway so they are metered and versioned like every other model call.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

Action = Literal["allow", "mask", "flag", "block"]


@dataclass
class Verdict:
    check: str
    action: Action
    reason: str = ""
    text: str | None = None          # transformed text when action == "mask"
    score: float | None = None
    latency_ms: int = 0


@dataclass
class GuardrailResult:
    allowed: bool
    text: str
    verdicts: list[Verdict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    safe_response: str | None = None


# --------------------------------------------------------------------------- #
# Deterministic detectors
# --------------------------------------------------------------------------- #
PII_PATTERNS = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "phone": re.compile(r"\b(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{3}\)?[\s-]?)\d{3}[\s-]?\d{4}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
}
SECRET_PATTERNS = {
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
}
INJECTION_HEURISTICS = [
    re.compile(r"ignore (all |the )?(previous|prior|above) instructions", re.I),
    re.compile(r"you are now (in )?(developer|dan|jailbreak) mode", re.I),
    re.compile(r"(reveal|print|show) (your|the) (system|hidden) prompt", re.I),
    re.compile(r"<\s*/?\s*(system|assistant|tool)\s*>", re.I),          # fake role tags
    re.compile(r"\bBEGIN (SYSTEM|ADMIN) OVERRIDE\b", re.I),
]


def _mask(text: str, patterns: dict[str, re.Pattern], label: str) -> tuple[str, list[str]]:
    hits: list[str] = []
    for name, rx in patterns.items():
        if rx.search(text):
            hits.append(name)
            text = rx.sub(f"[{label}:{name.upper()}]", text)
    return text, hits


# --------------------------------------------------------------------------- #
# Checks (each: async (text, ctx) -> Verdict)
# --------------------------------------------------------------------------- #
Check = Callable[[str, dict[str, Any]], Awaitable[Verdict]]


async def schema_validation(text: str, ctx: dict) -> Verdict:
    max_len = ctx.get("max_input_chars", 20_000)
    if not text.strip():
        return Verdict("schema", "block", "empty input")
    if len(text) > max_len:
        return Verdict("schema", "block", f"input exceeds {max_len} chars")
    return Verdict("schema", "allow")


async def injection_heuristics(text: str, ctx: dict) -> Verdict:
    for rx in INJECTION_HEURISTICS:
        if rx.search(text):
            return Verdict("injection_heuristic", "block", f"matched {rx.pattern[:40]}")
    return Verdict("injection_heuristic", "allow")


async def secret_detection(text: str, ctx: dict) -> Verdict:
    masked, hits = _mask(text, SECRET_PATTERNS, "SECRET")
    if hits:
        # Secrets in input are always masked, never forwarded to a model.
        return Verdict("secrets", "mask", f"masked {hits}", text=masked)
    return Verdict("secrets", "allow")


async def pii_detection(text: str, ctx: dict) -> Verdict:
    if ctx.get("pii_allowed"):          # e.g. an HR agent operating inside a trusted boundary
        return Verdict("pii", "allow")
    masked, hits = _mask(text, PII_PATTERNS, "PII")
    if hits:
        return Verdict("pii", "mask", f"masked {hits}", text=masked)
    return Verdict("pii", "allow")


def policy_rules(denied_topics: list[str]) -> Check:
    rx = re.compile("|".join(re.escape(t) for t in denied_topics), re.I) if denied_topics else None

    async def check(text: str, ctx: dict) -> Verdict:
        if rx and rx.search(text):
            return Verdict("policy", "block", "denied topic")
        return Verdict("policy", "allow")
    return check


async def context_sanitization(text: str, ctx: dict) -> Verdict:
    # Strip zero-width and bidi control characters used to hide instructions.
    cleaned = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069]", "", text)
    return Verdict("sanitize", "mask" if cleaned != text else "allow", text=cleaned)


# ---- model-based checks (via the LLM gateway) ------------------------------------- #
def llm_safety_classifier(gateway, *, categories: list[str]) -> Check:
    """Llama-Guard-style classification. Cheap model, temperature 0, strict output format."""
    prompt = ("Classify the USER TEXT for the following unsafe categories: "
              f"{', '.join(categories)}. Reply with 'safe' or 'unsafe: <category>' only.")

    async def check(text: str, ctx: dict) -> Verdict:
        t0 = time.time()
        out = await gateway.complete(task_class="simple", temperature=0, max_tokens=20,
                                     messages=[{"role": "system", "content": prompt},
                                               {"role": "user", "content": f"USER TEXT:\n{text[:6000]}"}])
        label = out["text"].strip().lower()
        action: Action = "block" if label.startswith("unsafe") else "allow"
        return Verdict("safety_classifier", action, label, latency_ms=int((time.time() - t0) * 1000))
    return check


def grounding_check(gateway, *, threshold: float = 0.7) -> Check:
    """Hallucination detection: an LLM judge scores whether the answer is supported by the evidence."""
    async def check(text: str, ctx: dict) -> Verdict:
        evidence = ctx.get("evidence")
        if not evidence:
            return Verdict("grounding", "flag", "no evidence supplied; cannot verify")
        out = await gateway.complete(task_class="simple", temperature=0, max_tokens=200, messages=[
            {"role": "system", "content":
             "You are a fact-checker. Given EVIDENCE and ANSWER, list claims in the ANSWER that are not "
             "supported by the EVIDENCE. Respond as JSON: {\"faithfulness\": 0..1, \"unsupported\": [..]}"},
            {"role": "user", "content": json.dumps({"EVIDENCE": evidence, "ANSWER": text}, default=str)[:30_000]},
        ])
        try:
            data = json.loads(out["text"].strip().strip("`"))
        except json.JSONDecodeError:
            return Verdict("grounding", "flag", "judge output unparsable")
        score = float(data.get("faithfulness", 0))
        if score < threshold:
            return Verdict("grounding", "block", f"unsupported: {data.get('unsupported')}", score=score)
        return Verdict("grounding", "allow", score=score)
    return check


async def response_validation(text: str, ctx: dict) -> Verdict:
    if ctx.get("expect_json"):
        try:
            json.loads(text)
        except json.JSONDecodeError:
            return Verdict("response_schema", "block", "expected JSON response")
    if len(text) > ctx.get("max_output_chars", 40_000):
        return Verdict("response_schema", "block", "response too long")
    return Verdict("response_schema", "allow")


# --------------------------------------------------------------------------- #
# Pipeline runner
# --------------------------------------------------------------------------- #
SAFE_RESPONSE = ("I can't help with that request. If you think this is a mistake, "
                 "please rephrase or contact support.")


class GuardrailPipeline:
    def __init__(self, checks: list[Check], *, name: str, telemetry=None):
        self.checks, self.name, self.telemetry = checks, name, telemetry

    async def run(self, text: str, ctx: dict[str, Any] | None = None) -> GuardrailResult:
        ctx = ctx or {}
        result = GuardrailResult(allowed=True, text=text)
        for check in self.checks:
            t0 = time.time()
            v = await check(result.text, ctx)
            v.latency_ms = v.latency_ms or int((time.time() - t0) * 1000)
            result.verdicts.append(v)
            if self.telemetry:
                self.telemetry.record(pipeline=self.name, check=v.check, action=v.action, ms=v.latency_ms)
            if v.action == "mask" and v.text is not None:
                result.text = v.text
            elif v.action == "flag":
                result.flags.append(f"{v.check}: {v.reason}")
            elif v.action == "block":
                result.allowed = False
                result.safe_response = SAFE_RESPONSE
                break
        return result


def build_input_pipeline(gateway, denied_topics: list[str] | None = None) -> GuardrailPipeline:
    return GuardrailPipeline([
        schema_validation,
        context_sanitization,
        injection_heuristics,
        secret_detection,
        pii_detection,
        policy_rules(denied_topics or []),
        llm_safety_classifier(gateway, categories=["violence", "hate", "sexual_minors",
                                                   "self_harm", "weapons", "prompt_injection"]),
    ], name="input")


def build_output_pipeline(gateway) -> GuardrailPipeline:
    return GuardrailPipeline([
        response_validation,
        grounding_check(gateway),
        secret_detection,
        pii_detection,
        llm_safety_classifier(gateway, categories=["violence", "hate", "sexual", "self_harm",
                                                   "dangerous_instructions"]),
    ], name="output")
